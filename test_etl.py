"""Tests for the ETL's level parsing.

Focused on the one thing that silently produces wrong numbers: a depth
snapshot whose level arrays are not [[price, qty], ...]. Everything else in
etl.py either works or raises loudly.
"""
import pytest

from etl import MalformedLevels, _levels


def test_parses_normal_levels():
    levels, total = _levels([["1.50", "10.0"], ["1.49", "5.5"]])
    assert levels == [
        {"price": 1.50, "qty": 10.0},
        {"price": 1.49, "qty": 5.5},
    ]
    assert total == pytest.approx(15.5)


def test_empty_is_not_an_error():
    # An empty side is handled by the caller's `if not bids or not asks`
    # guard, not here - a book with one empty side is odd but well-formed.
    assert _levels([]) == ([], 0.0)


def test_rejects_three_element_level():
    # Observed in the wild: arbusdt, 20260925T01.
    with pytest.raises(MalformedLevels):
        _levels([["0.06341000", "1763.766980000", "7.90000000"]])


def test_rejects_one_element_level():
    # Same snapshot as above.
    with pytest.raises(MalformedLevels):
        _levels([["0.21920"]])


def test_rejects_when_only_one_entry_is_bad():
    """The whole call fails even though most entries are fine.

    This is the behaviour that matters. A spliced line usually corrupts one
    entry and leaves the rest looking plausible, so parsing the good ones and
    dropping the bad one would yield a truncated book that is indistinguishable
    from a genuinely thin one.
    """
    with pytest.raises(MalformedLevels):
        _levels([["1.50", "10.0"], ["1.49"], ["1.48", "3.0"]])


@pytest.mark.parametrize("entry", ["notalist", 42, None, {"price": 1}])
def test_rejects_non_sequence_entries(entry):
    with pytest.raises(MalformedLevels):
        _levels([entry])


def test_error_message_identifies_the_entry():
    with pytest.raises(MalformedLevels) as exc:
        _levels([["0.21920"]])
    assert "0.21920" in str(exc.value)
