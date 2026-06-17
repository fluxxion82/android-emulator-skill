#!/usr/bin/env python3
"""
Android String Resource Localization Audit

Parses ``res/values*/strings.xml`` across locales and reports localization
gaps. This is pure file analysis — no device or emulator is required (and so
there is no ``--serial`` flag; the audit operates on a ``res/`` tree on disk).

Android-native (NOT an iOS .xcstrings port):
  - Locale resolution follows Android resource-qualifier rules: ``values/`` is
    the default (typically English), and ``values-<locale>/`` (e.g.
    ``values-es/``, ``values-fr/``, ``values-zh-rCN/``) hold translations.
  - All resource types are parsed: ``<string>``, ``<plurals>`` (per-quantity
    ``<item>``), and ``<string-array>`` (ordered ``<item>`` list).

Reports:
  - Per-locale missing keys vs. the default ``values/strings.xml``.
  - Placeholder mismatches (``%s``, ``%d``, positional ``%1$s``) between the
    default value and each translation.
  - With ``--source DIR``: keys referenced in code (``R.string.NAME``,
    ``getString(R.string.NAME)``, ``stringResource(R.string.NAME)``) that are
    undefined, and defined keys that are never referenced (unused).

Usage:
    python scripts/localization_audit.py --res app/src/main/res
    python scripts/localization_audit.py --res ./res --source ./src
    python scripts/localization_audit.py --res ./res --locale es --strict --json
"""

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common.env_config import env_int

# Tunable defaults (override via the ANDROID_EMU_ prefix). Caps keep default
# output token-efficient; --verbose/--json always emit the full detail.
MAX_LISTED_KEYS = env_int("ANDROID_EMU_LOCALIZATION_MAX_LISTED_KEYS", 10, min_value=1)


# === TYPES ===


@dataclass
class MissingKey:
    """A key present in the default locale but absent from a translation."""

    key: str
    locale: str
    kind: str  # "string", "plurals", or "string-array"


@dataclass
class PlaceholderMismatch:
    """A key whose format specifiers differ between default and a translation."""

    key: str
    locale: str
    default_placeholders: list[str]
    locale_placeholders: list[str]


@dataclass
class AuditReport:
    """Structured result of a full string-resource audit."""

    res_dir: str
    default_locale: str
    total_keys: int
    locales: list[str]
    missing_keys: list[MissingKey] = field(default_factory=list)
    placeholder_mismatches: list[PlaceholderMismatch] = field(default_factory=list)
    undefined_in_resources: list[str] = field(default_factory=list)  # used in code, undefined
    unused_in_source: list[str] = field(default_factory=list)  # defined, never referenced

    def has_findings(self) -> bool:
        return bool(
            self.missing_keys
            or self.placeholder_mismatches
            or self.undefined_in_resources
            or self.unused_in_source
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# === PLACEHOLDER EXTRACTION ===

# Android printf-style format specifiers used in strings.xml:
#   %s %d %f ... plus positional %1$s %2$d, width/precision/flags, and length
#   modifiers. Mirrors java.util.Formatter conversions.
_PLACEHOLDER_RE = re.compile(
    r"%(?:\d+\$)?(?:[-+ 0,(#]*)?(?:\d+)?(?:\.\d+)?(?:hh|h|ll|l|z|t|q)?[bBhHsScCdoxXeEfgGaA%n]"
)

# Source references: R.string.NAME, getString(R.string.NAME),
# stringResource(R.string.NAME), getString(R.plurals.NAME), @string/NAME.
_SOURCE_REF_RE = re.compile(r"R\.(?:string|plurals|array)\.([A-Za-z_]\w*)")
_XML_REF_RE = re.compile(r"@(?:string|plurals|array)/([A-Za-z_]\w*)")


def _extract_placeholders(value: str) -> list[str]:
    """Extract all printf-style format specifiers from a string value."""
    # A bare "%%" is a literal percent, not a placeholder — drop it.
    return [p for p in _PLACEHOLDER_RE.findall(value) if p != "%%"]


# === RESOURCE PARSING ===


def locale_from_dir(dir_name: str) -> str:
    """
    Derive a locale code from a ``values*`` resource directory name.

    ``values`` -> "" (the default locale). ``values-es`` -> "es".
    ``values-zh-rCN`` -> "zh-rCN". Non-locale qualifiers (e.g. ``values-night``,
    ``values-sw600dp``) still produce a token here; callers decide relevance by
    whether a ``strings.xml`` exists with translatable keys.
    """
    if dir_name == "values":
        return ""
    return dir_name.removeprefix("values-")


def _node_text(node: ET.Element) -> str:
    """Flatten an element's text content (including nested tags like <b>)."""
    parts: list[str] = []
    if node.text:
        parts.append(node.text)
    for child in node:
        parts.append(_node_text(child))
        if child.tail:
            parts.append(child.tail)
    return "".join(parts)


def parse_strings_xml(path: Path) -> dict[str, dict[str, Any]]:
    """
    Parse a single ``strings.xml`` into a key -> entry map.

    Each entry is ``{"kind": str, "value": str}`` where ``value`` is the
    concatenation of all translatable text used for placeholder comparison:
      - ``<string>``  -> the element text
      - ``<plurals>`` -> all ``<item>`` texts joined
      - ``<string-array>`` -> all ``<item>`` texts joined

    Entries marked ``translatable="false"`` are skipped (they are not localized).
    Raises ValueError on malformed XML.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML in {path}: {exc}") from exc

    entries: dict[str, dict[str, Any]] = {}
    for elem in root:
        if elem.tag not in {"string", "plurals", "string-array"}:
            continue
        name = elem.get("name")
        if not name:
            continue
        if (elem.get("translatable") or "").lower() == "false":
            continue

        if elem.tag == "string":
            value = _node_text(elem)
        else:
            value = " ".join(_node_text(item) for item in elem.findall("item"))

        entries[name] = {"kind": elem.tag, "value": value}

    return entries


def load_locales(res_dir: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Load every ``values*/strings.xml`` under ``res_dir``.

    Returns a map ``locale -> {key -> entry}``. The default locale ("") comes
    from ``values/strings.xml``. Directories without a ``strings.xml`` are
    ignored. Raises ValueError if any file is malformed.
    """
    locales: dict[str, dict[str, dict[str, Any]]] = {}
    for child in sorted(res_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name != "values" and not child.name.startswith("values-"):
            continue
        strings_file = child / "strings.xml"
        if not strings_file.is_file():
            continue
        locale = locale_from_dir(child.name)
        locales[locale] = parse_strings_xml(strings_file)
    return locales


# === SOURCE SCANNER ===


def scan_source_refs(source_dir: Path) -> set[str]:
    """
    Scan source files under ``source_dir`` for referenced string resource keys.

    Matches ``R.string.NAME`` (covers ``getString(R.string.NAME)`` and
    ``stringResource(R.string.NAME)``), ``R.plurals.NAME``, ``R.array.NAME``,
    and XML ``@string/NAME`` references in Java, Kotlin, and XML layouts.
    """
    keys: set[str] = set()
    suffixes = {".kt", ".java", ".xml"}
    for path in source_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        keys.update(_SOURCE_REF_RE.findall(content))
        keys.update(_XML_REF_RE.findall(content))
    return keys


# === CORE AUDITOR ===


class LocalizationAuditor:
    """Audits an Android ``res/`` tree for string-localization gaps."""

    def __init__(
        self,
        res_dir: Path,
        source_dir: Path | None = None,
        locale_filter: str | None = None,
    ):
        self.res_dir = res_dir
        self.source_dir = source_dir
        self.locale_filter = locale_filter

    def _collect_missing(
        self,
        default: dict[str, dict[str, Any]],
        locales: dict[str, dict[str, dict[str, Any]]],
        translation_locales: list[str],
    ) -> list[MissingKey]:
        """Find default keys absent from each translation locale."""
        missing: list[MissingKey] = []
        for locale in translation_locales:
            translated = locales[locale]
            for key, entry in default.items():
                if key not in translated:
                    missing.append(MissingKey(key=key, locale=locale, kind=entry["kind"]))
        return sorted(missing, key=lambda m: (m.locale, m.key))

    def _check_placeholders(
        self,
        default: dict[str, dict[str, Any]],
        locales: dict[str, dict[str, dict[str, Any]]],
        translation_locales: list[str],
    ) -> list[PlaceholderMismatch]:
        """Compare format specifiers between default and each translation."""
        mismatches: list[PlaceholderMismatch] = []
        for locale in translation_locales:
            translated = locales[locale]
            for key, entry in default.items():
                if key not in translated:
                    continue  # already reported as missing
                default_ph = sorted(_extract_placeholders(entry["value"]))
                locale_ph = sorted(_extract_placeholders(translated[key]["value"]))
                if default_ph != locale_ph:
                    mismatches.append(
                        PlaceholderMismatch(
                            key=key,
                            locale=locale,
                            default_placeholders=default_ph,
                            locale_placeholders=locale_ph,
                        )
                    )
        return sorted(mismatches, key=lambda m: (m.locale, m.key))

    def audit(self) -> AuditReport:
        """Run the full audit and return a structured report."""
        locales = load_locales(self.res_dir)

        default = locales.get("", {})
        translation_locales = sorted(loc for loc in locales if loc != "")
        if self.locale_filter:
            translation_locales = [loc for loc in translation_locales if loc == self.locale_filter]

        report = AuditReport(
            res_dir=str(self.res_dir),
            default_locale="values",
            total_keys=len(default),
            locales=translation_locales,
        )

        report.missing_keys = self._collect_missing(default, locales, translation_locales)
        report.placeholder_mismatches = self._check_placeholders(
            default, locales, translation_locales
        )

        if self.source_dir:
            referenced = scan_source_refs(self.source_dir)
            defined = set(default.keys())
            report.undefined_in_resources = sorted(referenced - defined)
            report.unused_in_source = sorted(defined - referenced)

        return report


# === OUTPUT FORMATTING ===


def _format_default(report: AuditReport) -> str:
    """Compact summary — a few lines."""
    missing_by_locale: dict[str, int] = {}
    for m in report.missing_keys:
        missing_by_locale[m.locale] = missing_by_locale.get(m.locale, 0) + 1
    missing_summary = (
        ", ".join(f"{count} in {loc}" for loc, count in sorted(missing_by_locale.items())) or "none"
    )

    lines = [
        f"Resources: {report.total_keys} keys, {len(report.locales)} translation locale(s).",
        f"Missing keys: {missing_summary}.",
    ]
    if report.placeholder_mismatches:
        lines.append(f"Placeholder mismatches: {len(report.placeholder_mismatches)}.")
    if report.undefined_in_resources:
        lines.append(f"Referenced but undefined: {len(report.undefined_in_resources)} keys.")
    if report.unused_in_source:
        lines.append(f"Defined but unused: {len(report.unused_in_source)} keys.")
    if not report.has_findings():
        lines.append("No issues found.")
    return "\n".join(lines)


def _format_key_list(title: str, keys: list[str]) -> list[str]:
    """Render a capped key list for default output."""
    out = [title]
    shown = keys[:MAX_LISTED_KEYS]
    out.extend(f"  {k}" for k in shown)
    remaining = len(keys) - len(shown)
    if remaining > 0:
        out.append(f"  ... and {remaining} more (use --verbose or --json)")
    return out


def _format_verbose(report: AuditReport) -> str:
    """Detailed listing of every finding."""
    sections: list[str] = [_format_default(report), ""]

    if report.missing_keys:
        sections.append("=== Missing Keys ===")
        current = None
        for m in report.missing_keys:
            if m.locale != current:
                sections.append(f"\n[{m.locale}]")
                current = m.locale
            sections.append(f"  {m.kind:13s}  {m.key}")

    if report.placeholder_mismatches:
        sections.append("\n=== Placeholder Mismatches ===")
        for m in report.placeholder_mismatches:
            sections.append(
                f"  [{m.locale}] {m.key}\n"
                f"    values: {m.default_placeholders}\n"
                f"    {m.locale}: {m.locale_placeholders}"
            )

    if report.undefined_in_resources:
        sections.append("\n=== Referenced in Source, Undefined in Resources ===")
        sections.extend(f"  {k}" for k in report.undefined_in_resources)

    if report.unused_in_source:
        sections.append("\n=== Defined in Resources, Unused in Source ===")
        sections.extend(f"  {k}" for k in report.unused_in_source)

    return "\n".join(sections)


# === CLI ===


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit Android res/values*/strings.xml for localization gaps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/localization_audit.py --res app/src/main/res
  python scripts/localization_audit.py --res ./res --source ./src
  python scripts/localization_audit.py --res ./res --locale es --strict
  python scripts/localization_audit.py --res ./res --json
        """,
    )
    parser.add_argument(
        "--res",
        required=True,
        type=Path,
        metavar="DIR",
        help="Path to the res/ directory (contains values/, values-es/, ...).",
    )
    parser.add_argument(
        "--source",
        type=Path,
        metavar="DIR",
        help="Source root to cross-reference R.string/getString/stringResource usage.",
    )
    parser.add_argument(
        "--locale",
        metavar="CODE",
        help="Restrict the audit to a single locale code (e.g. es, fr, zh-rCN).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with status 2 if any findings are present.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output a structured JSON report.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="List every missing key, mismatch, and cross-reference finding.",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    res_dir: Path = args.res
    if not res_dir.is_dir():
        print(f"Error: res directory not found: {res_dir}", file=sys.stderr)
        sys.exit(1)

    source_dir: Path | None = args.source
    if source_dir is not None and not source_dir.is_dir():
        print(f"Error: source directory not found: {source_dir}", file=sys.stderr)
        sys.exit(1)

    try:
        auditor = LocalizationAuditor(
            res_dir=res_dir, source_dir=source_dir, locale_filter=args.locale
        )
        report = auditor.audit()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.verbose:
        print(_format_verbose(report))
    else:
        print(_format_default(report))

    if args.strict and report.has_findings():
        sys.exit(2)


if __name__ == "__main__":
    main()
