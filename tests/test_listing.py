"""Tests for scrape/listing.py — parsing both HTML pages and API JSON items."""
from __future__ import annotations

import json

import pytest

import re

from metacrawl.scrape.listing import GameSummary, PlatformScore, _image_url, parse_listing, to_summary


# ---------------------------------------------------------------------------
# parse_listing() on browse.html
# ---------------------------------------------------------------------------

def test_browse_html_yields_24_games(browse_html):
    games = parse_listing(browse_html)
    assert len(games) == 24


def test_browse_game_has_required_fields(browse_html):
    games = parse_listing(browse_html)
    first = games[0]
    assert isinstance(first.external_id, int) and first.external_id > 0
    assert isinstance(first.slug, str) and first.slug
    assert isinstance(first.title, str) and first.title
    assert first.url.startswith("https://www.metacritic.com/game/")


def test_browse_all_games_are_game_summaries(browse_html):
    games = parse_listing(browse_html)
    assert all(isinstance(g, GameSummary) for g in games)


# ---------------------------------------------------------------------------
# to_summary() on API JSON items
# ---------------------------------------------------------------------------

def test_api_new_releases_yields_20_summaries(api_new_releases):
    items = api_new_releases["data"]["items"]
    summaries = [to_summary(item) for item in items if isinstance(item, dict)]
    summaries = [s for s in summaries if s is not None]
    assert len(summaries) == 20


def test_api_page2_yields_24_summaries(api_page2):
    items = api_page2["data"]["items"]
    summaries = [to_summary(item) for item in items if isinstance(item, dict)]
    summaries = [s for s in summaries if s is not None]
    assert len(summaries) == 24


def test_api_item_fields_correct(api_new_releases):
    item = api_new_releases["data"]["items"][0]
    s = to_summary(item)
    assert s is not None
    assert s.external_id == item["id"]
    assert s.slug == item["slug"]
    assert s.title == item["title"]


def test_api_item_metascore_parsed(api_new_releases):
    # First item in the new-releases fixture has score=68 (over-the-hill)
    item = api_new_releases["data"]["items"][0]
    s = to_summary(item)
    assert s.metascore == item["criticScoreSummary"]["score"]


def test_api_item_platform_names(api_new_releases):
    item = api_new_releases["data"]["items"][0]
    s = to_summary(item)
    assert len(s.platforms) == len(item["platforms"])
    expected = {p["name"] for p in item["platforms"]}
    actual = {p.name for p in s.platforms}
    assert actual == expected


def test_api_item_with_null_description(api_new_releases):
    # Unreleased / minimal entries have null description — must not crash.
    null_desc = next(
        (item for item in api_new_releases["data"]["items"] if item.get("description") is None),
        None,
    )
    if null_desc is None:
        pytest.skip("fixture has no item with null description")
    s = to_summary(null_desc)
    assert s is not None
    assert s.description is None


def test_api_item_genre_names(api_new_releases):
    item = api_new_releases["data"]["items"][0]
    s = to_summary(item)
    expected = [g["name"] for g in item["genres"] if isinstance(g, dict) and g.get("name")]
    assert s.genres == expected


def test_to_summary_returns_none_for_non_game():
    bad = {"id": 1, "slug": "x", "title": "X", "type": "movie-title"}
    assert to_summary(bad) is None


def test_to_summary_returns_none_for_missing_slug():
    assert to_summary({"id": 1, "title": "X"}) is None


# ---------------------------------------------------------------------------
# _image_url() helper
# ---------------------------------------------------------------------------

def test_image_url_prefers_direct_url():
    img = {"imageUrl": "https://cdn.example.com/cover.jpg", "bucketPath": "/foo/bar.jpg",
           "width": 460, "height": 215}
    assert _image_url(img) == "https://cdn.example.com/cover.jpg"


def test_image_url_uses_url_map_over_composition():
    img = {"imageUrl": None, "bucketPath": "/provider/6/12/6-1-1077396-52.jpg",
           "width": 460, "height": 215}
    hashed = "https://www.metacritic.com/a/img/resize/aac46445b00acb384d2342af93103c7c29918d33/catalog/provider/6/12/6-1-1077396-52.jpg?auto=webp"
    url_map = {"/provider/6/12/6-1-1077396-52.jpg": hashed}
    assert _image_url(img, url_map) == hashed


def test_image_url_returns_none_without_map():
    """No url_map means no hash available — composed form 403s, so return None."""
    img = {"imageUrl": None, "bucketPath": "/provider/7/2/123.jpg", "width": 373, "height": 560}
    assert _image_url(img) is None


def test_api_path_image_url_is_none():
    """Games from the finder JSON API have imageUrl=null and no HTML to scrape.
    to_summary() with no url_map must yield image_url=None (placeholder),
    not a broken composed URL that 403s on every request."""
    item = {
        "id": 1087502,
        "slug": "over-the-hill",
        "title": "Over the Hill",
        "image": {
            "imageUrl": None,
            "bucketPath": "/provider/6/12/6-1-1087502-52.jpg",
            "width": 460,
            "height": 215,
            "bucketType": "catalog",
        },
    }
    s = to_summary(item)   # no url_map — simulates the finder JSON API path
    assert s is not None
    assert s.image_url is None, (
        f"expected None for API path with no url_map, got {s.image_url!r}"
    )


def test_image_url_none_for_null_image():
    assert _image_url(None) is None
    assert _image_url({}) is None


def test_browse_cover_url_is_hashed_cdn_url(browse_html):
    """parse_listing must extract the hashed CDN URL, not a composed width/height form."""
    games = parse_listing(browse_html)
    assert games[0].image_url is not None
    assert re.match(
        r"https://www\.metacritic\.com/a/img/resize/[a-f0-9]{40}/catalog/",
        games[0].image_url,
    ), f"expected hashed CDN URL, got: {games[0].image_url}"


def test_browse_cover_url_includes_query_params(browse_html):
    """
    The extracted URL must include ?auto=webp query params.
    Without them the CDN returns 403 — the hash is computed per-size and the
    query string must be present for the CDN to accept the request.
    """
    games = parse_listing(browse_html)
    url = games[0].image_url
    assert url is not None
    assert "?auto=webp" in url, (
        f"CDN URL is missing required query params (?auto=webp…), got: {url}\n"
        "Root cause: _CDN_IMG regex must not stop at '?' — it must capture the "
        "full query string."
    )
