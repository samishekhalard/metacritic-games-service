"""Tests for metacrawl.payload — devalue decoding, no network."""
from __future__ import annotations

import math

import pytest

from metacrawl.payload import PayloadError, extract, parse, resolve


# ---------------------------------------------------------------------------
# extract()
# ---------------------------------------------------------------------------

def test_extract_raises_when_no_script():
    with pytest.raises(PayloadError, match="no __NUXT_DATA__"):
        extract("<html><body>no payload</body></html>")


def test_extract_raises_on_invalid_json():
    html = '<script id="__NUXT_DATA__">{not json}</script>'
    with pytest.raises(PayloadError, match="not valid JSON"):
        extract(html)


def test_extract_raises_when_root_is_not_list():
    html = '<script id="__NUXT_DATA__">{"key": "value"}</script>'
    with pytest.raises(PayloadError, match="devalue array"):
        extract(html)


def test_extract_returns_list():
    html = '<script id="__NUXT_DATA__">[1, 2, {"a": 3}]</script>'
    result = extract(html)
    assert result == [1, 2, {"a": 3}]


# ---------------------------------------------------------------------------
# resolve() — devalue sentinel values
# ---------------------------------------------------------------------------

def test_resolve_negative_one_is_none():
    assert resolve([], -1) is None


def test_resolve_negative_two_is_none():
    assert resolve([], -2) is None


def test_resolve_negative_three_is_nan():
    assert math.isnan(resolve([], -3))


def test_resolve_negative_four_is_inf():
    assert resolve([], -4) == float("inf")


def test_resolve_negative_five_is_neg_inf():
    assert resolve([], -5) == float("-inf")


def test_resolve_integer_reference_inside_dict():
    # Devalue puts integer references INSIDE composite values, not as bare slots.
    # data[0] = "hello"  (literal string)
    # data[1] = {"msg": 0}  (dict whose "msg" value is a pointer to slot 0)
    data = ["hello", {"msg": 0}]
    assert resolve(data, 1) == {"msg": "hello"}


def test_resolve_dict_with_literal_values():
    # Values in the object are themselves literal integers (not pointers here
    # because positive integers ARE resolved as pointers — so we use strings).
    data = [{"a": 1, "b": 2}]
    # data[1] and data[2] don't exist → both resolve to None
    assert resolve(data, 0) == {"a": None, "b": None}


def test_resolve_integer_reference_inside_list():
    # data[0] = "x", data[1] = [0, 0]  → list of two references to slot 0
    data = ["x", [0, 0]]
    assert resolve(data, 1) == ["x", "x"]


def test_resolve_unwraps_ref():
    # ["Ref", <pointer>]  →  the pointed-to value
    data = ["world", ["Ref", 0]]
    assert resolve(data, 1) == "world"


def test_resolve_depth_limit_returns_none():
    # A deeply nested structure should not blow the stack.
    # Build a chain: each slot points to the next.
    data = [["Ref", 1], ["Ref", 2], ["Ref", 3], "deep"]
    # Resolve with depth already at MAX_DEPTH — must return None, not recurse.
    from metacrawl.payload import MAX_DEPTH
    result = resolve(data, 0, depth=MAX_DEPTH)
    assert result is None


# ---------------------------------------------------------------------------
# parse() — full integration on game.html fixture
# ---------------------------------------------------------------------------

def test_parse_returns_dict(game_html):
    root = parse(game_html)
    assert isinstance(root, dict)


def test_parse_game_html_has_elden_ring_key(game_html):
    root = parse(game_html)
    assert "loadPage:games:elden-ring:" in root["data"]


def test_parse_game_html_product_component(game_html):
    root = parse(game_html)
    page = root["data"]["loadPage:games:elden-ring:"]
    component_names = [
        (c.get("meta") or {}).get("componentName")
        for c in page["components"]
        if isinstance(c, dict)
    ]
    assert "product" in component_names
    assert "critic-score-summary" in component_names
    assert "user-score-summary" in component_names
    assert "critic-reviews" in component_names
    assert "user-reviews" in component_names


def test_parse_browse_html_returns_dict(browse_html):
    root = parse(browse_html)
    assert isinstance(root, dict)
    assert "data" in root
