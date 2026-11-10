"""Unit tests for the Android string-resource localization audit.

Pure file analysis — no device or subprocess. Each test builds a temporary
``res/`` tree under ``tmp_path`` with ``values/`` and ``values-*/`` strings.xml
and asserts missing-key, placeholder-mismatch, and source cross-reference
detection.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import localization_audit
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


def _labels(report) -> list[str]:
    """The display labels of the sets a report audited, in its order."""
    return [audited.label for audited in report.locales]


# === locale_from_dir ===


def test_locale_from_dir_default():
    assert locale_from_dir("values") == ""


def test_locale_from_dir_simple_and_region():
    assert locale_from_dir("values-es") == "es"
    assert locale_from_dir("values-zh-rCN") == "zh-rCN"


# L6: only a LOCALE qualifier makes a values-* directory a translation. The
# grammar is Android's (aapt2): optional mcc/mnc, then the language (two or
# three letters, any case, "car" being UI mode) with an optional `r` + two-letter
# region, or a BCP 47 `b+` tag; any other qualifier may follow. A directory whose
# first qualifier past mcc/mnc is not a locale has none -- aapt2 37.0.0 refuses
# `values-night-fr` and `values-es-r419` as invalid configurations.


@pytest.mark.parametrize(
    ("dir_name", "locale"),
    [
        ("values-fr", "fr"),
        ("values-fr-rCA", "fr-rCA"),
        ("values-b+sr+Latn", "b+sr+Latn"),
        ("values-b+es+419", "b+es+419"),
        ("values-fr-night", "fr"),
        ("values-en-rUS-v21", "en-rUS"),
        ("values-fr-ldrtl", "fr"),
        ("values-mcc310-fr", "fr"),
        ("values-mcc310-mnc004-en", "en"),
        ("values-fil", "fil"),
    ],
)
def test_locale_from_dir_reads_the_locale_qualifier(dir_name, locale):
    assert locale_from_dir(dir_name) == locale


@pytest.mark.parametrize(
    "dir_name",
    [
        "values-night",
        "values-sw600dp",
        "values-v31",
        "values-land",
        "values-port",
        "values-car",
        "values-ldrtl",
        "values-hdpi",
        "values-w820dp",
        "values-mcc310",
        "values-mcc310-mnc004",
        "values-night-fr",
    ],
)
def test_locale_from_dir_has_no_locale_for_other_qualifiers(dir_name):
    assert locale_from_dir(dir_name) is None


LOCALIZATION_RES = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "scaffold"
    / "localization"
    / "app"
    / "src"
    / "main"
    / "res"
)

NOT_LOCALES = ["values-land", "values-night", "values-sw600dp", "values-v31"]


def test_a_partial_override_by_a_non_locale_qualifier_is_not_a_translation():
    """L6: values-night, -sw600dp, -v31 and -land override a key or two; none is a locale."""
    report = LocalizationAuditor(LOCALIZATION_RES).audit()

    assert _labels(report) == ["ar", "b+sr+Latn", "fr", "fr-rCA"]
    assert report.missing_keys == [], [f"{m.locale}:{m.key}" for m in report.missing_keys]
    assert report.placeholder_mismatches == []
    assert not report.has_findings()

    skipped = {entry["directory"]: entry["reason"] for entry in report.to_dict()["skipped"]}
    assert sorted(skipped) == sorted([*NOT_LOCALES, "values-fr-night"])
    for directory in NOT_LOCALES:
        qualifiers = directory.removeprefix("values-")
        assert "no locale qualifier" in skipped[directory], skipped[directory]
        assert qualifiers in skipped[directory], skipped[directory]
    # French in night mode is French: audited as fr, from values-fr.
    assert "fr" in skipped["values-fr-night"] and "values-fr" in skipped["values-fr-night"]


def test_strict_on_a_tree_with_qualified_overrides_finds_nothing(capsys):
    """The CLI: --strict exited 2 on four fake locales; --json lists what it skipped."""
    localization_audit.main(["--res", str(LOCALIZATION_RES), "--strict", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert [entry["label"] for entry in report["locales"]] == ["ar", "b+sr+Latn", "fr", "fr-rCA"]
    assert report["missing_keys"] == []
    assert {e["directory"] for e in report["skipped"]} >= set(NOT_LOCALES)

    localization_audit.main(["--res", str(LOCALIZATION_RES)])
    out = capsys.readouterr().out
    assert "No issues found." in out
    assert "values-night" in out, out


def test_a_locale_only_in_a_qualified_directory_is_still_audited(tmp_path: Path):
    """values-de-land with no values-de: German in landscape, audited from what it has."""
    res = _make_res(
        tmp_path,
        {
            "values": '<string name="a">A</string><string name="b">B</string>',
            "values-de-land": '<string name="a">A (de)</string>',
        },
    )
    report = LocalizationAuditor(res).audit()
    assert _labels(report) == ["de (land)"]
    assert [(m.locale, m.qualifiers, m.key) for m in report.missing_keys] == [("de", "land", "b")]


# Overrides are validated, and configurations are never merged. The tree is a
# project's res/: tests/fixtures/scaffold/localization-overrides.
OVERRIDES_RES = (
    LOCALIZATION_RES.parents[4] / "localization-overrides" / "app" / "src" / "main" / "res"
)


def test_an_override_s_strings_are_checked_like_a_translation_s():
    """values-fr-night and values-night each drop a placeholder: both are findings.

    Neither must hold every key (an override supplies some), so neither has a
    missing-key finding; what each does supply is checked against values/.
    """
    report = LocalizationAuditor(OVERRIDES_RES).audit()
    mismatches = [(m.label, m.key) for m in report.placeholder_mismatches]
    assert mismatches == [("default (night)", "status"), ("fr (night)", "greeting")]
    assert not [m for m in report.missing_keys if m.qualifiers == "night"]

    reasons = {entry["directory"]: entry["reason"] for entry in report.to_dict()["skipped"]}
    assert sorted(reasons) == ["values-fr-night", "values-night"]
    for reason in reasons.values():
        assert "checked against values/" in reason, reason
        assert "not a translation" not in reason and "ignored" not in reason, reason


def test_a_locale_in_two_configurations_is_audited_per_configuration():
    """values-de-land and values-de-port each lack a different key: two gaps, not none.

    Android picks ONE configuration and falls back to values/; it never takes
    landscape's strings in portrait. A union of the two read as complete German.
    """
    report = LocalizationAuditor(OVERRIDES_RES).audit()
    assert _labels(report) == ["de (land)", "de (port)", "fr"]
    assert [(m.label, m.key) for m in report.missing_keys] == [
        ("de (land)", "farewell"),
        ("de (port)", "theme_label"),
    ]

    only_de = LocalizationAuditor(OVERRIDES_RES, locale_filter="de").audit()
    assert _labels(only_de) == ["de (land)", "de (port)"]
    assert len(only_de.missing_keys) == 2


def _audit_json(capsys, *argv: str) -> dict:
    localization_audit.main(["--res", str(OVERRIDES_RES), "--json", *argv])
    return json.loads(capsys.readouterr().out)


def test_a_locale_code_from_the_json_selects_its_sets_again(capsys):
    """The loop: every `locale` the JSON names is a `--locale` that selects exactly those sets.

    Each audited set and each finding carries the locale's code, its other
    qualifiers and the display label. On main the code was the directory
    suffix and could be passed back; a label such as "de (land)" cannot.
    """
    report = _audit_json(capsys)
    assert {"locale": "de", "qualifiers": "land", "label": "de (land)"} in report["locales"]
    assert {"locale": "fr", "qualifiers": None, "label": "fr"} in report["locales"]
    missing = next(m for m in report["missing_keys"] if m["label"] == "de (port)")
    assert (missing["locale"], missing["qualifiers"]) == ("de", "port")
    night = next(m for m in report["placeholder_mismatches"] if m["label"] == "default (night)")
    assert (night["locale"], night["qualifiers"]) == ("default", "night")

    codes = {entry["locale"] for entry in report["locales"]}
    codes |= {m["locale"] for m in report["missing_keys"] + report["placeholder_mismatches"]}
    assert codes == {"de", "default", "fr"}
    for code in sorted(codes):
        again = _audit_json(capsys, "--locale", code)
        named = again["locales"] + again["missing_keys"] + again["placeholder_mismatches"]
        assert named, f"--locale {code} selected nothing"
        assert {entry["locale"] for entry in named} == {code}, (code, named)
    german = _audit_json(capsys, "--locale", "de")
    assert [entry["label"] for entry in german["locales"]] == ["de (land)", "de (port)"]


def test_the_locale_refusal_lists_the_codes_it_would_accept():
    with pytest.raises(cli.Failure) as excinfo:
        LocalizationAuditor(OVERRIDES_RES, locale_filter="es").audit()
    said = f"{excinfo.value} {excinfo.value.remedy}"
    assert "de, default, fr" in said, said
    assert "(land)" not in said and "(port)" not in said, said


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
    assert _labels(report) == ["es"]
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


# A <plurals> is read through getQuantityString(id, count, args...): the
# language picks ONE item by its own categories (Serbian one/few/other, Arabic
# zero/one/two/few/many/other) and that item is formatted with the args the
# call supplies. So items are compared one by one, never joined.


def test_a_language_with_more_plural_categories_is_not_a_mismatch():
    """values-b+sr+Latn has a few item and values-ar all six; English has one/other.

    Joined, Serbian read ['%d','%d','%d'] against ['%d','%d'] -- a finding that
    is no defect. Each item carries the %d the call supplies.
    """
    report = LocalizationAuditor(LOCALIZATION_RES).audit()
    assert report.placeholder_mismatches == [], [
        (m.label, m.key, m.locale_placeholders) for m in report.placeholder_mismatches
    ]


ITEMS_EN = (
    '<plurals name="items"><item quantity="one">%d item</item>'
    '<item quantity="other">%d items</item></plurals>'
)


def _plural(*items: tuple[str, str]) -> str:
    body = "".join(f'<item quantity="{quantity}">{text}</item>' for quantity, text in items)
    return f'<plurals name="items">{body}</plurals>'


def test_a_plural_item_that_drops_a_placeholder_every_default_item_carries_is_reported(
    tmp_path: Path,
):
    """Serbian few without %d prints no number for 2-4 items: still a finding."""
    sr = _plural(("one", "%d stavka"), ("few", "nekoliko stavki"), ("other", "%d stavki"))
    res = _make_res(tmp_path, {"values": ITEMS_EN, "values-b+sr+Latn": sr})
    report = LocalizationAuditor(res).audit()
    found = [
        (m.label, m.key, m.quantity, m.default_placeholders, m.locale_placeholders)
        for m in report.placeholder_mismatches
    ]
    assert found == [("b+sr+Latn", "items", "few", ["%1$d"], [])]


def test_a_plural_item_with_a_placeholder_the_call_does_not_supply_is_reported(tmp_path: Path):
    """%3$s in one item: the default's items take two arguments, so it has nothing to format."""
    default = _plural(("one", "%1$d file in %2$s"), ("other", "%1$d files in %2$s"))
    ar = _plural(
        ("zero", "%1$d in %2$s"),
        ("one", "%1$d in %2$s"),
        ("two", "%1$d in %2$s"),
        ("few", "%1$d in %2$s %3$s"),
        ("many", "%1$d in %2$s"),
        ("other", "%1$d in %2$s"),
    )
    res = _make_res(tmp_path, {"values": default, "values-ar": ar})
    report = LocalizationAuditor(res).audit()
    found = [(m.quantity, m.locale_placeholders) for m in report.placeholder_mismatches]
    assert found == [("few", ["%1$d", "%2$s", "%3$s"])]


def test_an_other_item_is_held_to_the_default_other_item(tmp_path: Path):
    """English one may say "One item" (it is only ever 1); every language's other must count.

    The default's one carries no %d, so no item is held to it by every default
    item -- but other is the category every language has, for the numbers no
    other category takes, and a translation's other without %d prints none.
    """
    default = _plural(("one", "One item"), ("other", "%d items"))
    fr_ok = _plural(("one", "Un élément"), ("many", "%d éléments"), ("other", "%d éléments"))
    de_bad = _plural(("one", "Ein Element"), ("other", "Elemente"))
    res = _make_res(tmp_path, {"values": default, "values-fr": fr_ok, "values-de": de_bad})
    report = LocalizationAuditor(res).audit()
    found = [(m.label, m.quantity, m.locale_placeholders) for m in report.placeholder_mismatches]
    assert found == [("de", "other", [])]


def test_a_plural_item_that_reads_more_arguments_than_the_call_supplies_is_reported(
    tmp_path: Path,
):
    """%d %d reads two arguments; the default's items read one. The format call throws."""
    de = _plural(("one", "%d Element"), ("other", "%d %d Elemente"))
    res = _make_res(tmp_path, {"values": ITEMS_EN, "values-de": de})
    found = [
        (m.quantity, m.default_placeholders, m.locale_placeholders)
        for m in LocalizationAuditor(res).audit().placeholder_mismatches
    ]
    assert found == [("other", ["%1$d"], ["%1$d", "%2$d"])]


def test_d_and_1_d_read_the_same_argument(tmp_path: Path):
    """%d is the first argument, as %1$d is: swapping the two forms is no mismatch."""
    default = _plural(("one", "%d item"), ("other", "%1$d items"))
    de = _plural(("one", "%1$d Element"), ("other", "%d Elemente"))
    res = _make_res(tmp_path, {"values": default, "values-de": de})
    assert LocalizationAuditor(res).audit().placeholder_mismatches == []


def test_a_percent_with_a_width_reads_no_argument(tmp_path: Path):
    """%5% prints a padded percent sign: it does not shift %d off argument 1."""
    default = _plural(("one", "%5% %d item"), ("other", "%5% %d items"))
    de = _plural(("one", "%d Element %5%"), ("other", "%d Elemente %5%"))
    res = _make_res(tmp_path, {"values": default, "values-de": de})
    assert LocalizationAuditor(res).audit().placeholder_mismatches == []


def test_a_plural_with_no_other_item_is_held_to_the_default_other(tmp_path: Path):
    """Android falls back to other when a category has no item; none there is a finding."""
    res = _make_res(
        tmp_path,
        {
            "values": ITEMS_EN,
            "values-de": _plural(("one", "%d Element")),
            "values-fr": '<plurals name="items"></plurals>',
        },
    )
    found = [
        (m.label, m.quantity, m.locale_placeholders)
        for m in LocalizationAuditor(res).audit().placeholder_mismatches
    ]
    assert found == [("de", "other", []), ("fr", "other", [])]


def test_every_item_of_a_repeated_quantity_is_checked(tmp_path: Path):
    """Two one items: the first's %s must not be lost behind the second's %d."""
    de = _plural(("one", "%s Element"), ("one", "%d Element"), ("other", "%d Elemente"))
    res = _make_res(tmp_path, {"values": ITEMS_EN, "values-de": de})
    found = [
        (m.quantity, m.locale_placeholders)
        for m in LocalizationAuditor(res).audit().placeholder_mismatches
    ]
    assert found == [("one", ["%1$s"])]


def test_a_plural_mismatch_names_its_item_in_text_and_json(tmp_path: Path, capsys):
    sr = _plural(("one", "%d stavka"), ("few", "stavke"), ("other", "%d stavki"))
    res = _make_res(tmp_path, {"values": ITEMS_EN, "values-b+sr+Latn": sr})
    localization_audit.main(["--res", str(res), "--verbose"])
    assert "[b+sr+Latn] items (few)" in capsys.readouterr().out
    localization_audit.main(["--res", str(res), "--json"])
    (mismatch,) = json.loads(capsys.readouterr().out)["placeholder_mismatches"]
    assert mismatch["quantity"] == "few"


def test_a_string_mismatch_has_no_quantity(tmp_path: Path):
    res = _make_res(
        tmp_path,
        {"values": '<string name="c">%d new</string>', "values-es": '<string name="c">x</string>'},
    )
    (mismatch,) = LocalizationAuditor(res).audit().placeholder_mismatches
    assert mismatch.quantity is None


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
    assert _labels(report) == ["es"]
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

    with pytest.raises(
        cli.Failure, match=r"no values-fr/ or values-fr-\*/ strings\.xml under"
    ) as excinfo:
        LocalizationAuditor(res_dir=res, locale_filter="fr").audit()
    assert "locale codes found: es" in str(excinfo.value)


def test_a_default_with_no_keys_is_still_an_answer(tmp_path: Path):
    """The line N11 draws: a `values/strings.xml` that was read is audited, even empty.

    Zero keys there is a fact about the app, reported as `total_keys: 0` --
    unlike a tree with no default at all, where nothing was read.
    """
    res = _make_res(tmp_path, {"values": "", "values-es": ""})

    report = LocalizationAuditor(res_dir=res).audit()

    assert (report.total_keys, _labels(report)) == (0, ["es"])


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
