#!/usr/bin/env python3
"""
Android Accessibility Audit

Audit app screens for accessibility issues and compliance.
Checks missing content descriptions, unlabelled fields, disabled-but-clickable
controls, and touch targets below 48dp (measured against the device's real
density, not in raw pixels).

Contrast is deliberately not claimed: it needs pixel sampling from a
screenshot, which this script does not do. It was listed here before it was
written, which is worse than not offering it.

Usage Examples:
    # Audit current screen
    python scripts/accessibility_audit.py

    # Audit with detailed report
    python scripts/accessibility_audit.py --verbose

    # Save audit report
    python scripts/accessibility_audit.py --output audit-reports/

Exit code:
    Returns 1 when any critical issue is found (CI gate), else 0.

Tunables (env, ANDROID_EMU_ prefix):
    ANDROID_EMU_A11Y_MAX_NESTING  Depth above which nodes are flagged (default 5)
    ANDROID_EMU_A11Y_TOP_ISSUES   Grouped issue types shown in console (default 10)
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from common.adb_exec import AdbError
from common.device_utils import get_device_density, get_ui_hierarchy
from common.env_config import env_int
from common.hierarchy import is_interactive, parse_bounds

# Tunable thresholds (overridable via env, ANDROID_EMU_ prefix).
A11Y_MAX_NESTING = env_int("ANDROID_EMU_A11Y_MAX_NESTING", 5)
A11Y_TOP_ISSUES = env_int("ANDROID_EMU_A11Y_TOP_ISSUES", 10)

# Human-readable fix suggestions keyed by issue type.
FIX_SUGGESTIONS = {
    "missing_content_description": "Add android:contentDescription to the element",
    "small_touch_target": "Increase touch target to at least 48x48dp",
    "image_missing_description": (
        "Add android:contentDescription, or set importantForAccessibility='no' if decorative"
    ),
    "edittext_missing_hint": "Add android:hint to describe the expected input",
    "long_text_block": "Ensure adequate line spacing and consider breaking up the content",
    "missing_resource_id": "Add android:id so the element can be reliably referenced and tested",
    "deep_nesting": "Flatten the layout to reduce view-hierarchy depth",
}


def _fix_for(issue_type: str) -> str:
    """Return the fix-suggestion string for an issue type."""
    return FIX_SUGGESTIONS.get(issue_type, "Review accessibility")


# Severity ordering used when ranking grouped issues for console output.
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _parse_bounds(bounds_str: str) -> dict:
    """Parse a uiautomator bounds string '[l,t][r,b]' into a dict of ints.

    The grammar itself lives in :func:`common.hierarchy.parse_bounds` -- there
    were three of them in this skill and they did not agree (C5/C7). This keeps
    the dict shape the report's ``element`` payload has always carried.
    """
    box = parse_bounds(bounds_str)
    if box is None:
        return {}
    left, top, right, bottom = box
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def _attr_bool(value: str) -> bool:
    """Coerce a uiautomator string attribute ('true'/'false') to a bool."""
    return str(value).lower() == "true"


class AccessibilityAuditor:
    """Audits Android screens for accessibility issues."""

    # Minimum touch target size (dp)
    # Material's minimum touch target, in dp. uiautomator reports bounds in
    # physical PIXELS, so this must be converted before any comparison -- see
    # min_touch_target_px(). Comparing pixels against this literal meant the
    # check only fired on elements roughly 2.6x too small at 420dpi, and its
    # message labelled the pixel figures "dp" (S7).
    MIN_TOUCH_TARGET_DP = 48

    # Fallback density when the device cannot be queried. mdpi means 1dp == 1px,
    # so the check degrades to the old dp-vs-pixel comparison rather than
    # inventing a scale factor.
    DEFAULT_DENSITY_DPI = 160

    # Minimum text size (sp)
    MIN_TEXT_SIZE = 12

    def __init__(self, serial: str | None = None, max_nesting: int | None = None):
        """
        Initialize accessibility auditor.

        Args:
            serial: Optional device serial
            max_nesting: Depth above which a node is flagged as deeply nested.
                Defaults to the ANDROID_EMU_A11Y_MAX_NESTING tunable.
        """
        self.serial = serial
        self.max_nesting = A11Y_MAX_NESTING if max_nesting is None else max_nesting
        self.issues = []
        # Resolved lazily from the device on first use, so constructing an
        # auditor stays device-free for tests and for callers that only parse.
        self.density: int | None = None

    def _resolve_density(self) -> int:
        """The device's effective dpi, queried once and cached.

        Falls back to mdpi (1dp == 1px) when the device cannot be asked, which
        degrades the touch-target check to its previous behaviour rather than
        inventing a scale factor.
        """
        if self.density is None:
            try:
                self.density = get_device_density(self.serial)
            except (AdbError, RuntimeError):
                self.density = self.DEFAULT_DENSITY_DPI
        return self.density

    def px_to_dp(self, pixels: float) -> float:
        """Convert a pixel measurement to density-independent pixels."""
        return pixels / (self._resolve_density() / 160.0)

    def min_touch_target_px(self) -> float:
        """The 48dp minimum expressed in this device's pixels.

        At 420dpi that is 126px, so a 100px control -- about 38dp, clearly under
        the minimum -- was previously passed because 100 > 48.
        """
        return self.MIN_TOUCH_TARGET_DP * (self._resolve_density() / 160.0)

    @staticmethod
    def _descendants(node: dict):
        """Yield every node beneath this one, in the dict hierarchy shape."""
        for child in node.get("children", []) or []:
            yield child
            yield from AccessibilityAuditor._descendants(child)

    @staticmethod
    def _own_label(node: dict) -> str:
        """The text this node carries in its own right."""
        attributes = node.get("attributes", {})
        return (attributes.get("text") or "").strip() or (
            attributes.get("content-desc") or ""
        ).strip()

    @classmethod
    def _has_label(cls, node: dict) -> bool:
        """Whether anything names this control -- its own text, or its subtree's.

        A screen reader announces a node from its own ``text``/``contentDescription``
        or from those of the nodes it contains. A caption that merely sits
        *beside* the control is not in either place, which is why a sibling does
        not count here even though `screen_mapper` uses one to name the control
        for a sighted agent: that caption is exactly the accessibility defect.
        """
        return bool(cls._own_label(node)) or any(
            cls._own_label(child) for child in cls._descendants(node)
        )

    def audit_tree(self, hierarchy: dict) -> list:
        """Run every check over an already-fetched hierarchy.

        Split out from :meth:`audit` so the checks can be exercised against a
        recorded dump without a device.

        Args:
            hierarchy: The dict shape returned by ``get_ui_hierarchy``.

        Returns:
            The accumulated issue list.
        """
        self.issues = []
        self._audit_node(hierarchy)
        return self.issues

    def audit(self) -> tuple:
        """
        Audit current screen for accessibility issues.

        Returns:
            (success, message, audit_data) tuple
        """
        try:
            # Get UI hierarchy
            hierarchy = get_ui_hierarchy(self.serial)

            # Run checks
            self.audit_tree(hierarchy)

            # Categorize issues
            critical = [i for i in self.issues if i["severity"] == "critical"]
            warnings = [i for i in self.issues if i["severity"] == "warning"]
            info = [i for i in self.issues if i["severity"] == "info"]

            audit_data = {
                "timestamp": datetime.now().isoformat(),
                "total_issues": len(self.issues),
                "critical": len(critical),
                "warnings": len(warnings),
                "info": len(info),
                "issues": self.issues,
            }

            # Generate message
            if len(critical) > 0:
                message = f"Accessibility: {len(critical)} critical, {len(warnings)} warnings"
            elif len(warnings) > 0:
                message = f"Accessibility: {len(warnings)} warnings, {len(info)} info"
            else:
                message = f"Accessibility: No critical issues ({len(info)} info)"

            return True, message, audit_data

        except Exception as e:
            return False, f"Audit failed: {e}", None

    def _audit_node(self, node: dict, depth: int = 0):
        """
        Recursively audit a UI hierarchy node.

        Args:
            node: UI hierarchy node
            depth: Current depth in tree
        """
        attrs = node.get("attributes", {})
        class_name = attrs.get("class", "")
        bounds = _parse_bounds(attrs.get("bounds", ""))
        clickable = _attr_bool(attrs.get("clickable", "false"))
        enabled = _attr_bool(attrs.get("enabled", "true"))
        text = attrs.get("text", "")
        content_desc = attrs.get("content-desc", "")
        resource_id = attrs.get("resource-id", "")

        # Check 1: a control a screen reader cannot announce.
        #
        # Eligibility is `hierarchy.is_interactive` and the label test is "is
        # there any describing text in this node or below it" -- not a class
        # name. The class-name gate ("button", "imagebutton", "imageview") could
        # not fire on a Compose screen at all: Compose renders its controls as
        # `android.view.View`, so the check that Quick Start step 5 exists to
        # run reported zero criticals on every Compose app ever audited (L3).
        # It also missed a clickable `LinearLayout` row, which is how most
        # View-based lists are built.
        if is_interactive(node) and not self._has_label(node):
            self.issues.append(
                {
                    "type": "missing_content_description",
                    "severity": "critical",
                    "message": (
                        f"Interactive {class_name or 'element'} has no label: nothing in its "
                        f"own text, content-desc or subtree names it"
                    ),
                    "fix": _fix_for("missing_content_description"),
                    "element": {
                        "class": class_name,
                        "resource_id": resource_id,
                        "bounds": bounds,
                    },
                }
            )

        # Check 2: Touch target size. Bounds are pixels; the minimum is dp.
        if clickable and enabled and bounds:
            width = bounds.get("right", 0) - bounds.get("left", 0)
            height = bounds.get("bottom", 0) - bounds.get("top", 0)
            minimum_px = self.min_touch_target_px()

            if width < minimum_px or height < minimum_px:
                self.issues.append(
                    {
                        "type": "small_touch_target",
                        "severity": "warning",
                        "message": (
                            f"Touch target too small: "
                            f"{self.px_to_dp(width):.0f}x{self.px_to_dp(height):.0f}dp "
                            f"({width}x{height}px, min: {self.MIN_TOUCH_TARGET_DP}dp)"
                        ),
                        "fix": _fix_for("small_touch_target"),
                        "element": {
                            "class": class_name,
                            "resource_id": resource_id,
                            "size": f"{width}x{height}",
                        },
                    }
                )

        # Check 3: Images need content descriptions
        if "imageview" in class_name.lower() and not content_desc:
            # Decorative images can skip this, but we flag it as info
            self.issues.append(
                {
                    "type": "image_missing_description",
                    "severity": "info",
                    "message": "ImageView missing content description (okay if decorative)",
                    "fix": _fix_for("image_missing_description"),
                    "element": {
                        "class": class_name,
                        "resource_id": resource_id,
                    },
                }
            )

        # Check 4: an input the user cannot identify.
        #
        # This used to read attrs.get("hint"), but uiautomator emits no `hint`
        # attribute -- verified across every recorded dump -- so the condition
        # collapsed to "this field is empty" and flagged every correctly-hinted
        # empty field. A field's label is discoverable, just not there: Compose
        # puts a TextField's label in its subtree. Only flag a field with no
        # describing text in it or beneath it -- the same label test check 1
        # applies, so the two cannot drift into disagreeing about "labelled".
        if "edittext" in class_name.lower() and not self._has_label(node):
            self.issues.append(
                {
                    "type": "edittext_missing_hint",
                    "severity": "warning",
                    "message": "EditText missing hint text",
                    "fix": _fix_for("edittext_missing_hint"),
                    "element": {
                        "class": class_name,
                        "resource_id": resource_id,
                    },
                }
            )

        # Check 5: Text readability
        if text and len(text) > 100:
            # Long text blocks should be readable
            self.issues.append(
                {
                    "type": "long_text_block",
                    "severity": "info",
                    "message": f"Long text block ({len(text)} chars) - ensure adequate spacing",
                    "fix": _fix_for("long_text_block"),
                    "element": {
                        "class": class_name,
                        "resource_id": resource_id,
                        "text_length": len(text),
                    },
                }
            )

        # Check 6: Interactive elements should have a resource-id for testing
        if clickable and enabled and not resource_id:
            self.issues.append(
                {
                    "type": "missing_resource_id",
                    "severity": "info",
                    "message": f"Interactive {class_name or 'element'} missing resource-id",
                    "fix": _fix_for("missing_resource_id"),
                    "element": {
                        "class": class_name,
                        "bounds": bounds,
                    },
                }
            )

        # Check 7: Deeply nested elements complicate accessibility navigation
        if depth > self.max_nesting:
            self.issues.append(
                {
                    "type": "deep_nesting",
                    "severity": "info",
                    "message": f"Deeply nested element (depth {depth} > {self.max_nesting})",
                    "fix": _fix_for("deep_nesting"),
                    "element": {
                        "class": class_name,
                        "resource_id": resource_id,
                        "depth": depth,
                    },
                }
            )

        # Recurse to children
        for child in node.get("children", []):
            self._audit_node(child, depth + 1)

    @staticmethod
    def group_top_issues(issues: list, limit: int = A11Y_TOP_ISSUES) -> list:
        """
        Group issues by type and rank them by severity then count.

        Args:
            issues: List of issue dicts (as produced by :meth:`_audit_node`).
            limit: Maximum number of grouped issue types to return.

        Returns:
            A list of grouped dicts (``type``/``severity``/``count``/``fix``),
            sorted by severity (critical first) then descending count, truncated
            to ``limit`` entries.
        """
        grouped: dict[str, dict] = {}
        for issue in issues:
            issue_type = issue["type"]
            if issue_type not in grouped:
                grouped[issue_type] = {
                    "type": issue_type,
                    "severity": issue["severity"],
                    "count": 0,
                    "fix": issue.get("fix", _fix_for(issue_type)),
                }
            grouped[issue_type]["count"] += 1

        return sorted(
            grouped.values(),
            key=lambda g: (_SEVERITY_ORDER.get(g["severity"], 99), -g["count"]),
        )[:limit]

    def save_report(self, output_dir: str, audit_data: dict) -> str:
        """
        Save audit report to file.

        Args:
            output_dir: Directory to save report
            audit_data: Audit data

        Returns:
            Path to saved report
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        json_file = output_path / f"accessibility-audit-{timestamp}.json"

        # Save JSON report
        with open(json_file, "w") as f:
            json.dump(audit_data, f, indent=2)

        # Generate markdown report
        md_file = output_path / f"accessibility-audit-{timestamp}.md"
        self._generate_markdown(md_file, audit_data)

        return str(json_file)

    def _generate_markdown(self, output_path: Path, audit_data: dict):
        """
        Generate markdown audit report.

        Args:
            output_path: Path to save markdown
            audit_data: Audit data
        """
        lines = [
            "# Accessibility Audit Report",
            "",
            f"**Date:** {audit_data['timestamp']}",
            f"**Total Issues:** {audit_data['total_issues']}",
            f"**Critical:** {audit_data['critical']}",
            f"**Warnings:** {audit_data['warnings']}",
            f"**Info:** {audit_data['info']}",
            "",
            "## Issues by Severity",
            "",
        ]

        # Group by severity
        for severity in ["critical", "warning", "info"]:
            severity_issues = [i for i in audit_data["issues"] if i["severity"] == severity]

            if severity_issues:
                lines.append(f"### {severity.upper()} ({len(severity_issues)})")
                lines.append("")

                for issue in severity_issues:
                    symbol = (
                        "❌"
                        if severity == "critical"
                        else ("⚠️" if severity == "warning" else "ℹ️")
                    )
                    lines.append(f"**{symbol} {issue['type']}**")
                    lines.append(f"- {issue['message']}")

                    if "element" in issue:
                        elem = issue["element"]
                        if "class" in elem:
                            lines.append(f"- Class: `{elem['class']}`")
                        if "resource_id" in elem:
                            lines.append(f"- ID: `{elem['resource_id']}`")

                    lines.append("")

        with open(output_path, "w") as f:
            f.write("\n".join(lines))


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Audit Android screen for accessibility issues",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Audit current screen
  python scripts/accessibility_audit.py

  # Audit with verbose output
  python scripts/accessibility_audit.py --verbose

  # Save report to file
  python scripts/accessibility_audit.py --output audit-reports/

  # JSON output
  python scripts/accessibility_audit.py --json
        """,
    )

    parser.add_argument("--output", help="Save report to directory")
    parser.add_argument(
        "--serial", dest="device_serial", help="Device serial (uses default if not specified)"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose output (show all issues)")

    args = parser.parse_args()

    auditor = AccessibilityAuditor(serial=args.device_serial)

    # Run audit
    success, message, audit_data = auditor.audit()

    if not success:
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)

    # Save report if requested
    if args.output:
        report_path = auditor.save_report(args.output, audit_data)
        print(f"Report saved: {report_path}")

    # Output results
    if args.json:
        print(json.dumps(audit_data, indent=2))
    else:
        print(message)

        # Show issues in verbose mode
        if args.verbose and audit_data["issues"]:
            print("\nIssues:")
            for issue in audit_data["issues"]:
                severity_symbol = (
                    "❌"
                    if issue["severity"] == "critical"
                    else ("⚠️" if issue["severity"] == "warning" else "ℹ️")
                )
                print(f"\n{severity_symbol} {issue['type']} ({issue['severity']})")
                print(f"  {issue['message']}")
                print(f"  Fix: {issue.get('fix', _fix_for(issue['type']))}")

                if "element" in issue:
                    elem = issue["element"]
                    if "class" in elem:
                        print(f"  Class: {elem['class']}")
                    if "resource_id" in elem:
                        print(f"  ID: {elem['resource_id']}")
        elif audit_data["issues"]:
            # Concise default: top issue types grouped by severity then count.
            top_issues = AccessibilityAuditor.group_top_issues(audit_data["issues"])
            print(f"\nTop issues (by severity, count) — showing {len(top_issues)}:")
            for group in top_issues:
                symbol = (
                    "❌"
                    if group["severity"] == "critical"
                    else ("⚠️" if group["severity"] == "warning" else "ℹ️")
                )
                print(f"  {symbol} {group['type']} ({group['count']}x) - {group['fix']}")

    # CI gate: non-zero exit when any critical issue is present.
    if audit_data["critical"] > 0:
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
