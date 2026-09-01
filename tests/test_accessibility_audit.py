"""Regression tests for the accessibility-audit attribute-parsing fix.

Before the fix, ``_audit_node`` read fields directly off each node, but
``get_ui_hierarchy`` nests them under ``node["attributes"]`` (as XML strings),
so the audit silently found nothing. These tests parse a representative
uiautomator hierarchy and assert real issues are detected.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from accessibility_audit import AccessibilityAuditor, _attr_bool, _parse_bounds
from fixtures import SAMPLE_UI_HIERARCHY

from common.device_utils import _xml_to_dict

# The density every unpinned test in this file runs at. 160dpi is Android's
# baseline, where 1px == 1dp, so px-shaped fixtures keep their intended sizes.
BASELINE_DPI = 160


@pytest.fixture(autouse=True)
def pinned_density(monkeypatch):
    """Keep the audit off any attached device.

    `_resolve_density` asks adb for the real density, so without this every
    test here silently reads whichever phone or emulator happens to be plugged
    in -- and the touch-target findings change with it. That is what made
    `test_audit_parses_touch_target_bounds` fail: a device reported 420dpi, and
    the sample's 100px row became 38dp, correctly under the 48dp minimum. The
    result depended on the hardware, which is not a test.

    Density is pinned rather than the adb call blocked, so a test that wants a
    specific density just assigns `auditor.density`.
    """
    monkeypatch.setattr(
        AccessibilityAuditor, "_resolve_density", lambda self: self.density or BASELINE_DPI
    )


def _sample_hierarchy() -> dict:
    return _xml_to_dict(ET.fromstring(SAMPLE_UI_HIERARCHY))


def test_parse_bounds_valid():
    assert _parse_bounds("[0,0][1080,2400]") == {
        "left": 0,
        "top": 0,
        "right": 1080,
        "bottom": 2400,
    }


def test_parse_bounds_invalid():
    assert _parse_bounds("") == {}
    assert _parse_bounds("not-bounds") == {}


def test_attr_bool():
    assert _attr_bool("true") is True
    assert _attr_bool("false") is False
    assert _attr_bool("") is False


def test_audit_detects_issues_from_nested_attributes():
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    types = {issue["type"] for issue in auditor.issues}
    # EditText with no hint/text/content-desc -> warning
    assert "edittext_missing_hint" in types
    # Clickable ImageView with no content description -> critical + info
    assert "missing_content_description" in types
    assert "image_missing_description" in types


def test_audit_parses_touch_target_bounds():
    # Ensures bounds parse to ints (not an unparsed string), so touch-target
    # math runs rather than being silently skipped. At 160dpi the sample's
    # targets are all >= 48dp, so there must be no false small-target finding.
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    small = [i for i in auditor.issues if i["type"] == "small_touch_target"]
    assert small == []


def test_touch_targets_are_measured_in_dp_not_pixels():
    """48 *pixels* is not the threshold; 48dp is, and they differ per device.

    The sample's 1000x100px row passes at 160dpi and fails at 420dpi -- the
    same layout, the same pixels, a different verdict. A px-based check calls
    it fine on every device, which is how an undersized target ships.
    """
    auditor = AccessibilityAuditor()
    auditor.density = 420
    auditor._audit_node(_sample_hierarchy())

    small = [i for i in auditor.issues if i["type"] == "small_touch_target"]
    assert small, "a 100px-tall row is 38dp at 420dpi and must be flagged"
    assert "38dp" in small[0]["message"], f"message reports px, not dp: {small[0]['message']}"


def test_px_to_dp_converts_against_the_device_density():
    auditor = AccessibilityAuditor()
    auditor.density = 320
    assert auditor.px_to_dp(96) == pytest.approx(48.0)
    assert auditor.min_touch_target_px() == pytest.approx(96.0)


def test_audit_detects_critical_issue():
    # The clickable ImageView avatar with no content-desc/text is critical.
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    critical = [i for i in auditor.issues if i["severity"] == "critical"]
    assert critical, "expected at least one critical issue"
    assert any(i["type"] == "missing_content_description" for i in critical)


def test_every_issue_has_a_fix():
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    assert auditor.issues
    for issue in auditor.issues:
        assert issue.get("fix"), f"issue {issue['type']} is missing a fix suggestion"


def test_audit_flags_missing_resource_id():
    # All sample nodes carry a resource-id, so the fixture alone produces none.
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    assert not [i for i in auditor.issues if i["type"] == "missing_resource_id"]

    # A clickable, enabled node with an empty resource-id must be flagged info.
    node = {
        "tag": "node",
        "attributes": {
            "class": "android.widget.Button",
            "resource-id": "",
            "clickable": "true",
            "enabled": "true",
            "content-desc": "Submit",
            "bounds": "[0,0][100,100]",
        },
        "children": [],
    }
    auditor2 = AccessibilityAuditor()
    auditor2._audit_node(node)
    missing = [i for i in auditor2.issues if i["type"] == "missing_resource_id"]
    assert len(missing) == 1
    assert missing[0]["severity"] == "info"
    assert missing[0]["fix"]


def test_audit_flags_deep_nesting():
    # Default max nesting is 5; the shallow sample must not trip it.
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    assert not [i for i in auditor.issues if i["type"] == "deep_nesting"]

    # With a low threshold, a node audited at depth > max_nesting is flagged.
    leaf = {
        "tag": "node",
        "attributes": {
            "class": "android.widget.TextView",
            "resource-id": "x",
            "clickable": "false",
            "enabled": "true",
        },
        "children": [],
    }
    shallow = AccessibilityAuditor(max_nesting=2)
    shallow._audit_node(leaf, depth=2)
    assert not [i for i in shallow.issues if i["type"] == "deep_nesting"]

    deep = AccessibilityAuditor(max_nesting=2)
    deep._audit_node(leaf, depth=3)
    nesting = [i for i in deep.issues if i["type"] == "deep_nesting"]
    assert len(nesting) == 1
    assert nesting[0]["severity"] == "info"
    assert nesting[0]["element"]["depth"] == 3


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
