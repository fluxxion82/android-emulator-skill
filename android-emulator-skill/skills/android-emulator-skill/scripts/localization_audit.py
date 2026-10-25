#!/usr/bin/env python3
"""
Android String Resource Localization Audit

Parses ``res/values*/strings.xml`` across locales and reports localization
gaps. This is pure file analysis — no device or emulator is required (and so
there is no ``--serial`` flag; the audit operates on a ``res/`` tree on disk).

Android-native (NOT an iOS .xcstrings port):
  - Locale resolution follows Android resource-qualifier rules: ``values/`` is
    the default (typically English), and ``values-<locale>/`` (e.g.
    ``values-es/``, ``values-fr/``, ``values-zh-rCN/``, ``values-b+sr+Latn/``)
    hold translations. A directory whose qualifiers name no locale
    (``values-night/``, ``values-sw600dp/``, ``values-v31/``), or a locale's
    qualified directory beside its plain one (``values-fr-night/`` beside
    ``values-fr/``), is an override: held to no key, but every string it
    supplies is checked against ``values/``. Overrides are listed under
    ``skipped`` with the reason. A locale with no plain directory is audited
    per qualified directory (``de (land)``), never as their union.
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

from common import cli
from common.env_config import env_int

# Tunable defaults (override via the ANDROID_EMU_ prefix). Caps keep default
# output token-efficient; --verbose/--json always emit the full detail.
MAX_LISTED_KEYS = env_int("ANDROID_EMU_LOCALIZATION_MAX_LISTED_KEYS", 10, min_value=1)

# The usual wrong --res is one level off: app/src/main instead of app/src/main/res.
RES_REMEDY = "Pass the res/ directory that holds values/strings.xml, e.g. --res app/src/main/res."


# === TYPES ===


# Every audited set and every finding names its set three ways: ``locale``, the
# code ``--locale`` takes (``fr``, ``fr-rCA``, ``b+sr+Latn``, or ``default`` for
# an override of the default language); ``qualifiers``, the directory's other
# qualifiers (``land``, ``night``) or None; and ``label``, how text output shows
# it (``fr``, ``de (land)``, ``default (night)``).
DEFAULT_LANGUAGE = "default"


@dataclass
class AuditedSet:
    """A translation the audit held to every default key."""

    locale: str
    qualifiers: str | None
    label: str


@dataclass
class MissingKey:
    """A key present in the default locale but absent from a translation."""

    key: str
    locale: str
    qualifiers: str | None
    label: str
    kind: str  # "string", "plurals", or "string-array"


@dataclass
class PlaceholderMismatch:
    """A key whose format specifiers differ between default and a translation or override."""

    key: str
    locale: str
    qualifiers: str | None
    label: str
    default_placeholders: list[str]
    locale_placeholders: list[str]


@dataclass
class SkippedDir:
    """An override: a ``values-*`` directory not held to every key (its strings are checked)."""

    directory: str
    reason: str


@dataclass
class AuditReport:
    """Structured result of a full string-resource audit."""

    res_dir: str
    default_locale: str
    total_keys: int
    locales: list[AuditedSet]
    missing_keys: list[MissingKey] = field(default_factory=list)
    placeholder_mismatches: list[PlaceholderMismatch] = field(default_factory=list)
    undefined_in_resources: list[str] = field(default_factory=list)  # used in code, undefined
    unused_in_source: list[str] = field(default_factory=list)  # defined, never referenced
    skipped: list[SkippedDir] = field(default_factory=list)  # overrides, held to no key

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


# Android's resource-qualifier grammar for the locale, as aapt2 parses it:
# optional MCC/MNC first (`mcc310`, `mnc004`), then the locale, then any other
# qualifiers in their fixed order. The locale is a two- or three-letter
# language, any case, optionally followed by `r` and a two-letter region
# (`fr`, `fr-rCA`), or a BCP 47 tag behind `b+` (`b+sr+Latn`, `b+es+419`).
# "car" is three letters and the UI-mode qualifier, so it is not a language.
# A locale after another qualifier is not a locale (aapt2 37.0.0 refuses
# `values-night-fr` as an invalid configuration), nor is `r419` a region.
_MCC_MNC_RE = re.compile(r"^(?:mcc|mnc)\d+$", re.IGNORECASE)
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}$", re.IGNORECASE)
_REGION_RE = re.compile(r"^r[a-z]{2}$", re.IGNORECASE)
_BCP47_RE = re.compile(r"^b\+[a-z]{2,3}(?:\+[a-z0-9]{2,8})*$", re.IGNORECASE)
_NOT_A_LANGUAGE = frozenset({"car"})


def split_values_dir(dir_name: str) -> tuple[str | None, list[str]]:
    """Split a ``values*`` directory name into its locale and its other qualifiers.

    ``values`` -> ``("", [])``, the default locale. ``values-fr-rCA-night`` ->
    ``("fr-rCA", ["night"])``. ``values-night`` -> ``(None, ["night"])``: no
    locale. MCC/MNC qualifiers count among the others.
    """
    if dir_name == "values":
        return "", []
    parts = dir_name.removeprefix("values-").split("-")
    at = 0
    while at < len(parts) and _MCC_MNC_RE.match(parts[at]):
        at += 1
    others = parts[:at]
    if at < len(parts) and _BCP47_RE.match(parts[at]):
        return parts[at], others + parts[at + 1 :]
    if (
        at < len(parts)
        and _LANGUAGE_RE.match(parts[at])
        and parts[at].lower() not in _NOT_A_LANGUAGE
    ):
        end = at + 1
        if end < len(parts) and _REGION_RE.match(parts[end]):
            end += 1
        return "-".join(parts[at:end]), others + parts[end:]
    return None, parts


def locale_from_dir(dir_name: str) -> str | None:
    """
    Derive a locale code from a ``values*`` resource directory name.

    ``values`` -> "" (the default locale). ``values-es`` -> "es".
    ``values-zh-rCN`` -> "zh-rCN". ``values-b+sr+Latn`` -> "b+sr+Latn".
    ``values-fr-night`` -> "fr": French, in night mode. A directory whose
    qualifiers hold no locale (``values-night``, ``values-sw600dp``,
    ``values-v31``, ``values-land``) -> None: it is not a translation.
    """
    return split_values_dir(dir_name)[0]


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


@dataclass
class ResourceSet:
    """One ``values*/strings.xml`` the audit compares with the default.

    Attributes:
        locale: The locale code ``--locale`` selects it by: the locale
            qualifier, or ``default`` for an override of the default language.
        qualifiers: Its directory's other qualifiers (``land``), or None.
        entries: Its parsed keys (:func:`parse_strings_xml`).
        complete: Held to every default key (a translation), or not (an override).
    """

    locale: str
    qualifiers: str | None
    entries: dict[str, dict[str, Any]]
    complete: bool

    @property
    def label(self) -> str:
        """``fr``; ``de (land)`` for a qualified set; ``default (night)``."""
        return f"{self.locale} ({self.qualifiers})" if self.qualifiers else self.locale


def load_resources(
    res_dir: Path,
) -> tuple[dict[str, dict[str, Any]] | None, list[ResourceSet], list[SkippedDir]]:
    """
    Load every ``values*/strings.xml`` under ``res_dir`` as the audit reads it.

    Android picks ONE configuration for a string and falls back to
    ``values/``; it never merges two qualified directories. So:

    - ``values/`` is the default; ``values-fr/`` (a locale and nothing else)
      is a translation, held to every default key.
    - ``values-fr-night/`` beside ``values-fr/`` is an override of French, and
      ``values-night/`` (no locale, L6) an override of the default language:
      held to no key, but every string they supply is checked.
    - ``values-de-land/`` with no ``values-de/`` is German in landscape, a
      translation of its own (``de (land)``): ``values-de-port/`` beside it is
      another, each held to every default key. Their union read as complete
      German when each lacked a different key.

    Directories without a ``strings.xml`` are ignored.

    Returns:
        ``(default, sets, skipped)``: the default's entries (None when there is
        no ``values/strings.xml``), every other set, and one
        :class:`SkippedDir` per override, saying what is checked.

    Raises:
        ValueError: A ``strings.xml`` is malformed.
    """
    default: dict[str, dict[str, Any]] | None = None
    found: list[tuple[str, list[str], Path]] = []
    for child in sorted(res_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name != "values" and not child.name.startswith("values-"):
            continue
        strings_file = child / "strings.xml"
        if not strings_file.is_file():
            continue
        locale, others = split_values_dir(child.name)
        if locale == "":
            default = parse_strings_xml(strings_file)
        else:
            found.append((locale or "", others, strings_file))

    plain = {locale: path for locale, others, path in found if locale and not others}
    sets: list[ResourceSet] = []
    skipped: list[SkippedDir] = []
    for locale, others, path in found:
        entries = parse_strings_xml(path)
        code = locale or DEFAULT_LANGUAGE
        qualifiers = "-".join(others) or None
        if not others:
            sets.append(ResourceSet(code, None, entries, complete=True))
        elif locale and locale not in plain:
            sets.append(ResourceSet(code, qualifiers, entries, complete=True))
        else:
            sets.append(ResourceSet(code, qualifiers, entries, complete=False))
            of = (
                f"{locale} with qualifiers {qualifiers}: an override of {plain[locale].parent.name}"
                if locale
                else f"no locale qualifier in {qualifiers}: an override of the default language"
            )
            skipped.append(
                SkippedDir(
                    directory=path.parent.name,
                    reason=(
                        f"{of}, held to no key; the strings it supplies are checked "
                        f"against values/"
                    ),
                )
            )
    return default, sets, skipped


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
        translations: list[ResourceSet],
    ) -> list[MissingKey]:
        """Find default keys absent from each translation (never from an override)."""
        missing: list[MissingKey] = []
        for resource in translations:
            for key, entry in default.items():
                if key not in resource.entries:
                    missing.append(
                        MissingKey(
                            key=key,
                            locale=resource.locale,
                            qualifiers=resource.qualifiers,
                            label=resource.label,
                            kind=entry["kind"],
                        )
                    )
        return sorted(missing, key=lambda m: (m.label, m.key))

    def _check_placeholders(
        self,
        default: dict[str, dict[str, Any]],
        resources: list[ResourceSet],
    ) -> list[PlaceholderMismatch]:
        """Compare format specifiers between default and every string a set supplies."""
        mismatches: list[PlaceholderMismatch] = []
        for resource in resources:
            for key, entry in default.items():
                if key not in resource.entries:
                    continue  # a translation's gap is reported as missing
                default_ph = sorted(_extract_placeholders(entry["value"]))
                locale_ph = sorted(_extract_placeholders(resource.entries[key]["value"]))
                if default_ph != locale_ph:
                    mismatches.append(
                        PlaceholderMismatch(
                            key=key,
                            locale=resource.locale,
                            qualifiers=resource.qualifiers,
                            label=resource.label,
                            default_placeholders=default_ph,
                            locale_placeholders=locale_ph,
                        )
                    )
        return sorted(mismatches, key=lambda m: (m.label, m.key))

    def audit(self) -> AuditReport:
        """Run the full audit and return a structured report.

        Raises:
            ValueError: A ``strings.xml`` is malformed.
            cli.Failure: There is nothing to audit -- no default
                ``values/strings.xml`` under ``res_dir``, or no set for the
                ``--locale`` code asked about (a translation such as
                ``values-de/`` or ``values-de-land/``, or ``default`` for the
                default language's overrides). Every check compares a set with
                the default, so either way the audit would inspect nothing and
                report "No issues found." at exit 0 (N11).
        """
        default, resources, skipped = load_resources(self.res_dir)

        if default is None:
            found = ", ".join(sorted({r.locale for r in resources if r.complete})) or "none"
            cli.fail(
                f"nothing to audit: no values/strings.xml under {self.res_dir} "
                f"(translation locales found: {found})",
                RES_REMEDY,
            )
        codes = sorted({r.locale for r in resources})
        if self.locale_filter and self.locale_filter not in codes:
            choices = ", ".join(codes) or "none"
            cli.fail(
                f"nothing to audit: no values-{self.locale_filter}/ or "
                f"values-{self.locale_filter}-*/ strings.xml under {self.res_dir} "
                f"(locale codes found: {choices})",
                f"Pass --locale one of: {choices}; or omit it to audit them all.",
                details={"locale_codes": codes},
            )

        if self.locale_filter:
            resources = [r for r in resources if r.locale == self.locale_filter]
            skipped = [
                entry
                for entry in skipped
                if (split_values_dir(entry.directory)[0] or DEFAULT_LANGUAGE) == self.locale_filter
            ]
        translations = sorted((r for r in resources if r.complete), key=lambda r: r.label)

        report = AuditReport(
            res_dir=str(self.res_dir),
            default_locale="values",
            total_keys=len(default),
            locales=[AuditedSet(r.locale, r.qualifiers, r.label) for r in translations],
            skipped=skipped,
        )

        report.missing_keys = self._collect_missing(default, translations)
        report.placeholder_mismatches = self._check_placeholders(default, resources)

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
        missing_by_locale[m.label] = missing_by_locale.get(m.label, 0) + 1
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
    if report.skipped:
        names = [entry.directory for entry in report.skipped]
        shown = ", ".join(names[:MAX_LISTED_KEYS])
        more = f" and {len(names) - MAX_LISTED_KEYS} more" if len(names) > MAX_LISTED_KEYS else ""
        lines.append(f"Overrides (strings checked, held to no key): {shown}{more}.")
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
            if m.label != current:
                sections.append(f"\n[{m.label}]")
                current = m.label
            sections.append(f"  {m.kind:13s}  {m.key}")

    if report.placeholder_mismatches:
        sections.append("\n=== Placeholder Mismatches ===")
        for m in report.placeholder_mismatches:
            sections.append(
                f"  [{m.label}] {m.key}\n"
                f"    values: {m.default_placeholders}\n"
                f"    {m.label}: {m.locale_placeholders}"
            )

    if report.undefined_in_resources:
        sections.append("\n=== Referenced in Source, Undefined in Resources ===")
        sections.extend(f"  {k}" for k in report.undefined_in_resources)

    if report.unused_in_source:
        sections.append("\n=== Defined in Resources, Unused in Source ===")
        sections.extend(f"  {k}" for k in report.unused_in_source)

    if report.skipped:
        sections.append("\n=== Overrides (strings checked, held to no key) ===")
        sections.extend(f"  {entry.directory}: {entry.reason}" for entry in report.skipped)

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


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Refusals go through common.cli: printed here, they reached --json callers
    # as an empty stdout behind exit 1.
    res_dir: Path = args.res
    if not res_dir.is_dir():
        cli.fail(f"res directory not found: {res_dir}", RES_REMEDY)

    source_dir: Path | None = args.source
    if source_dir is not None and not source_dir.is_dir():
        cli.fail(
            f"source directory not found: {source_dir}",
            "Pass --source the root of the module's code (e.g. app/src/main/java), or omit it.",
        )

    try:
        auditor = LocalizationAuditor(
            res_dir=res_dir, source_dir=source_dir, locale_filter=args.locale
        )
        report = auditor.audit()
    except ValueError as exc:
        # parse_strings_xml's message names the file and the parser's complaint.
        cli.fail(str(exc), "Fix the XML the error names so that it parses, then re-run.")

    if args.json_output:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.verbose:
        print(_format_verbose(report))
    else:
        print(_format_default(report))

    if args.strict and report.has_findings():
        sys.exit(2)


if __name__ == "__main__":
    cli.run(main)
