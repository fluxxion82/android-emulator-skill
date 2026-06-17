"""Device-free tests for clipboard.py pure logic.

Two layers, both adb/subprocess-free:

1. ``parse_clipboard_parcel`` decodes the length-prefixed UTF-16LE Parcel dump
   emitted by ``adb shell service call clipboard 4`` into a Python string.
2. ``ClipboardManager.read_text`` / the ``--expected`` round-trip, with
   ``clipboard.subprocess.run`` monkeypatched, asserts match vs. mismatch.
"""

from __future__ import annotations

import subprocess

import clipboard
from clipboard import ClipboardManager, parse_clipboard_parcel


def _make_parcel_dump(text: str) -> str:
    """Build a ``service call clipboard`` Parcel dump for ``text``.

    Mirrors the real Android layout this parser targets: one leading zero
    header word, a 32-bit character-count word, then UTF-16 code units packed
    two-per-word (low half first). The exact hex grouping/whitespace is
    irrelevant to the parser (it scans 8-digit words), so a flat line suffices.
    """
    units = [ord(c) for c in text]
    words = [0x00000000, len(units)]
    for i in range(0, len(units), 2):
        low = units[i]
        high = units[i + 1] if i + 1 < len(units) else 0
        words.append((high << 16) | low)
    hex_words = " ".join(f"{w:08x}" for w in words)
    return f"Result: Parcel(\n  0x00000000: {hex_words} '....')\n)"


def test_parse_roundtrip_ascii():
    assert parse_clipboard_parcel(_make_parcel_dump("Hello")) == "Hello"


def test_parse_roundtrip_even_length():
    assert parse_clipboard_parcel(_make_parcel_dump("test")) == "test"


def test_parse_roundtrip_unicode():
    assert parse_clipboard_parcel(_make_parcel_dump("café ☕")) == "café ☕"


def test_parse_trailing_nul_padding_trimmed():
    # Odd-length strings leave a high half = 0x0000 in the final word.
    assert parse_clipboard_parcel(_make_parcel_dump("abc")) == "abc"


def test_parse_empty_clipboard_returns_none():
    # Header word present but zero character count -> nothing to decode.
    assert parse_clipboard_parcel("Result: Parcel(\n  0x0: 00000000 00000000 )\n)") is None


def test_parse_garbage_returns_none():
    assert parse_clipboard_parcel("nothing useful here") is None


def test_read_text_decodes_clipboard(monkeypatch):
    dump = _make_parcel_dump("Hello World")

    def fake_run(cmd, *args, **kwargs):  # noqa: ARG001
        assert "service" in cmd and "call" in cmd and "clipboard" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=dump, stderr="")

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    ok, _msg, text = ClipboardManager().read_text()
    assert ok is True
    assert text == "Hello World"


def test_copy_then_expected_match(monkeypatch):
    captured: list[list[str]] = []
    dump = _make_parcel_dump("secret")

    def fake_run(cmd, *args, **kwargs):  # noqa: ARG001
        captured.append(cmd)
        # First call is the copy (service code "1"); second is the read ("4").
        out = dump if "4" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    mgr = ClipboardManager()
    copy_ok, _ = mgr.copy("secret")
    read_ok, _msg, actual = mgr.read_text()

    assert copy_ok is True
    assert read_ok is True
    assert actual == "secret"
    # The copy must marshal the text via service code 1; read uses code 4.
    assert any("1" in c and "service" in c for c in captured)
    assert any("4" in c and "service" in c for c in captured)


def test_copy_then_expected_mismatch(monkeypatch):
    dump = _make_parcel_dump("actual-value")

    def fake_run(cmd, *args, **kwargs):  # noqa: ARG001
        out = dump if "4" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)

    mgr = ClipboardManager()
    mgr.copy("expected-value")
    _ok, _msg, actual = mgr.read_text()
    assert actual != "expected-value"
    assert actual == "actual-value"
