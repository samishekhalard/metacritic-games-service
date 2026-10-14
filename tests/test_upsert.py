"""
Tests for db/upsert.py.

Covers:
- insert on first call, update (not duplicate) on second
- multi-platform: N platforms in summary → N platform_score rows
- multi-genre: N genres → N game_genres rows
- platform rows replaced on re-upsert (no stale rows from prior version)
- game.html fixture: Elden Ring has 5 platforms in the product component
"""
from __future__ import annotations

from sqlalchemy import func, select, text

from metacrawl.db.models import CrawlRun, Game, PlatformScore, game_genres
from metacrawl.db.upsert import upsert_game
from metacrawl.payload import parse
from metacrawl.scrape.listing import GameSummary
from metacrawl.scrape.listing import PlatformScore as PSummary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summary(*, external_id=1, platforms=None, genres=None) -> GameSummary:
    return GameSummary(
        external_id=external_id,
        slug=f"game-{external_id}",
        title=f"Game {external_id}",
        url=f"https://www.metacritic.com/game/game-{external_id}/",
        platforms=platforms or [],
        genres=genres or [],
    )


def _platform_score_count(session, game_id: int) -> int:
    return session.query(PlatformScore).filter_by(game_id=game_id).count()


def _genre_count(session, game_id: int) -> int:
    return session.execute(
        select(func.count()).where(game_genres.c.game_id == game_id)
    ).scalar()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_first_upsert_creates_game(db):
    Session, _ = db
    with Session() as s:
        _, created = upsert_game(s, _summary())
        s.commit()
    assert created is True


def test_second_upsert_does_not_create_duplicate(db):
    Session, _ = db
    summary = _summary()
    with Session() as s:
        upsert_game(s, summary)
        s.commit()
    with Session() as s:
        _, created = upsert_game(s, summary)
        s.commit()
    assert created is False

    with Session() as s:
        count = s.query(Game).filter_by(external_id=1).count()
    assert count == 1


def test_upsert_updates_title_on_second_call(db):
    Session, _ = db
    with Session() as s:
        upsert_game(s, _summary(external_id=7))
        s.commit()
    updated = _summary(external_id=7)
    updated.title = "Updated Title"
    with Session() as s:
        upsert_game(s, updated)
        s.commit()
    with Session() as s:
        game = s.query(Game).filter_by(external_id=7).one()
    assert game.title == "Updated Title"


def test_first_seen_at_not_changed_on_update(db):
    Session, _ = db
    with Session() as s:
        game, _ = upsert_game(s, _summary(external_id=5))
        s.commit()
        first_seen = game.first_seen_at

    with Session() as s:
        upsert_game(s, _summary(external_id=5))
        s.commit()

    with Session() as s:
        game = s.query(Game).filter_by(external_id=5).one()
    assert game.first_seen_at == first_seen


# ---------------------------------------------------------------------------
# Multi-platform
# ---------------------------------------------------------------------------

def test_three_platforms_create_three_rows(db):
    Session, _ = db
    summary = _summary(platforms=[
        PSummary("PC", metascore=94),
        PSummary("PlayStation 5", metascore=96),
        PSummary("Xbox Series X", metascore=96),
    ])
    with Session() as s:
        game, _ = upsert_game(s, summary)
        s.commit()
        game_id = game.id

    with Session() as s:
        assert _platform_score_count(s, game_id) == 3


def test_single_platform_creates_one_row(db):
    Session, _ = db
    summary = _summary(platforms=[PSummary("PC", metascore=80)])
    with Session() as s:
        game, _ = upsert_game(s, summary)
        s.commit()
        game_id = game.id

    with Session() as s:
        assert _platform_score_count(s, game_id) == 1


def test_platform_rows_replaced_not_accumulated(db):
    """Re-upserting with different platforms must not accumulate stale rows."""
    Session, _ = db
    v1 = _summary(platforms=[PSummary("PC"), PSummary("PlayStation 5")])
    with Session() as s:
        game, _ = upsert_game(s, v1)
        s.commit()
        game_id = game.id

    # Second upsert: only one platform now.
    v2 = _summary(platforms=[PSummary("Xbox Series X")])
    with Session() as s:
        upsert_game(s, v2)
        s.commit()

    with Session() as s:
        rows = s.query(PlatformScore).filter_by(game_id=game_id).all()
    assert len(rows) == 1
    assert rows[0].platform == "Xbox Series X"


# ---------------------------------------------------------------------------
# Multi-genre
# ---------------------------------------------------------------------------

def test_two_genres_create_two_rows(db):
    Session, _ = db
    summary = _summary(genres=["Action RPG", "RPG"])
    with Session() as s:
        game, _ = upsert_game(s, summary)
        s.commit()
        game_id = game.id

    with Session() as s:
        assert _genre_count(s, game_id) == 2


def test_genre_rows_replaced_on_update(db):
    Session, _ = db
    with Session() as s:
        game, _ = upsert_game(s, _summary(genres=["Action RPG", "Shooter"]))
        s.commit()
        game_id = game.id

    with Session() as s:
        upsert_game(s, _summary(genres=["Puzzle"]))
        s.commit()

    with Session() as s:
        count = _genre_count(s, game_id)
    assert count == 1


# ---------------------------------------------------------------------------
# game.html fixture — verify 5-platform Elden Ring structure
# (This is read-only payload inspection; the detail upsert comes in step 3.)
# ---------------------------------------------------------------------------

def test_elden_ring_has_five_platforms_in_game_html(game_html):
    root = parse(game_html)
    page = root["data"]["loadPage:games:elden-ring:"]
    product = next(
        c for c in page["components"]
        if isinstance(c, dict)
        and (c.get("meta") or {}).get("componentName") == "product"
    )
    item = product["data"]["item"]
    platforms = item.get("platforms") or []
    assert len(platforms) == 5
    names = {p["name"] for p in platforms if isinstance(p, dict) and p.get("name")}
    assert "PlayStation 5" in names
    assert "PC" in names


def test_elden_ring_per_platform_scores(game_html):
    root = parse(game_html)
    page = root["data"]["loadPage:games:elden-ring:"]
    product = next(
        c for c in page["components"]
        if isinstance(c, dict)
        and (c.get("meta") or {}).get("componentName") == "product"
    )
    item = product["data"]["item"]
    scored = [
        p for p in (item.get("platforms") or [])
        if isinstance(p, dict)
        and isinstance(p.get("criticScoreSummary"), dict)
        and p["criticScoreSummary"].get("score") is not None
    ]
    # PC=94, Xbox Series X=96, PlayStation 5=96 are scored in the fixture
    assert len(scored) >= 3


def test_elden_ring_has_developer_and_publisher(game_html):
    root = parse(game_html)
    page = root["data"]["loadPage:games:elden-ring:"]
    product = next(
        c for c in page["components"]
        if isinstance(c, dict)
        and (c.get("meta") or {}).get("componentName") == "product"
    )
    item = product["data"]["item"]
    companies = (item.get("production") or {}).get("companies") or []
    types = {c["typeName"] for c in companies if isinstance(c, dict) and c.get("typeName")}
    assert "Developer" in types
    assert "Publisher" in types


# ---------------------------------------------------------------------------
# CrawlRun model sanity check
# ---------------------------------------------------------------------------

def test_crawl_run_can_be_inserted(db):
    from datetime import datetime
    Session, _ = db
    with Session() as s:
        run = CrawlRun(
            started_at=datetime.utcnow(),
            trigger="manual",
            source="new-releases",
            page=1,
            status="done",
            games_seen=20,
            games_created=20,
            games_updated=0,
            errors=0,
        )
        s.add(run)
        s.commit()
        assert run.id is not None
