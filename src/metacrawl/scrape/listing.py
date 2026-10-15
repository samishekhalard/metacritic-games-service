"""
Parsing a Metacritic listing page into game records.

Two listing shapes matter, and they carry the same item objects:

    /game/                                   the New Releases block
    /browse/game/all/all/all-time/new/       the paginated full list

Everything below is defensive about missing fields. Unreleased games — which
is most of what "new releases" means — have a null metascore, no reviews and
sometimes no description, and a scraper that assumes otherwise dies on its
first run at 03:00.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..payload import find_items, parse, scrape_image_urls


@dataclass
class PlatformScore:
    """One platform's scores. A game can ship on several, each scored apart."""

    name: str
    metascore: int | None = None
    critic_reviews: int = 0
    userscore: float | None = None
    user_reviews: int = 0


@dataclass
class GameSummary:
    """What a listing page tells us about a game, before we open its page."""

    external_id: int
    slug: str
    title: str
    url: str
    description: str | None = None
    release_date: str | None = None
    premiere_year: int | None = None
    rating: str | None = None
    image_url: str | None = None
    genres: list[str] = field(default_factory=list)
    platforms: list[PlatformScore] = field(default_factory=list)
    metascore: int | None = None
    critic_review_count: int = 0
    critic_sentiment: str | None = None

    @property
    def is_released(self) -> bool:
        return self.metascore is not None or self.critic_review_count > 0


def _image_url(image: dict | None, url_map: dict | None = None) -> str | None:
    """
    Return a cover URL for an image object.

    Prefers the hashed CDN URL scraped from server-rendered <img> tags
    (url_map, keyed by bucketPath) because imageUrl in the payload is always
    null and the composed resize/{width}/{height}/… form returns 403 — the
    CDN requires a content-hash token where width/height appear.
    """
    if not isinstance(image, dict):
        return None
    direct = image.get("imageUrl")
    if isinstance(direct, str) and direct.startswith("http"):
        return direct
    bucket_path = image.get("bucketPath")
    if not isinstance(bucket_path, str) or not bucket_path:
        return None
    if url_map and bucket_path in url_map:
        return url_map[bucket_path]
    # No hash available (API path has no HTML to scrape).  The composed
    # resize/{w}/{h}/… form always 403s; return None so the card shows a
    # placeholder instead of a broken image.  The detail fetch will supply
    # the real hashed URL and overwrite this None.
    return None


def _platforms(item: dict) -> list[PlatformScore]:
    scores: list[PlatformScore] = []
    for entry in item.get("platforms") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name") or entry.get("platform") or entry.get("slug")
        if not name:
            continue
        critic = entry.get("criticScoreSummary") or {}
        user = entry.get("userScoreSummary") or {}
        scores.append(
            PlatformScore(
                name=str(name),
                metascore=critic.get("score") if isinstance(critic, dict) else None,
                critic_reviews=(critic.get("reviewCount") or 0) if isinstance(critic, dict) else 0,
                userscore=user.get("score") if isinstance(user, dict) else None,
                user_reviews=(user.get("reviewCount") or 0) if isinstance(user, dict) else 0,
            )
        )
    return scores


def _genres(item: dict) -> list[str]:
    names = []
    for entry in item.get("genres") or []:
        if isinstance(entry, dict) and entry.get("name"):
            names.append(str(entry["name"]))
        elif isinstance(entry, str):
            names.append(entry)
    return names


def to_summary(item: dict, url_map: dict | None = None) -> GameSummary | None:
    """One payload item to a GameSummary. Returns None for non-game entries."""
    slug = item.get("slug")
    title = item.get("title")
    external_id = item.get("id")
    if not slug or not title or external_id is None:
        return None
    if item.get("type") and item["type"] != "game-title":
        return None

    critic = item.get("criticScoreSummary")
    critic = critic if isinstance(critic, dict) else {}

    return GameSummary(
        external_id=int(external_id),
        slug=str(slug),
        title=str(title),
        url=f"https://www.metacritic.com/game/{slug}/",
        description=item.get("description") or None,
        release_date=item.get("releaseDate") or None,
        premiere_year=item.get("premiereYear") or None,
        rating=item.get("rating") or None,
        image_url=_image_url(item.get("image"), url_map),
        genres=_genres(item),
        platforms=_platforms(item),
        metascore=critic.get("score"),
        critic_review_count=critic.get("reviewCount") or 0,
        critic_sentiment=critic.get("sentiment") or None,
    )


def parse_listing(html: str) -> list[GameSummary]:
    """A listing page to the games it advertises, in page order."""
    url_map = scrape_image_urls(html)
    items = find_items(parse(html))
    summaries = [to_summary(item, url_map) for item in items if isinstance(item, dict)]
    return [summary for summary in summaries if summary is not None]
