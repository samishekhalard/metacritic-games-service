"""
Crawl logic and CLI entry point.

`do_crawl` is the unit of work: decide which source to use (new-releases or
browse), fetch one page, upsert, detail-fetch games not yet processed today,
generate LLM summaries, write a CrawlRun row.

NEVER call `do_crawl` inside a FastAPI request handler — the rate limiter
sleeps for ~20 s per run (plus LLM latency).  See worker.py.

DAY LOGIC (UTC)
───────────────
First run of a UTC day   → New Releases carousel (20 scored games)
Subsequent runs that day → one page of the browse list (24 games)

"Processed today" = last_crawled_at >= midnight UTC today.
Games already processed today are upserted (keep metadata fresh) but their
detail fetch and summary generation are skipped to avoid redundant work and
LLM cost.

BROWSE CURSOR
─────────────
Stored in crawl_runs.next_browse_url after every successful browse run.
Survives restarts because it lives in SQLite, not in process memory.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from .api.detail import fetch_critic_reviews, fetch_detail, fetch_user_reviews
from .api.finder import fetch_browse_page, fetch_browse_url, fetch_new_releases
from .db.models import CrawlRun, Game
from .db.session import init_db, make_engine, make_session_factory
from .db.upsert import apply_detail, upsert_game
from .embed import EmbedCallable, call_embedder, embed_game
from .http import HttpClient
from .llm import call_llm
from .schedule import decide_crawl_source
from .summarise import generate_summaries

log = logging.getLogger(__name__)


def _today_start() -> datetime:
    d = datetime.now(timezone.utc).date()
    return datetime(d.year, d.month, d.day)


def do_crawl(
    session_factory,
    *,
    trigger: str = "manual",
    _embedder: "EmbedCallable | None" = None,
) -> CrawlRun:
    """
    One run: decide source, fetch listing, detail-fetch new games, summarise.
    Always writes a CrawlRun row; sets status='failed' if an exception escapes.
    Returns the completed CrawlRun (detached).
    """
    source, browse_url, page = decide_crawl_source(session_factory)

    with session_factory() as session:
        run = CrawlRun(
            started_at=datetime.utcnow(),
            trigger=trigger,
            source=source,
            page=page,
            status="running",
            games_seen=0,
            games_created=0,
            games_updated=0,
            games_skipped=0,
            errors=0,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    embedder: EmbedCallable = _embedder if _embedder is not None else call_embedder

    created = updated = skipped = errors = 0
    games_seen = 0
    next_url: str | None = None
    status = "done"
    today = _today_start()

    try:
        with HttpClient() as client:
            # ── 1. Listing fetch ────────────────────────────────────────────
            if source == "new-releases":
                summaries = fetch_new_releases(client, limit=20)
            else:
                if browse_url:
                    summaries, next_url = fetch_browse_url(client, browse_url)
                else:
                    summaries, next_url = fetch_browse_page(client, offset=0, limit=24)
            games_seen = len(summaries)

            # ── 2. Listing upsert ───────────────────────────────────────────
            with session_factory() as session:
                for summary in summaries:
                    try:
                        _, is_new = upsert_game(session, summary)
                        if is_new:
                            created += 1
                        else:
                            updated += 1
                    except Exception:
                        log.exception("upsert failed for %s", summary.slug)
                        errors += 1
                session.commit()

            # ── 3. Detail fetch + summaries (skip if processed today) ───────
            with session_factory() as session:
                for summary in summaries:
                    game = session.query(Game).filter_by(slug=summary.slug).first()
                    if game is None:
                        continue

                    # Already processed today — skip expensive steps.
                    if game.last_crawled_at and game.last_crawled_at >= today:
                        # Still check: was it processed THIS run (just upserted)?
                        # If first_seen_at == last_crawled_at it's brand new now.
                        if game.first_seen_at != game.last_crawled_at:
                            skipped += 1
                            continue

                    try:
                        detail = fetch_detail(client, summary.slug)
                        critic_revs = fetch_critic_reviews(client, summary.slug, limit=50)
                        user_revs = fetch_user_reviews(client, summary.slug, limit=50)
                        if critic_revs:
                            detail.critic_reviews = critic_revs
                        if user_revs:
                            detail.user_reviews = user_revs
                        apply_detail(session, game, detail)
                        session.flush()

                        embed_game(session, game, embedder)
                        session.flush()

                        generate_summaries(session, game, call_llm)

                    except Exception:
                        log.exception("detail/summary failed for %s", summary.slug)
                        errors += 1

                session.commit()

    except Exception:
        status = "failed"
        errors += 1
        log.exception("crawl run %s failed", run_id)
        raise

    finally:
        with session_factory() as session:
            run = session.get(CrawlRun, run_id)
            run.finished_at = datetime.utcnow()
            run.status = status
            run.games_seen = games_seen
            run.games_created = created
            run.games_updated = updated
            run.games_skipped = skipped
            run.errors = errors
            if source == "browse":
                run.next_browse_url = next_url
            session.commit()
            session.refresh(run)
            session.expunge(run)

    return run


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    engine = make_engine()
    init_db(engine)
    Session = make_session_factory(engine)

    run = do_crawl(Session)

    print(
        f"Done: {run.games_created} created, {run.games_updated} updated, "
        f"{run.games_skipped} skipped, {run.errors} errors  ({run.status}).\n"
    )
    _print_games(engine)
    _print_run(run)


def _print_games(engine) -> None:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT external_id, title, metascore, release_date, rating "
            "FROM games "
            "ORDER BY CASE WHEN release_date IS NULL THEN 1 ELSE 0 END, "
            "         release_date DESC "
            "LIMIT 20"
        )).fetchall()

    if not rows:
        print("(no games in database)")
        return

    print(f"{'ext_id':>12}  {'score':>5}  {'release':>10}  {'rating':>5}  title")
    print("─" * 90)
    for r in rows:
        ext_id, title, score, rel, rating = r
        print(
            f"{ext_id:>12}  {str(score or '–'):>5}  "
            f"{str(rel or '–'):>10}  {str(rating or '–'):>5}  {title}"
        )


def _print_run(run: CrawlRun) -> None:
    print()
    print("crawl_run row:")
    print(f"  id={run.id}  trigger={run.trigger}  source={run.source}  page={run.page}")
    print(f"  started={run.started_at}  finished={run.finished_at}")
    print(
        f"  seen={run.games_seen}  created={run.games_created}  "
        f"updated={run.games_updated}  skipped={run.games_skipped}  "
        f"errors={run.errors}  status={run.status}"
    )
    if run.next_browse_url:
        print(f"  next_browse_url={run.next_browse_url[:80]}…")


if __name__ == "__main__":
    main()
