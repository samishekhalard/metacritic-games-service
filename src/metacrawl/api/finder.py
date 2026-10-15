"""
Metacritic finder API client.

Wraps the two query modes described in BRIEF.md §1.1:
  - new_releases(): 20 scored games from the New Releases carousel
  - browse_page():  one page from the full sorted browse list
"""
from __future__ import annotations

from ..http import HttpClient
from ..scrape.listing import GameSummary, to_summary

_FINDER = "https://backend.metacritic.com/finder/metacritic/web"
_BASE_PARAMS = {
    "componentType": "ProductList",
    "mcoTypeId": "13",
    "sortBy": "-releaseDate",
}


def _items(data: dict) -> list[dict]:
    return data.get("data", {}).get("items", []) or []


def _summaries(data: dict) -> list[GameSummary]:
    return [s for item in _items(data) if isinstance(item, dict) and (s := to_summary(item))]


def fetch_new_releases(client: HttpClient, limit: int = 20) -> list[GameSummary]:
    """First `limit` scored games from the New Releases carousel."""
    data = client.get_json(_FINDER, {
        **_BASE_PARAMS,
        "componentName": "new-releases-carousel",
        "metaScoreMin": "1",
        "offset": "0",
        "limit": str(limit),
    })
    return _summaries(data)


def fetch_browse_page(client: HttpClient, offset: int = 0, limit: int = 24) -> tuple[list[GameSummary], str | None]:
    """One page of the full browse list. Returns (games, next_url)."""
    data = client.get_json(_FINDER, {
        **_BASE_PARAMS,
        "offset": str(offset),
        "limit": str(limit),
    })
    next_href: str | None = (
        data.get("links", {}).get("next", {}) or {}
    ).get("href")
    return _summaries(data), next_href


def fetch_browse_url(client: HttpClient, url: str) -> tuple[list[GameSummary], str | None]:
    """Fetch a pre-built browse URL (e.g. links.next.href from a previous page)."""
    data = client.get_json(url)
    next_href: str | None = (
        data.get("links", {}).get("next", {}) or {}
    ).get("href")
    return _summaries(data), next_href


def fetch_scored_page(
    client: HttpClient,
    offset: int = 0,
    limit: int = 20,
) -> tuple[list[GameSummary], str | None]:
    """
    One page of games that have a Metascore (metaScoreMin=1), sorted by release
    date descending.  Returns (games, next_url) where next_url is the
    links.next.href cursor for the following page.

    This is the paginated form of the New Releases carousel query from
    BRIEF.md §1.1.  The carousel only exposes 20 items; this function lets
    callers walk the full scored catalogue page-by-page.
    """
    data = client.get_json(_FINDER, {
        **_BASE_PARAMS,
        "metaScoreMin": "1",
        "offset": str(offset),
        "limit": str(limit),
    })
    next_href: str | None = (
        data.get("links", {}).get("next", {}) or {}
    ).get("href")
    return _summaries(data), next_href
