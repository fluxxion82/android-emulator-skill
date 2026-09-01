"""R11: screen_mapper must see a Jetpack Compose screen.

`screen_mapper` decided what was interactive from a class-name whitelist
(Button, EditText, TextView, ...). Compose renders its semantics nodes as
`android.view.View`, so on a Compose screen — the default for new Android apps
since 2022, and step 2 of SKILL.md's own Quick Start — it reported almost
nothing.

Everything asserted here is measured from two recorded dumps of a real Compose
app (`tests/fixtures/scaffold/compose/`), not from expectation. Three
assumptions in the original plan were wrong, and the fixtures are what caught
them:

1. **`AndroidComposeView` never appears.** The host class in the dump is
   `androidx.compose.ui.platform.ComposeView`. A detector looking for the former
   would match nothing.
2. **`mergeDescendants` merges nothing here.** uiautomator dumps the *unmerged*
   semantics tree, so a clickable Card keeps its three Texts as separate
   children and its own `text` stays empty. The plan expected concatenated
   labels; there is no concatenation anywhere.
3. **Every interactive Compose node is unlabelled** — `text=""` and
   `content-desc=""` on all seven. The planned eligibility rule required a
   label, so it would have matched zero elements and "fixed" the defect into
   producing identical empty output.

Labels therefore have to be recovered from the tree: from descendants for a
Button/Card/list, and from a row-adjacent sibling for a Checkbox/Switch/icon.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

RECORDED = Path(__file__).resolve().parent / "fixtures" / "recorded" / "emulator-api35"

INTERACTIVE_ATTRS = ("clickable", "checkable", "scrollable", "long-clickable")


def _root(name: str) -> ET.Element:
    return ET.parse(RECORDED / f"{name}.xml").getroot()


def _is_interactive(node: ET.Element) -> bool:
    return any(node.get(attr) == "true" for attr in INTERACTIVE_ATTRS)


def _label(node: ET.Element) -> str:
    return ((node.get("text") or "") or (node.get("content-desc") or "")).strip()


# ---------------------------------------------------------------------------
# Premises. If these stop holding, the fix below is aimed at the wrong thing.
# ---------------------------------------------------------------------------


def test_compose_host_class_is_composeview_not_androidcomposeview():
    """The detection string the plan assumed does not occur."""
    raw = (RECORDED / "uiautomator_compose_default.xml").read_text(encoding="utf-8")
    assert "AndroidComposeView" not in raw
    assert "androidx.compose.ui.platform.ComposeView" in raw


def test_every_interactive_compose_node_is_unlabelled():
    """The premise that breaks a label-requiring eligibility rule."""
    interactive = [
        n for n in _root("uiautomator_compose_default").iter("node") if _is_interactive(n)
    ]
    assert interactive, "fixture has no interactive nodes"
    assert not [n for n in interactive if _label(n)], (
        "some interactive node carries its own label; re-check whether label "
        "recovery is still required"
    )


def test_compose_emits_no_resource_ids_by_default():
    """The realistic case: nothing to address elements by."""
    ids = {
        n.get("resource-id")
        for n in _root("uiautomator_compose_default").iter("node")
        if (n.get("resource-id") or "").strip()
    }
    # The only id present belongs to the AOSP content FrameLayout, not Compose.
    assert ids <= {"android:id/content"}, f"unexpected resource-ids: {ids}"


def test_test_tags_surface_as_bare_ids_not_package_qualified():
    """`testTagsAsResourceId` yields the bare tag, e.g. `submit_button`.

    Anything splitting a resource-id on ":id/" to get a name drops all of them.
    """
    ids = {
        n.get("resource-id")
        for n in _root("uiautomator_compose_testtags").iter("node")
        if (n.get("resource-id") or "").strip()
    }
    tags = ids - {"android:id/content"}
    assert tags, "testtags fixture exposes no tags"
    assert not any(":id/" in tag for tag in tags), f"expected bare tags, got {tags}"


def test_a_clickable_card_does_not_merge_its_children():
    """uiautomator dumps the unmerged tree, so there is no concatenated label."""
    card = next(
        n
        for n in _root("uiautomator_compose_default").iter("node")
        if _is_interactive(n) and len([c for c in n.iter("node") if _label(c)]) >= 3
    )
    assert _label(card) == "", "the card now carries a merged label; revisit recovery"
    child_labels = [_label(c) for c in card.iter("node") if c is not card and _label(c)]
    assert "Order #4821" in child_labels
    assert len(child_labels) >= 3


# ---------------------------------------------------------------------------
# The defect and its fix.
# ---------------------------------------------------------------------------


def _analyse(fixture: str) -> dict:
    """Run the production analyser over a recorded dump.

    `screen_mapper.analyze_tree` takes an ``ET.Element``, not the dict shape
    `common.device_utils.get_ui_hierarchy` returns -- the repo still carries two
    hierarchy representations, which the hierarchy-API work will unify.
    """
    from screen_mapper import ScreenMapper

    return ScreenMapper().analyze_tree(_root(fixture))


def test_compose_screen_reports_its_interactive_elements():
    """The defect itself: this returned ~0 on a screen full of controls."""
    analysis = _analyse("uiautomator_compose_default")
    assert analysis["interactive_elements"] >= 6, (
        f"only {analysis['interactive_elements']} interactive elements found on a "
        f"Compose screen with 7 controls; the class-name whitelist is still gating"
    )


def test_a_view_based_screen_is_not_flooded():
    """Guard against over-correcting: eligibility must stay selective.

    Dropping the whitelist for "anything focusable" would report most of the
    tree. The View-based dump has 65 nodes; a sane answer is a small fraction.
    """
    analysis = _analyse("uiautomator_current_screen")
    assert analysis["interactive_elements"] <= analysis["total_elements"] // 3, (
        f"{analysis['interactive_elements']} of {analysis['total_elements']} nodes "
        f"reported interactive; eligibility is too loose to be useful"
    )
    assert analysis["interactive_elements"] > 0


@pytest.mark.parametrize(
    "expected",
    ["Submit Order", "Order #4821", "Email address", "Remember me", "Dark theme"],
)
def test_control_labels_are_recovered_from_the_tree(expected: str):
    """Compose controls are unlabelled; their text lives in the subtree or the row.

    'Submit Order', 'Order #4821' and 'Email address' are descendants of their
    control. 'Remember me' and 'Dark theme' are row-adjacent siblings of a
    Checkbox and a Switch. Both positions must be recovered or those controls
    are anonymous to an agent.
    """
    analysis = _analyse("uiautomator_compose_default")

    # Deliberately excludes TextView: those passive labels are already in the
    # dump, so counting them would let this pass while every control stayed
    # anonymous -- which is exactly what the unfixed code does.
    control_labels = [
        label
        for cls, labels in analysis["elements_by_type"].items()
        if cls != "TextView"
        for label in labels
    ]
    # Substring, because a control's recovered label may join several child
    # texts ("Order #4821 2 items Ships tomorrow"). TextView is excluded above,
    # so this cannot be satisfied by the passive label alone.
    assert any(expected in label for label in control_labels), (
        f"{expected!r} is not attached to any interactive control; "
        f"controls carry: {control_labels}"
    )


def test_bounds_are_required_so_zero_size_nodes_are_not_offered():
    """A zero-area node cannot be tapped, whatever its flags say."""
    from screen_mapper import ScreenMapper

    zero = ET.fromstring(
        '<hierarchy><node class="android.view.View" clickable="true" enabled="true" '
        'text="" content-desc="" resource-id="" bounds="[0,0][0,0]" /></hierarchy>'
    )
    analysis = ScreenMapper().analyze_tree(zero)
    assert analysis["interactive_elements"] == 0


# ---------------------------------------------------------------------------
# S8 — the unlabelled-field check must use information that actually exists.
# ---------------------------------------------------------------------------


def test_a_field_described_by_a_child_label_is_not_flagged():
    """`hint` is not an attribute uiautomator emits, so it was always absent.

    The check read `attrs.get("hint", "")`, which is empty on every node in
    every recorded dump, so the condition collapsed to "this field is empty" --
    flagging every correctly-hinted empty field. The Compose TextField here IS
    labelled: 'Email address' sits in its subtree. It must not be reported.
    """
    from accessibility_audit import AccessibilityAuditor

    from common.device_utils import _xml_to_dict

    auditor = AccessibilityAuditor()
    auditor.density = 420
    auditor.audit_tree(_xml_to_dict(_root("uiautomator_compose_default")))

    flagged = [i for i in auditor.issues if i["type"] == "edittext_missing_hint"]
    assert (
        not flagged
    ), f"a field labelled 'Email address' by its child was flagged as unlabelled: {flagged}"


def test_a_genuinely_unlabelled_field_is_still_flagged():
    """Guard against deleting the check rather than fixing it."""
    from accessibility_audit import AccessibilityAuditor

    from common.device_utils import _xml_to_dict

    bare = ET.fromstring(
        '<hierarchy><node class="android.widget.EditText" clickable="true" enabled="true" '
        'text="" content-desc="" resource-id="" bounds="[0,0][400,120]" /></hierarchy>'
    )
    auditor = AccessibilityAuditor()
    auditor.density = 420
    auditor.audit_tree(_xml_to_dict(bare))

    assert any(i["type"] == "edittext_missing_hint" for i in auditor.issues)
