"""Accessibility-audit tests, against recorded screens only.

Two things this file used to get wrong, both fixed here (T2).

1. Its sole input was ``SAMPLE_UI_HIERARCHY`` -- fifteen hand-written lines
   nobody recorded, describing a login screen that does not exist. Because
   ``_audit_node`` matches neither of the fixture policy's name rules, the file
   was outside the ratchet, so the imagined dump was never challenged. Every
   test now reads ``tests/fixtures/recorded/`` and says which dump it uses.

2. The imagined dump was shaped to make the audit look good. It carried a
   clickable ``android.widget.ImageView`` with no label, which is the one thing
   the ``critical`` check can fire on -- and no recorded screen has one. That is
   finding L3, pinned below with ``xfail(strict=True)`` rather than papered
   over: the check keys off the class NAME, and on Compose every unlabelled
   clickable node reports ``android.view.View``.

Dumps used:

- ``uiautomator_compose_default`` -- a Compose screen with no
  testTagsAsResourceId. Six unlabelled clickable nodes (4x android.view.View,
  an EditText, a CheckBox), every resource-id empty.
- ``uiautomator_compose_testtags`` -- the same screen with
  ``testTagsAsResourceId``, so the resource-id findings disappear.
- ``uiautomator_current_screen`` -- Settings, an AOSP-widget screen with real
  ImageViews.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from accessibility_audit import AccessibilityAuditor, _attr_bool

from common.device_utils import _xml_to_dict

# The density every unpinned test in this file runs at. 160dpi is Android's
# baseline, where 1px == 1dp.
BASELINE_DPI = 160

# A real xxhdpi device (the Pixel 4 XL profile records 560). The recorded
# Compose screen's controls pass the 48dp minimum at 160dpi and fail at 560 --
# the same pixels, a different verdict, which is the whole of S7.
XXHDPI = 560


@pytest.fixture(autouse=True)
def pinned_density(monkeypatch):
    """Keep the audit off any attached device.

    `_resolve_density` asks adb for the real density, so without this every
    test here silently reads whichever phone or emulator happens to be plugged
    in -- and the touch-target findings change with it. The result would depend
    on the hardware, which is not a test.

    Density is pinned rather than the adb call blocked, so a test that wants a
    specific density just assigns `auditor.density`.
    """
    monkeypatch.setattr(
        AccessibilityAuditor, "_resolve_density", lambda self: self.density or BASELINE_DPI
    )


def _audit(xml: str, density: int = BASELINE_DPI, **kwargs) -> AccessibilityAuditor:
    """Run the audit over a dump and return the auditor holding its findings."""
    auditor = AccessibilityAuditor(**kwargs)
    auditor.density = density
    auditor._audit_node(_xml_to_dict(ET.fromstring(xml)))
    return auditor


def _of_type(auditor: AccessibilityAuditor, issue_type: str) -> list[dict]:
    return [issue for issue in auditor.issues if issue["type"] == issue_type]


def test_the_report_carries_the_rectangle_as_ints(recorded):
    """A finding reports the rectangle parsed, not the raw attribute string.

    `accessibility_audit._parse_bounds` is gone: it was one of the skill's three
    bounds grammars, and the grammar now lives in `common.hierarchy.parse_bounds`
    where `test_hierarchy.py` tests it. What this file still owns is the shape a
    finding hands back, which a consumer indexes by name.
    """
    auditor = _audit(recorded.text("uiautomator_compose_default"))

    reported = [
        issue["element"]["bounds"] for issue in auditor.issues if "bounds" in issue["element"]
    ]
    assert reported, "no finding carried an element rectangle"
    assert all(
        isinstance(value, int) for box in reported for value in box.values()
    ), f"a rectangle came back unparsed: {reported}"
    assert {"left", "top", "right", "bottom"} == set(reported[0])


def test_attr_bool():
    assert _attr_bool("true") is True
    assert _attr_bool("false") is False
    assert _attr_bool("") is False


# ---------------------------------------------------------------------------
# The nested-attributes contract (CLAUDE.md): every UI field lives under
# node["attributes"] as a string. Reading them off the node returns nothing,
# which is how the audit once found zero issues on every screen.
# ---------------------------------------------------------------------------


def test_audit_reads_fields_from_the_nested_attributes_dict(recorded):
    """Settings has seven unlabelled ImageViews; the audit must see them."""
    auditor = _audit(recorded.text("uiautomator_current_screen"))

    assert auditor.issues, "the audit found nothing on a real screen"
    assert _of_type(auditor, "image_missing_description"), (
        "no ImageView finding on a screen full of ImageViews -- the audit is "
        "reading fields off the node instead of node['attributes']"
    )


# ---------------------------------------------------------------------------
# S7 -- touch targets are measured in dp, not pixels.
# ---------------------------------------------------------------------------


def test_recorded_controls_pass_at_the_baseline_density(recorded):
    """At 160dpi (1px == 1dp) nothing on the Compose screen is undersized."""
    auditor = _audit(recorded.text("uiautomator_compose_default"), density=BASELINE_DPI)
    assert _of_type(auditor, "small_touch_target") == []


def test_touch_targets_are_measured_in_dp_not_pixels(recorded):
    """The same 126x126px Compose checkbox is 126dp at mdpi and 36dp at 560dpi.

    A px-based check calls it fine on every device, which is how an undersized
    target ships. The reported figure must be the dp one, not the pixels.
    """
    auditor = _audit(recorded.text("uiautomator_compose_default"), density=XXHDPI)
    small = _of_type(auditor, "small_touch_target")

    assert small, "a 126px control is 36dp at 560dpi and must be flagged"
    checkbox = next(i for i in small if i["element"]["class"].endswith(".CheckBox"))
    assert checkbox["element"]["size"] == "126x126"
    assert "36dp" in checkbox["message"], f"message reports px, not dp: {checkbox['message']}"


def test_px_to_dp_converts_against_the_device_density():
    auditor = AccessibilityAuditor()
    auditor.density = 320
    assert auditor.px_to_dp(96) == pytest.approx(48.0)
    assert auditor.min_touch_target_px() == pytest.approx(96.0)


# ---------------------------------------------------------------------------
# S8 -- an unlabelled input is one with no describing text anywhere beneath it.
# ---------------------------------------------------------------------------


def test_a_field_labelled_by_its_own_subtree_is_not_flagged(recorded):
    """Compose puts a TextField's label in its subtree, not in an attribute.

    uiautomator emits no `hint` attribute at all, so a check reading one
    collapses to "this field is empty" and flags every correctly-labelled
    empty field. The recorded field's caption ("Email address") is a child
    TextView.
    """
    auditor = _audit(recorded.text("uiautomator_compose_default"))
    assert _of_type(auditor, "edittext_missing_hint") == []


def test_a_field_with_no_describing_text_anywhere_is_flagged(recorded):
    """The same recorded field with its caption blanked -- and only that.

    No recorded screen has a genuinely unlabelled input, so the scenario is
    derived by clearing the text of the nodes beneath the recorded EditText.
    Nothing else about the dump changes, so a passing test cannot be an
    artefact of an invented hierarchy.
    """
    root = ET.fromstring(recorded.text("uiautomator_compose_default"))
    field = next(n for n in root.iter("node") if n.get("class", "").endswith(".EditText"))
    for descendant in field.iter("node"):
        if descendant is not field:
            descendant.set("text", "")
            descendant.set("content-desc", "")

    auditor = AccessibilityAuditor()
    auditor.density = BASELINE_DPI
    auditor._audit_node(_xml_to_dict(root))

    hints = _of_type(auditor, "edittext_missing_hint")
    assert len(hints) == 1
    assert hints[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# L3 -- the only `critical` check cannot fire on any screen we have recorded.
# ---------------------------------------------------------------------------


def test_unlabelled_clickable_nodes_are_critical(recorded):
    """Quick Start step 5 used to return zero criticals on every Compose app.

    This screen has six clickable, enabled nodes with no text and no
    content-desc: four `android.view.View`, an EditText and a CheckBox. Check 1
    only fires when the class name contains button/imagebutton/imageview, so it
    fires on none of them -- and Compose emits `android.view.View` for every
    interactive node, so it fires on no Compose screen at all. Measured across
    the whole corpus: zero criticals on compose_default, compose_testtags,
    current_screen, settings_top and dialer_keypad.

    Inc 1 rebased the check on `is_interactive()` plus "no label anywhere in
    the subtree" (C7 + C5 + L3), and this test is unmarked from that commit on.
    """
    auditor = _audit(recorded.text("uiautomator_compose_default"))

    critical = [issue for issue in auditor.issues if issue["severity"] == "critical"]
    assert critical, "no critical finding on a screen with six unlabelled controls"
    assert any(issue["type"] == "missing_content_description" for issue in critical)


# ---------------------------------------------------------------------------
# The remaining checks, on the screens that exercise them.
# ---------------------------------------------------------------------------


def test_every_issue_has_a_fix(recorded):
    auditor = _audit(recorded.text("uiautomator_compose_default"))
    assert auditor.issues
    for issue in auditor.issues:
        assert issue.get("fix"), f"issue {issue['type']} is missing a fix suggestion"


def test_missing_resource_id_is_reported_and_testtags_clear_it(recorded):
    """Compose emits no resource-id, which `testTagsAsResourceId` fixes.

    The two dumps are the same screen recorded either side of that modifier, so
    the delta is evidence rather than assertion: seven findings become none.

    Seven, not the six this asserted when the check read `clickable and
    enabled`: the scrolling list at [32,1164][1048,1637] is driven by
    `scrollable`, carries no `clickable`, and was therefore invisible to a
    check about interactive elements. It is one of the seven controls the
    screen report names, so the audit was answering about a different set of
    controls than the rest of the skill (C7 / INC1-05).
    """
    default = _of_type(_audit(recorded.text("uiautomator_compose_default")), "missing_resource_id")
    assert len(default) == 7
    assert {issue["severity"] for issue in default} == {"info"}
    assert all(issue["fix"] for issue in default)

    tagged = _of_type(_audit(recorded.text("uiautomator_compose_testtags")), "missing_resource_id")
    assert tagged == []


def test_audit_flags_deep_nesting(recorded):
    """Real Compose trees are deep: 27 nodes sit below the default limit of 5."""
    flagged = _of_type(_audit(recorded.text("uiautomator_compose_default")), "deep_nesting")
    assert len(flagged) == 27
    assert {issue["severity"] for issue in flagged} == {"info"}
    assert min(issue["element"]["depth"] for issue in flagged) > 5

    # Raise the threshold above the tree's depth and the finding disappears,
    # so it is the threshold being tested and not the shape of the dump.
    relaxed = _audit(recorded.text("uiautomator_compose_default"), max_nesting=100)
    assert _of_type(relaxed, "deep_nesting") == []


def test_group_top_issues_ranks_by_severity_then_count():
    issues = [
        {"type": "image_missing_description", "severity": "info", "fix": "f"},
        {"type": "image_missing_description", "severity": "info", "fix": "f"},
        {"type": "missing_content_description", "severity": "critical", "fix": "f"},
        {"type": "edittext_missing_hint", "severity": "warning", "fix": "f"},
    ]
    grouped = AccessibilityAuditor.group_top_issues(issues)
    # Critical first, then warning, then info regardless of count.
    assert [g["type"] for g in grouped] == [
        "missing_content_description",
        "edittext_missing_hint",
        "image_missing_description",
    ]
    info_group = next(g for g in grouped if g["type"] == "image_missing_description")
    assert info_group["count"] == 2

    # Limit truncates the grouped list.
    assert len(AccessibilityAuditor.group_top_issues(issues, limit=1)) == 1
