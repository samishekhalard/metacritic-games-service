"""
Tests for schedule.py — day logic and browse cursor.

All tests use an in-memory DB and a fixed "today" injected via monkeypatch
so they are independent of the real UTC clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from metacrawl.db.models import CrawlRun
from metacrawl.schedule import decide_crawl_source, _today_start


# ── helpers ──────────────────────────────────────────────────────────────────

def _add_run(session, *, source, status, started_at, page=1, next_browse_url=None):
    run = CrawlRun(
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=90),
        trigger="schedule",
        source=source,
        page=page,
        status=status,
        games_seen=20,
        games_created=10,
        games_updated=10,
        games_skipped=0,
        errors=0,
        next_browse_url=next_browse_url,
    )
    session.add(run)
    session.commit()
    return run


def _today() -> datetime:
    """Midnight UTC today (naive)."""
    return _today_start()


def _yesterday() -> datetime:
    return _today() - timedelta(days=1)


# ── decide_crawl_source ───────────────────────────────────────────────────────

def test_empty_db_returns_new_releases(db):
    Session, _ = db
    source, url, page = decide_crawl_source(Session)
    assert source == "new-releases"
    assert url is None
    assert page == 1


def test_no_nr_run_today_returns_new_releases(db):
    """A done NR run from yesterday should not count as today's."""
    Session, _ = db
    with Session() as s:
        _add_run(s, source="new-releases", status="done", started_at=_yesterday())

    source, url, page = decide_crawl_source(Session)
    assert source == "new-releases"


def test_failed_nr_run_today_still_returns_new_releases(db):
    """A failed NR run does not count; we try again."""
    Session, _ = db
    with Session() as s:
        _add_run(s, source="new-releases", status="failed", started_at=_today())

    source, url, page = decide_crawl_source(Session)
    assert source == "new-releases"


def test_done_nr_run_today_returns_browse(db):
    Session, _ = db
    with Session() as s:
        _add_run(s, source="new-releases", status="done", started_at=_today())

    source, url, page = decide_crawl_source(Session)
    assert source == "browse"


def test_first_browse_has_no_url(db):
    Session, _ = db
    with Session() as s:
        _add_run(s, source="new-releases", status="done", started_at=_today())
        # No prior browse run

    source, url, page = decide_crawl_source(Session)
    assert source == "browse"
    assert url is None
    assert page == 1


def test_browse_cursor_from_last_successful_run(db):
    Session, _ = db
    next_url = "https://backend.metacritic.com/finder/metacritic/web?offset=24"
    with Session() as s:
        _add_run(s, source="new-releases", status="done", started_at=_today())
        _add_run(
            s, source="browse", status="done",
            started_at=_today() + timedelta(hours=1),
            page=1, next_browse_url=next_url,
        )

    source, url, page = decide_crawl_source(Session)
    assert source == "browse"
    assert url == next_url
    assert page == 2


def test_browse_cursor_ignores_failed_runs(db):
    """A failed browse run's next_browse_url is not used."""
    Session, _ = db
    with Session() as s:
        _add_run(s, source="new-releases", status="done", started_at=_today())
        _add_run(
            s, source="browse", status="done",
            started_at=_today() + timedelta(hours=1),
            page=1, next_browse_url="https://example.com/page2",
        )
        _add_run(
            s, source="browse", status="failed",
            started_at=_today() + timedelta(hours=2),
            page=2, next_browse_url="https://example.com/page3",
        )

    source, url, page = decide_crawl_source(Session)
    assert source == "browse"
    assert url == "https://example.com/page2"   # from last *done* run
    assert page == 2


def test_browse_page_increments(db):
    Session, _ = db
    with Session() as s:
        _add_run(s, source="new-releases", status="done", started_at=_today())
        for i in range(1, 4):
            _add_run(
                s, source="browse", status="done",
                started_at=_today() + timedelta(hours=i),
                page=i, next_browse_url=f"https://example.com/page{i+1}",
            )

    _, _, page = decide_crawl_source(Session)
    assert page == 4


def test_new_day_resets_to_new_releases(db):
    """After midnight UTC, the first run of the new day is New Releases."""
    Session, _ = db
    yesterday = _yesterday()
    with Session() as s:
        # Yesterday's runs
        _add_run(s, source="new-releases", status="done", started_at=yesterday)
        for i in range(1, 5):
            _add_run(
                s, source="browse", status="done",
                started_at=yesterday + timedelta(hours=i),
                page=i, next_browse_url=f"https://example.com/page{i+1}",
            )

    source, _, _ = decide_crawl_source(Session)
    assert source == "new-releases"


def test_nr_run_from_yesterday_does_not_satisfy_today(db):
    """An NR run at 23:59 UTC yesterday does not satisfy today's NR requirement."""
    Session, _ = db
    with Session() as s:
        # 1 second before today's midnight
        just_before = _today() - timedelta(seconds=1)
        _add_run(s, source="new-releases", status="done", started_at=just_before)

    source, _, _ = decide_crawl_source(Session)
    assert source == "new-releases"
