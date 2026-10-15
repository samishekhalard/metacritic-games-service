"""
Tests for scrape/detail.py and api/detail.py — offline, fixture-based.

Covers: parse_detail from game.html, critic/user review parsing from
their respective page fixtures, apply_detail upsert integration.
"""
from __future__ import annotations

import json

import pytest

from metacrawl.scrape.detail import GameDetail, parse_detail
from metacrawl.api.detail import _parse_critic_reviews, _parse_user_reviews
from metacrawl.db.upsert import apply_detail, upsert_game
from metacrawl.db.models import Game, PlatformScore, Review, game_genres
from metacrawl.scrape.listing import GameSummary
from sqlalchemy import func, select


# ---------------------------------------------------------------------------
# parse_detail — product component
# ---------------------------------------------------------------------------

def test_parse_detail_slug(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert d.slug == "elden-ring"


def test_parse_detail_title(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert d.title == "Elden Ring"


def test_parse_detail_developer(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert d.developer == "From Software"


def test_parse_detail_publisher(game_html):
    d = parse_detail(game_html, "elden-ring")
    # Bandai Namco is listed first as Publisher
    assert d.publisher == "Bandai Namco Games"


def test_parse_detail_video_url(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert d.video_url is not None
    assert "jwplayer" in d.video_url or "cdn" in d.video_url


def test_parse_detail_cover_url(game_html):
    import re
    d = parse_detail(game_html, "elden-ring")
    assert d.cover_url is not None
    assert re.match(
        r"https://www\.metacritic\.com/a/img/resize/[a-f0-9]{40}/catalog/",
        d.cover_url,
    ), f"expected hashed CDN URL, got: {d.cover_url}"


def test_parse_detail_genres(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert "Action RPG" in d.genres


def test_parse_detail_five_platforms(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert len(d.platforms) == 5


def test_parse_detail_platform_names(game_html):
    d = parse_detail(game_html, "elden-ring")
    names = {p.name for p in d.platforms}
    assert "PlayStation 5" in names
    assert "PC" in names
    assert "Xbox Series X" in names


def test_parse_detail_per_platform_scores(game_html):
    d = parse_detail(game_html, "elden-ring")
    scored = {p.name: p.metascore for p in d.platforms if p.metascore is not None}
    assert scored.get("PlayStation 5") == 96
    assert scored.get("PC") == 94
    assert scored.get("Xbox Series X") == 96


def test_parse_detail_metascore(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert d.metascore == 96


def test_parse_detail_release_date(game_html):
    d = parse_detail(game_html, "elden-ring")
    assert d.release_date == "2022-02-25"


# ---------------------------------------------------------------------------
# critic-reviews fixture
# ---------------------------------------------------------------------------

def test_parse_detail_critic_reviews_from_page(critic_reviews_html):
    d = parse_detail(critic_reviews_html, "elden-ring")
    # The critic-reviews page fixture has reviews in its component
    assert len(d.critic_reviews) > 0


def test_critic_review_fields(critic_reviews_html):
    d = parse_detail(critic_reviews_html, "elden-ring")
    r = d.critic_reviews[0]
    assert r.kind == "critic"
    assert r.score is not None
    assert r.quote
    assert r.publication


# ---------------------------------------------------------------------------
# user-reviews fixture
# ---------------------------------------------------------------------------

def test_parse_detail_user_reviews_from_page(user_reviews_html):
    d = parse_detail(user_reviews_html, "elden-ring")
    assert len(d.user_reviews) > 0


def test_user_review_fields(user_reviews_html):
    d = parse_detail(user_reviews_html, "elden-ring")
    r = d.user_reviews[0]
    assert r.kind == "user"
    assert r.score is not None
    assert r.quote


# ---------------------------------------------------------------------------
# JSON reviews API parsers (_parse_critic_reviews / _parse_user_reviews)
# ---------------------------------------------------------------------------

def _make_critic_api_data(n: int) -> dict:
    return {"data": {"items": [
        {"quote": f"review {i}", "score": 80 + i, "author": f"Critic {i}",
         "publicationName": f"Pub {i}", "date": "2022-03-01", "platform": "PC"}
        for i in range(n)
    ]}}


def _make_user_api_data(n: int) -> dict:
    return {"data": {"items": [
        {"quote": f"user review {i}", "score": 7 + (i % 3), "author": f"User {i}",
         "date": "2022-03-02", "platform": "PC", "spoiler": False,
         "thumbsUp": 0, "thumbsDown": 0}
        for i in range(n)
    ]}}


def test_parse_critic_reviews_count():
    revs = _parse_critic_reviews(_make_critic_api_data(10))
    assert len(revs) == 10


def test_parse_critic_review_kind():
    revs = _parse_critic_reviews(_make_critic_api_data(1))
    assert revs[0].kind == "critic"


def test_parse_user_reviews_count():
    revs = _parse_user_reviews(_make_user_api_data(5))
    assert len(revs) == 5


def test_parse_user_review_kind():
    revs = _parse_user_reviews(_make_user_api_data(1))
    assert revs[0].kind == "user"


def test_parse_reviews_empty_data():
    assert _parse_critic_reviews({}) == []
    assert _parse_user_reviews({"data": {}}) == []


# ---------------------------------------------------------------------------
# apply_detail integration
# ---------------------------------------------------------------------------

def _insert_bare_game(session, external_id: int = 42, slug: str = "test-game"):
    summary = GameSummary(
        external_id=external_id, slug=slug, title="Old Title",
        url=f"https://www.metacritic.com/game/{slug}/",
    )
    game, _ = upsert_game(session, summary)
    session.flush()
    return game


def test_apply_detail_sets_developer(db):
    Session, _ = db
    with Session() as s:
        game = _insert_bare_game(s)
        detail = GameDetail(slug="test-game", developer="TestDev", publisher="TestPub")
        apply_detail(s, game, detail)
        s.commit()
        game_id = game.id

    with Session() as s:
        g = s.get(Game, game_id)
        assert g.developer == "TestDev"
        assert g.publisher == "TestPub"


def test_apply_detail_sets_video_url(db):
    Session, _ = db
    with Session() as s:
        game = _insert_bare_game(s, external_id=43)
        detail = GameDetail(slug="test-game", video_url="https://cdn.jwplayer.com/players/ABC.html")
        apply_detail(s, game, detail)
        s.commit()
        game_id = game.id

    with Session() as s:
        g = s.get(Game, game_id)
        assert g.video_url == "https://cdn.jwplayer.com/players/ABC.html"


def test_apply_detail_replaces_platform_scores(db):
    Session, _ = db
    from metacrawl.scrape.detail import PlatformDetail

    with Session() as s:
        game = _insert_bare_game(s, external_id=44)
        detail = GameDetail(
            slug="test-game",
            platforms=[
                PlatformDetail("PC", metascore=94, critic_reviews=3),
                PlatformDetail("PlayStation 5", metascore=96, critic_reviews=10),
                PlatformDetail("Xbox Series X", metascore=96, critic_reviews=8),
            ],
        )
        apply_detail(s, game, detail)
        s.commit()
        game_id = game.id

    with Session() as s:
        rows = s.query(PlatformScore).filter_by(game_id=game_id).all()
        assert len(rows) == 3
        scored = {r.platform: r.metascore for r in rows}
        assert scored["PC"] == 94
        assert scored["PlayStation 5"] == 96


def test_apply_detail_inserts_reviews(db):
    Session, _ = db
    from metacrawl.scrape.detail import ReviewItem

    with Session() as s:
        game = _insert_bare_game(s, external_id=45)
        detail = GameDetail(
            slug="test-game",
            critic_reviews=[
                ReviewItem("critic", "Alice", "IGN", 90, "Great game", "2022-03-01"),
                ReviewItem("critic", "Bob", "Eurogamer", 85, "Good game", "2022-03-02"),
            ],
            user_reviews=[
                ReviewItem("user", "Charlie", None, 9, "Loved it", "2022-03-05"),
            ],
        )
        apply_detail(s, game, detail)
        s.commit()
        game_id = game.id

    with Session() as s:
        total = s.query(Review).filter_by(game_id=game_id).count()
        critic_count = s.query(Review).filter_by(game_id=game_id, kind="critic").count()
        user_count = s.query(Review).filter_by(game_id=game_id, kind="user").count()
        assert total == 3
        assert critic_count == 2
        assert user_count == 1


def test_apply_detail_reviews_replaced_on_reapply(db):
    """Re-applying detail replaces reviews rather than accumulating them."""
    Session, _ = db
    from metacrawl.scrape.detail import ReviewItem

    def _detail_with_n_reviews(n):
        return GameDetail(
            slug="test-game",
            critic_reviews=[
                ReviewItem("critic", f"Critic {i}", "Pub", 80, f"Quote {i}", "2022-03-01")
                for i in range(n)
            ],
        )

    with Session() as s:
        game = _insert_bare_game(s, external_id=46)
        apply_detail(s, game, _detail_with_n_reviews(3))
        s.commit()
        game_id = game.id

    with Session() as s:
        game = s.get(Game, game_id)
        apply_detail(s, game, _detail_with_n_reviews(2))
        s.commit()

    with Session() as s:
        count = s.query(Review).filter_by(game_id=game_id).count()
    assert count == 2


def test_apply_detail_on_elden_ring_fixture(db, game_html, critic_reviews_html):
    """End-to-end: insert via listing, enrich with game.html, check all detail fields."""
    Session, _ = db

    summary = GameSummary(
        external_id=1300501979,
        slug="elden-ring",
        title="Elden Ring (listing)",
        url="https://www.metacritic.com/game/elden-ring/",
    )

    with Session() as s:
        game, _ = upsert_game(s, summary)
        s.commit()
        game_id = game.id

    detail = parse_detail(game_html, "elden-ring")
    # Merge critic reviews parsed from the reviews page fixture
    cr_detail = parse_detail(critic_reviews_html, "elden-ring")
    detail.critic_reviews = cr_detail.critic_reviews

    with Session() as s:
        game = s.get(Game, game_id)
        apply_detail(s, game, detail)
        s.commit()

    with Session() as s:
        g = s.get(Game, game_id)
        assert g.title == "Elden Ring"
        assert g.developer == "From Software"
        assert g.video_url is not None
        assert g.metascore == 96

        plat_count = s.query(PlatformScore).filter_by(game_id=game_id).count()
        assert plat_count == 5

        review_count = s.query(Review).filter_by(game_id=game_id, kind="critic").count()
        assert review_count > 0
