"""Shared test fixtures for android-emulator-skill tests.

Sample data does not live here. It used to: ``SAMPLE_UI_HIERARCHY`` was a
hand-written ``uiautomator dump`` of a login screen that does not exist, and it
was the only input ``test_accessibility_audit.py`` ever ran against -- a screen
shaped, unavoidably, like the checks someone expected to pass. It has been
deleted (T2).

Real dumps live in ``tests/fixtures/recorded/<profile>/`` and are read through
the ``recorded`` / ``any_profile`` / ``recorded_gradle`` fixtures in
``tests/conftest.py``. If a test needs a screen the corpus does not have,
record it (``python tests/record_fixtures.py --list``) or derive it by editing
one attribute of a recorded dump in the test, saying which dump and which
attribute. Do not add a plausible substitute here.
"""

from __future__ import annotations
