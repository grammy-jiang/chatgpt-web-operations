"""Offline tests for label_matches, the pure helper in
tests/live/test_browser_model_label.py (TESTING.md section 2, "browser dry
run"). Imported directly rather than re-implemented, so this test can never
drift from what the live test actually asserts.

Tier T0: no live marker, no network. Importing the live test module is safe
here -- it only imports model_settings and defines functions at module
level; chatgpt_client is imported inside the live test itself, never at
module import time (the skill's HARD RULES), so nothing here needs
Playwright or a browser.
"""

from __future__ import annotations

from live.test_browser_model_label import label_matches


def test_an_exact_match_matches() -> None:
    assert label_matches("Medium", "Medium")


def test_matching_is_case_insensitive() -> None:
    assert label_matches("extra high", "Extra High")
    assert label_matches("EXTRA HIGH", "Extra High")


def test_matching_strips_incidental_whitespace_on_either_side() -> None:
    assert label_matches("  Medium\n", "Medium")
    assert label_matches("Medium", "  Medium  ")


def test_a_different_preset_title_does_not_match() -> None:
    assert not label_matches("Medium", "Extra High")


def test_a_substring_is_not_a_match() -> None:
    """A substring must not match: "High" is not "Extra High"."""
    assert not label_matches("High", "Extra High")


def test_empty_strings_do_not_falsely_match_a_real_title() -> None:
    assert not label_matches("", "Medium")
    assert not label_matches("Medium", "")
