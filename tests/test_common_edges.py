"""T0 tests for _common's boundaries, written for the mutants the P10 pilot
found alive (TESTING.md section 6, P10; VERIFICATION.md)."""

from __future__ import annotations

import _common as common
import pytest


def test_shorten_keeps_exactly_seventy_characters_and_cuts_the_seventy_first() -> None:
    assert common.shorten("x" * 70) == "x" * 70
    cut = common.shorten("x" * 71)
    assert cut == "x" * 69 + "…"
    assert len(cut) == 70


def test_shorten_keeps_text_as_long_as_the_limit() -> None:
    assert common.shorten("abcde", 5) == "abcde"
    assert common.shorten("abcdef", 5) == "abcd…"


def test_a_row_with_more_cells_than_headers_is_an_error_not_a_lost_cell() -> None:
    with pytest.raises(ValueError):
        common.table([("a", "b", "extra")], ("one", "two"))
