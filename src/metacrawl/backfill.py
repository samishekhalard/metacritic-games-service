"""
Backfill command — walk games already in the database and complete them:
detail page, reviews, embeddings, and LLM summaries.

WHY THIS EXISTS
───────────────
The regular hourly crawl processes only games seen on the browse/new-releases
listing.  Games that appeared during periods when the LLM or embedder was
offline may have reviews but no summaries, or may be missing developer info
that only appears on the detail page.  This command fills those gaps
without touching the crawl machinery.

RESUMABLE
─────────
Each game is committed in its own transaction.  If the command is interrupted
(Ctrl-C, timeout, crash) the next run picks up from the next unprocessed
game — already-complete games are detected cheaply from DB state and skipped.

PRIORITY ORDER
──────────────
  1. Games with a metascore AND a description (released, has reviewable text)
  2. Everything else (unreleased, null score, no description yet)
  Within each tier: highest metascore first, then most-recent release.

COMMAND LINE
────────────
  PYTHONPATH=src python -m metacrawl.backfill [--limit N] [--only-missing-summaries]
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import and_, case, select

from .api.detail import fetch_critic_reviews, fetch_detail, fetch_user_reviews
from .db.models import Game, Review, Summary
from .db.upsert import apply_detail
from .embed import call_embedder, embed_game
from .http import HttpClient
from .llm import call_llm
from .summarise import generate_summaries

log = logging.getLogger(__name__)


@dataclass
class GameResult:
    slug: str
    critic_reviews: int = 0
    user_reviews: int = 0
    critic_written: bool = False
    user_written: bool = False
    llm_errors: int = 0
    skipped_no_reviews: bool = False   # unreleased: metascore IS NULL, expected
    scored_no_reviews: bool = False    # has metascore but MC returned 0 reviews
    already_complete: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _priority_query(session, only_missing_summaries: bool):
    """
    Return a query for Game rows in processing priority order.

    Priority 0 (first): has both metascore and description.
    Priority 1 (later): anything else.
    Within each priority tier: metascore DESC NULLS LAST, then by id.

    Both modes pre-filter to games that actually need work, so the limit
    is consumed only by games with genuine gaps (no wasted API calls on
    already-complete entries).
    """
    priority = case(
        (and_(Game.metascore.isnot(None), Game.description.isnot(None)), 0),
        else_=1,
    )
    query = (
        session.query(Game)
        .order_by(priority, Game.metascore.desc().nullslast(), Game.id)
    )

    has_critic_rev = session.query(Review.game_id).filter(Review.kind == "critic")
    has_user_rev   = session.query(Review.game_id).filter(Review.kind == "user")
    has_critic_sum = session.query(Summary.game_id).filter(Summary.kind == "critic")
    has_user_sum   = session.query(Summary.game_id).filter(Summary.kind == "user")

    if only_missing_summaries:
        # Games with reviews of a kind but no summary for that kind.
        needs_critic = and_(Game.id.in_(has_critic_rev), ~Game.id.in_(has_critic_sum))
        needs_user   = and_(Game.id.in_(has_user_rev),   ~Game.id.in_(has_user_sum))
        query = query.filter(needs_critic | needs_user)
    else:
        # Full backfill: only games that have actual gaps.
        # A game is complete when it has a description AND summaries exist for
        # every review kind that has reviews.  Exclude those; include everything else.
        needs_work = (
            Game.description.is_(None)                                       # detail not fetched
            | (Game.id.in_(has_critic_rev) & ~Game.id.in_(has_critic_sum))   # critic summary missing
            | (Game.id.in_(has_user_rev)   & ~Game.id.in_(has_user_sum))     # user summary missing
        )
        query = query.filter(needs_work)

    return query


def _process_game(
    session,
    game: Game,
    client: HttpClient,
    only_missing_summaries: bool,
) -> GameResult:
    """Process one game: detail + reviews + summaries.  Idempotent."""
    slug = game.slug

    if not only_missing_summaries:
        try:
            detail = fetch_detail(client, slug)
            critic_revs = fetch_critic_reviews(client, slug, limit=50)
            user_revs = fetch_user_reviews(client, slug, limit=50)
            if critic_revs:
                detail.critic_reviews = critic_revs
            if user_revs:
                detail.user_reviews = user_revs
            apply_detail(session, game, detail)
            session.flush()

            outcome = embed_game(session, game, call_embedder)
            if outcome.stored:
                session.flush()
        except Exception as exc:
            log.exception("detail/review fetch failed for %s", slug)
            return GameResult(slug=slug, error=str(exc)[:120])

    critic_n: int = session.query(Review).filter_by(game_id=game.id, kind="critic").count()
    user_n: int = session.query(Review).filter_by(game_id=game.id, kind="user").count()

    if critic_n == 0 and user_n == 0:
        if game.metascore is not None:
            return GameResult(slug=slug, scored_no_reviews=True)
        return GameResult(slug=slug, skipped_no_reviews=True)

    existing_kinds = set(
        session.execute(
            select(Summary.kind).where(Summary.game_id == game.id)
        ).scalars().all()
    )
    need_critic = critic_n > 0 and "critic" not in existing_kinds
    need_user = user_n > 0 and "user" not in existing_kinds

    if not need_critic and not need_user:
        return GameResult(
            slug=slug,
            critic_reviews=critic_n,
            user_reviews=user_n,
            already_complete=True,
        )

    c_written, u_written, llm_errs = generate_summaries(
        session, game, call_llm, skip_existing=True
    )
    return GameResult(
        slug=slug,
        critic_reviews=critic_n,
        user_reviews=user_n,
        critic_written=bool(c_written),
        user_written=bool(u_written),
        llm_errors=llm_errs,
    )


def _print_result(idx: int, total: int, r: GameResult) -> None:
    prefix = f"[{idx:4d}/{total}] {r.slug:<45}"
    if r.error:
        print(f"{prefix} ERROR: {r.error}", flush=True)
    elif r.scored_no_reviews:
        print(f"{prefix} scored but no reviews (metascore present, MC returned 0)", flush=True)
    elif r.skipped_no_reviews:
        print(f"{prefix} skipped (unreleased, no reviews)", flush=True)
    elif r.already_complete:
        print(f"{prefix} {r.critic_reviews}c+{r.user_reviews}u  already complete", flush=True)
    else:
        c = "critic✓" if r.critic_written else ("critic–" if r.critic_reviews == 0 else "critic✓had")
        u = "user✓" if r.user_written else ("user–" if r.user_reviews == 0 else "user✓had")
        llm = f"  [{r.llm_errors} llm_err]" if r.llm_errors else ""
        print(f"{prefix} {r.critic_reviews}c+{r.user_reviews}u  {c} {u}{llm}", flush=True)


def run_backfill(
    session_factory,
    *,
    limit: Optional[int] = None,
    only_missing_summaries: bool = False,
) -> list:
    """
    Walk games in priority order and fill gaps.  Commits after each game.
    Returns the list of GameResult objects (one per processed game).
    """
    with session_factory() as session:
        query = _priority_query(session, only_missing_summaries)
        if limit is not None:
            query = query.limit(limit)
        # Load IDs only — game objects will be reloaded in fresh sessions below.
        game_ids = [g.id for g in query.all()]

    total = len(game_ids)
    if total == 0:
        print("Nothing to backfill.", flush=True)
        return []

    mode = "--only-missing-summaries" if only_missing_summaries else "full"
    lim_str = f" (limit {limit})" if limit is not None else ""
    print(f"Backfilling {total} games [{mode}]{lim_str}", flush=True)

    results: list = []
    with HttpClient() as client:
        for idx, game_id in enumerate(game_ids, 1):
            with session_factory() as session:
                game = session.get(Game, game_id)
                if game is None:
                    continue
                result = _process_game(session, game, client, only_missing_summaries)
                session.commit()

            results.append(result)
            _print_result(idx, total, result)

    new_summaries = sum(r.critic_written + r.user_written for r in results)
    complete = sum(r.already_complete for r in results)
    skipped = sum(r.skipped_no_reviews for r in results)
    scored_no_rev = sum(r.scored_no_reviews for r in results)
    errors = sum(r.error is not None for r in results)
    games_with_new = sum(r.critic_written or r.user_written for r in results)
    print(
        f"\nDone: {new_summaries} new summaries on {games_with_new} games, "
        f"{complete} already complete, "
        f"{skipped} skipped (unreleased), "
        f"{scored_no_rev} scored-but-no-reviews, "
        f"{errors} errors",
        flush=True,
    )
    return results


def _cli() -> None:
    import argparse

    from .db.session import init_db, make_engine, make_session_factory

    parser = argparse.ArgumentParser(
        description="Backfill games in the database with detail, reviews, and summaries."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of games to process (default: all)",
    )
    parser.add_argument(
        "--only-missing-summaries", action="store_true",
        help="Only process games that have reviews but are missing summaries",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Suppress noisy sub-loggers during the run.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)

    run_backfill(
        session_factory,
        limit=args.limit,
        only_missing_summaries=args.only_missing_summaries,
    )


if __name__ == "__main__":
    _cli()
