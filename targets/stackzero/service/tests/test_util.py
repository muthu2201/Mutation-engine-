"""Unit tests for pure helpers. Tests are frozen loci: genes can never modify them, and the
evaluator always runs the baseline copy of this file against the candidate."""

import pytest

from shop import native, util


def test_tokenize_order_and_dedupe():
    assert util.tokenize("The Waterproof waterproof Leather BACKPACK, for camping!") == ["waterproof", "leather", "backpack", "camping"]


def test_tokenize_drops_stopwords_and_short_tokens():
    assert util.tokenize("a b and of x2 to") == ["x2"]
    assert util.tokenize("") == []


def test_format_money():
    assert util.format_money(0) == "0.00"
    assert util.format_money(5) == "0.05"
    assert util.format_money(123456) == "1234.56"
    assert util.format_money(-250) == "-2.50"


def test_parse_int_bounds():
    assert util.parse_int("7", "n", 1, 10) == 7
    with pytest.raises(util.HTTPError):
        util.parse_int("11", "n", 1, 10)
    with pytest.raises(util.HTTPError):
        util.parse_int("x", "n", 1, 10)


def test_parse_timestamp_requires_timezone():
    assert util.parse_timestamp("2030-01-01T10:00:00+00:00", "t").year == 2030
    with pytest.raises(util.HTTPError):
        util.parse_timestamp("2030-01-01T10:00:00", "t")


def test_native_score_exact_and_fuzzy_matches():
    docs = ["Rugged Leather Backpack 120", "Steel Kettle Glass 300", "Rugged Lether Bakpack 120"]
    scores = native.score(["leather", "backpack"], docs)
    # equal-length documents: exact matches outrank typo matches, unrelated text scores 0
    assert scores[0] > scores[2] > 0.0
    assert scores[1] == 0.0


def test_native_score_empty():
    assert native.score(["x"], []) == []
    assert native.score([], ["anything here"]) == [0.0]
