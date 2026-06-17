"""Regression tests for the accessibility-audit attribute-parsing fix.

Before the fix, ``_audit_node`` read fields directly off each node, but
``get_ui_hierarchy`` nests them under ``node["attributes"]`` (as XML strings),
so the audit silently found nothing. These tests parse a representative
uiautomator hierarchy and assert real issues are detected.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from accessibility_audit import AccessibilityAuditor, _attr_bool, _parse_bounds
from common.device_utils import _xml_to_dict
from fixtures import SAMPLE_UI_HIERARCHY


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
    # math runs rather than being silently skipped. The sample's targets are
    # all large, so there must be no false small-target finding.
    auditor = AccessibilityAuditor()
    auditor._audit_node(_sample_hierarchy())
    small = [i for i in auditor.issues if i["type"] == "small_touch_target"]
    assert small == []
