"""
Fetching and parsing a game detail page.

The game page is a server-rendered Nuxt 3 HTML page; the backend exposes no
JSON endpoint for game data.  We fetch the HTML and decode the __NUXT_DATA__
blob via payload.py → scrape/detail.py.

Reviews are fetched from the separate backend reviews API described in
BRIEF.md §1.4 and parsed directly from JSON (no Nuxt blob there).
"""
from __future__ import annotations

from ..http import HttpClient
from ..scrape.detail import GameDetail, ReviewItem, parse_detail

_GAME_BASE = "https://www.metacritic.com/game"
_REVIEWS_BASE = "https://backend.metacritic.com/reviews/metacritic"


def fetch_detail(client: HttpClient, slug: str) -> GameDetail:
    """Fetch and parse a game's detail page."""
    url = f"{_GAME_BASE}/{slug}/"
    html = client.get_text(url)
    return parse_detail(html, slug)


def fetch_critic_reviews(client: HttpClient, slug: str, limit: int = 50) -> list[ReviewItem]:
    """Fetch critic reviews from the backend JSON API."""
    url = f"{_REVIEWS_BASE}/critic/games/{slug}/web"
    data = client.get_json(url, {
        "offset": "0",
        "limit": str(limit),
        "filterBySentiment": "all",
        "sort": "score",
    })
    return _parse_critic_reviews(data)


def fetch_user_reviews(client: HttpClient, slug: str, limit: int = 50) -> list[ReviewItem]:
    """Fetch user reviews from the backend JSON API."""
    url = f"{_REVIEWS_BASE}/user/games/{slug}/web"
    data = client.get_json(url, {
        "offset": "0",
        "limit": str(limit),
        "filterBySentiment": "all",
        "sort": "date",
    })
    return _parse_user_reviews(data)


def _parse_critic_reviews(data: dict) -> list[ReviewItem]:
    items = (data.get("data") or {}).get("items") or []
    out = []
    for r in items:
        if not isinstance(r, dict):
            continue
        out.append(ReviewItem(
            kind="critic",
            author=r.get("author") or None,
            publication=r.get("publicationName") or None,
            score=r.get("score"),
            quote=r.get("quote") or None,
            review_date=r.get("date") or None,
            platform=r.get("platform") or None,
        ))
    return out


def _parse_user_reviews(data: dict) -> list[ReviewItem]:
    items = (data.get("data") or {}).get("items") or []
    out = []
    for r in items:
        if not isinstance(r, dict):
            continue
        out.append(ReviewItem(
            kind="user",
            author=r.get("author") or None,
            publication=None,
            score=r.get("score"),
            quote=r.get("quote") or None,
            review_date=r.get("date") or None,
            platform=r.get("platform") or None,
            thumbs_up=r.get("thumbsUp"),
            thumbs_down=r.get("thumbsDown"),
        ))
    return out
