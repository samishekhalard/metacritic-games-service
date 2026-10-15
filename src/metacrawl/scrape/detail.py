"""
Parsing a Metacritic game detail page (game.html fixture).

All component lookups go by meta.componentName, never by index.
The BRIEF verified this is stable across fetches; the index order is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..payload import parse, scrape_image_urls


@dataclass
class PlatformDetail:
    name: str
    metascore: int | None = None
    critic_reviews: int = 0
    userscore: float | None = None
    user_reviews: int = 0


@dataclass
class ReviewItem:
    kind: str                          # 'critic' | 'user'
    author: str | None
    publication: str | None
    score: int | None
    quote: str | None
    review_date: str | None
    platform: str | None = None
    thumbs_up: int | None = None
    thumbs_down: int | None = None


@dataclass
class GameDetail:
    slug: str
    title: str | None = None
    description: str | None = None
    release_date: str | None = None
    rating: str | None = None
    developer: str | None = None
    publisher: str | None = None
    cover_url: str | None = None
    video_url: str | None = None
    metascore: int | None = None
    userscore: float | None = None
    critic_review_count: int = 0
    user_review_count: int = 0
    platforms: list[PlatformDetail] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    critic_reviews: list[ReviewItem] = field(default_factory=list)
    user_reviews: list[ReviewItem] = field(default_factory=list)


def _by_name(components: list, name: str) -> dict | None:
    for c in components:
        if isinstance(c, dict) and (c.get("meta") or {}).get("componentName") == name:
            return c
    return None


def _platforms(item: dict) -> list[PlatformDetail]:
    out = []
    for p in (item.get("platforms") or []):
        if not isinstance(p, dict):
            continue
        name = p.get("name") or p.get("slug")
        if not name:
            continue
        cs = p.get("criticScoreSummary") or {}
        us = p.get("userScoreSummary") or {}
        out.append(PlatformDetail(
            name=str(name),
            metascore=cs.get("score") if isinstance(cs, dict) else None,
            critic_reviews=(cs.get("reviewCount") or 0) if isinstance(cs, dict) else 0,
            userscore=us.get("score") if isinstance(us, dict) else None,
            user_reviews=(us.get("reviewCount") or 0) if isinstance(us, dict) else 0,
        ))
    return out


def _genres(item: dict) -> list[str]:
    return [
        g["name"] for g in (item.get("genres") or [])
        if isinstance(g, dict) and g.get("name")
    ]


def _companies(item: dict) -> tuple[str | None, str | None]:
    """Return (developer_name, publisher_name) from production.companies."""
    developer = publisher = None
    for company in ((item.get("production") or {}).get("companies") or []):
        if not isinstance(company, dict):
            continue
        kind = company.get("typeName")
        name = company.get("name")
        if kind == "Developer" and developer is None:
            developer = name
        elif kind == "Publisher" and publisher is None:
            publisher = name
    return developer, publisher


def _video_url(item: dict) -> str | None:
    video = item.get("video")
    if not isinstance(video, dict):
        return None
    # embedUrl is an iframe-embeddable JW Player URL; url is null in the fixture.
    return video.get("embedUrl") or video.get("manifestUrl") or None


def _cover_url(item: dict, url_map: dict | None = None) -> str | None:
    images = item.get("images") or []
    for img in images:
        if not isinstance(img, dict):
            continue
        url = img.get("imageUrl")
        if isinstance(url, str) and url.startswith("http"):
            return url
        bucket = img.get("bucketPath")
        if isinstance(bucket, str) and bucket:
            if url_map and bucket in url_map:
                return url_map[bucket]
            w = img.get("width") or 460
            h = img.get("height") or 215
            return f"https://www.metacritic.com/a/img/resize/{w}/{h}/catalog{bucket}"
    return None


def _critic_reviews(comp: dict) -> tuple[list[ReviewItem], int]:
    data = comp.get("data") or {}
    items = data.get("items") or []
    total = data.get("totalResults") or 0
    reviews = []
    for r in items:
        if not isinstance(r, dict):
            continue
        reviews.append(ReviewItem(
            kind="critic",
            author=r.get("author") or None,
            publication=r.get("publicationName") or None,
            score=r.get("score"),
            quote=r.get("quote") or None,
            review_date=r.get("date") or None,
            platform=r.get("platform") or None,
        ))
    return reviews, total


def _user_reviews(comp: dict) -> tuple[list[ReviewItem], int]:
    data = comp.get("data") or {}
    items = data.get("items") or []
    total = data.get("totalResults") or 0
    reviews = []
    for r in items:
        if not isinstance(r, dict):
            continue
        reviews.append(ReviewItem(
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
    return reviews, total


def parse_detail(html: str, slug: str) -> GameDetail:
    """
    Parse a game detail page into a GameDetail.

    `slug` is used to locate the correct loadPage key; it must match the
    game's Metacritic slug exactly.
    """
    url_map = scrape_image_urls(html)
    root = parse(html)
    page_key = f"loadPage:games:{slug}:"
    page = (root.get("data") or {}).get(page_key)
    if page is None:
        # Fallback: find any loadPage:games:…: key
        for k, v in (root.get("data") or {}).items():
            if k.startswith("loadPage:games:") and isinstance(v, dict):
                page = v
                break

    if page is None:
        # Handle review sub-pages (critic-reviews, user-reviews) whose keys
        # are loadPage:games-critic-reviews:… and loadPage:games-user-reviews:…
        for k, v in (root.get("data") or {}).items():
            if k.startswith("loadPage:games") and isinstance(v, dict):
                page = v
                break

    if page is None or not isinstance(page.get("components"), list):
        return GameDetail(slug=slug)

    comps = page["components"]
    detail = GameDetail(slug=slug)

    # --- product component ---
    prod_comp = _by_name(comps, "product")
    if prod_comp:
        item = (prod_comp.get("data") or {}).get("item") or {}
        detail.title = item.get("title")
        detail.description = item.get("description") or None
        detail.release_date = item.get("releaseDate") or None
        detail.rating = item.get("rating") or None
        detail.developer, detail.publisher = _companies(item)
        detail.video_url = _video_url(item)
        detail.cover_url = _cover_url(item, url_map)
        detail.platforms = _platforms(item)
        detail.genres = _genres(item)

    # --- critic-score-summary ---
    cs_comp = _by_name(comps, "critic-score-summary")
    if cs_comp:
        cs = (cs_comp.get("data") or {}).get("item") or {}
        detail.metascore = cs.get("score")
        detail.critic_review_count = cs.get("reviewCount") or 0

    # --- user-score-summary ---
    us_comp = _by_name(comps, "user-score-summary")
    if us_comp:
        us = (us_comp.get("data") or {}).get("item") or {}
        detail.userscore = us.get("score")
        detail.user_review_count = us.get("reviewCount") or 0

    # --- critic-reviews ---
    cr_comp = _by_name(comps, "critic-reviews")
    if cr_comp:
        detail.critic_reviews, _ = _critic_reviews(cr_comp)

    # --- user-reviews ---
    ur_comp = _by_name(comps, "user-reviews")
    if ur_comp:
        detail.user_reviews, _ = _user_reviews(ur_comp)

    return detail
