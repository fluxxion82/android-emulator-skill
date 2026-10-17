"""Unit tests for the Android string-resource localization audit.

Pure file analysis — no device or subprocess. Each test builds a temporary
``res/`` tree under ``tmp_path`` with ``values/`` and ``values-*/`` strings.xml
and asserts missing-key, placeholder-mismatch, and source cross-reference
detection.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fake_toolchain import SCRIPTS, assert_refusal
from localization_audit import (
    LocalizationAuditor,
    _extract_placeholders,
    locale_from_dir,
    parse_strings_xml,
    scan_source_refs,
)

from common import cli


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_res(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a res/ tree. ``files`` maps "values-es" -> strings.xml body."""
    res = tmp_path / "res"
    for dir_name, body in files.items():
        _write(res / dir_name / "strings.xml", f"<resources>\n{body}\n</resources>")
    return res


# === locale_from_dir ===


def test_locale_from_dir_default():
    assert locale_from_dir("values") == ""


def test_locale_from_dir_simple_and_region():
    assert locale_from_dir("values-es") == "es"
    assert locale_from_dir("values-zh-rCN") == "zh-rCN"


# === placeholder extraction ===


def test_extract_placeholders_basic_and_positional():
    assert _extract_placeholders("Hello %s, you have %d items") == ["%s", "%d"]
    assert _extract_placeholders("%2$s before %1$d") == ["%2$s", "%1$d"]


def test_extract_placeholders_ignores_literal_percent():
    assert _extract_placeholders("100%% complete") == []


# === parse_strings_xml ===


def test_parse_strings_all_kinds(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": (
                '<string name="hello">Hello</string>'
                '<plurals name="dogs"><item quantity="one">%d dog</item>'
                '<item quantity="other">%d dogs</item></plurals>'
                '<string-array name="days"><item>Mon</item><item>Tue</item></string-array>'
            )
        },
    )
    entries = parse_strings_xml(res / "values" / "strings.xml")
    assert entries["hello"]["kind"] == "string"
    assert entries["dogs"]["kind"] == "plurals"
    assert entries["days"]["kind"] == "string-array"
    # plurals/array values flatten all items for placeholder comparison
    assert "%d" in entries["dogs"]["value"]
    assert "Mon" in entries["days"]["value"] and "Tue" in entries["days"]["value"]


def test_parse_strings_skips_non_translatable(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": (
                '<string name="app_name" translatable="false">MyApp</string>'
                '<string name="greeting">Hi</string>'
            )
        },
    )
    entries = parse_strings_xml(res / "values" / "strings.xml")
    assert "app_name" not in entries
    assert "greeting" in entries


def test_parse_strings_malformed_raises(tmp_path: Path):
    bad = tmp_path / "values" / "strings.xml"
    _write(bad, "<resources><string name='x'>oops</resources>")
    with pytest.raises(ValueError, match="Invalid XML"):
        parse_strings_xml(bad)


# === missing-key detection ===


def test_detects_missing_keys(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": (
                '<string name="hello">Hello</string>'
                '<string name="bye">Goodbye</string>'
                '<string name="welcome">Welcome</string>'
            ),
            "values-es": ('<string name="hello">Hola</string>' '<string name="bye">Adios</string>'),
        },
    )
    report = LocalizationAuditor(res_dir=res).audit()
    assert report.total_keys == 3
    assert report.locales == ["es"]
    missing = {(m.key, m.locale) for m in report.missing_keys}
    assert missing == {("welcome", "es")}


def test_no_missing_when_fully_translated(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="hello">Hello</string>',
            "values-fr": '<string name="hello">Bonjour</string>',
        },
    )
    report = LocalizationAuditor(res_dir=res).audit()
    assert report.missing_keys == []
    assert not report.has_findings()


# === placeholder-mismatch detection ===


def test_detects_placeholder_mismatch(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="count">You have %d new messages</string>',
            # Translation dropped the %d placeholder
            "values-es": '<string name="count">Tienes mensajes nuevos</string>',
        },
    )
    report = LocalizationAuditor(res_dir=res).audit()
    assert len(report.placeholder_mismatches) == 1
    m = report.placeholder_mismatches[0]
    assert m.key == "count"
    assert m.locale == "es"
    assert m.default_placeholders == ["%d"]
    assert m.locale_placeholders == []


def test_positional_placeholder_match_is_ok(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="msg">%1$s sent %2$d files</string>',
            # Reordered positional args still use the same specifier set
            "values-fr": '<string name="msg">%2$d fichiers de %1$s</string>',
        },
    )
    report = LocalizationAuditor(res_dir=res).audit()
    assert report.placeholder_mismatches == []


def test_missing_key_not_double_reported_as_mismatch(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="count">%d items</string>',
            "values-es": '<string name="other">otro</string>',
        },
    )
    report = LocalizationAuditor(res_dir=res).audit()
    # "count" is missing in es; it must not also show up as a placeholder mismatch
    assert {m.key for m in report.missing_keys} == {"count"}
    assert report.placeholder_mismatches == []


# === locale filter ===


def test_locale_filter_restricts_output(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="a">A</string><string name="b">B</string>',
            "values-es": '<string name="a">A</string>',
            "values-fr": '<string name="a">A</string>',
        },
    )
    report = LocalizationAuditor(res_dir=res, locale_filter="es").audit()
    assert report.locales == ["es"]
    assert {m.locale for m in report.missing_keys} == {"es"}


# === nothing to audit (N11) ===
#
# Every check compares a translation with the default values/strings.xml. An
# audit that has no default, or no translation for the --locale it was asked
# about, inspects nothing -- and used to say "No issues found." at exit 0.
# The empty --res itself is pinned end to end in test_localization_audit_empty.py.


def test_translations_without_a_default_are_nothing_to_audit(tmp_path: Path):
    """A `values-es/` with no `values/` beside it: nothing to compare it with."""
    res = _make_res(tmp_path, {"values-es": '<string name="hello">Hola</string>'})

    with pytest.raises(cli.Failure, match=r"no values/strings\.xml under") as excinfo:
        LocalizationAuditor(res_dir=res).audit()
    assert "translation locales found: es" in str(excinfo.value)


def test_a_locale_that_is_not_there_is_nothing_to_audit(tmp_path: Path):
    """`--locale fr` against a tree with only `values-es/` audited no locale, clean."""
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="hello">Hello</string>',
            "values-es": '<string name="other">Hola</string>',
        },
    )

    with pytest.raises(cli.Failure, match=r"no values-fr/strings\.xml under") as excinfo:
        LocalizationAuditor(res_dir=res, locale_filter="fr").audit()
    assert "translation locales found: es" in str(excinfo.value)


def test_a_default_with_no_keys_is_still_an_answer(tmp_path: Path):
    """The line N11 draws: a `values/strings.xml` that was read is audited, even empty.

    Zero keys there is a fact about the app, reported as `total_keys: 0` --
    unlike a tree with no default at all, where nothing was read.
    """
    res = _make_res(tmp_path, {"values": "", "values-es": ""})

    report = LocalizationAuditor(res_dir=res).audit()

    assert (report.total_keys, report.locales) == (0, ["es"])


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "localization_audit.py"), *argv],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("case", ["missing-res", "missing-source", "malformed-xml"])
def test_every_refusal_reaches_the_boundary(tmp_path: Path, case: str, json_mode: bool):
    """The refusals N11 sits beside, in the same shape.

    Each printed `Error:` to stderr and exited 1, so under --json stdout was
    empty -- the shape the boundary exists to end.
    """
    res = _make_res(tmp_path, {"values": '<string name="a">A</string>'})
    argv = ["--res", str(res)]
    if case == "missing-res":
        argv = ["--res", str(tmp_path / "absent")]
        naming = "res directory not found"
    elif case == "missing-source":
        argv += ["--source", str(tmp_path / "absent")]
        naming = "source directory not found"
    else:
        _write(res / "values-es" / "strings.xml", "<resources><string name='a'>A</resources>")
        naming = "invalid xml"

    result = _cli(*argv, *(["--json"] if json_mode else []))

    assert_refusal(result, json_mode=json_mode, naming=(naming,), status=1)


# === source cross-reference ===


def test_source_cross_reference_undefined_and_unused(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {
            "values": (
                '<string name="used_key">Used</string>' '<string name="orphan_key">Orphan</string>'
            )
        },
    )
    src = tmp_path / "src"
    _write(
        src / "Main.kt",
        "val a = getString(R.string.used_key)\n" "val b = stringResource(R.string.ghost_key)\n",
    )
    report = LocalizationAuditor(res_dir=res, source_dir=src).audit()
    # ghost_key referenced in code but not defined
    assert report.undefined_in_resources == ["ghost_key"]
    # orphan_key defined but never referenced
    assert report.unused_in_source == ["orphan_key"]


def test_scan_source_refs_matches_all_forms(tmp_path: Path):
    src = tmp_path / "src"
    _write(
        src / "A.java",
        "getString(R.string.alpha); R.plurals.beta; R.array.gamma;",
    )
    _write(src / "layout.xml", '<TextView android:text="@string/delta"/>')
    refs = scan_source_refs(src)
    assert refs == {"alpha", "beta", "gamma", "delta"}
