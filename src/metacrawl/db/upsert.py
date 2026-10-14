"""
Upsert a GameSummary (listing) or apply a GameDetail (detail page) to a game.

`upsert_game`   — called after every listing fetch; sets basic fields.
`apply_detail`  — called after fetching the game page; overwrites with richer
                   data (developer, video, per-platform scores, reviews).

The game's `first_seen_at` is never changed after creation.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from ..scrape.detail import GameDetail
from ..scrape.listing import GameSummary
from .models import Game, PlatformScore, Review, game_genres


def upsert_game(session: Session, summary: GameSummary) -> tuple[Game, bool]:
    """
    Insert or update a game from a listing summary.

    Returns (game, created) where `created` is True for new rows.
    """
    now = datetime.utcnow()
    game = session.query(Game).filter_by(external_id=summary.external_id).first()
    created = game is None

    if created:
        game = Game(
            external_id=summary.external_id,
            slug=summary.slug,
            title=summary.title,
            first_seen_at=now,
            last_crawled_at=now,
        )
        session.add(game)
        session.flush()  # populate game.id before FK inserts

    # Update every listing-level field.
    game.slug = summary.slug
    game.title = summary.title
    game.description = summary.description
    game.cover_url = summary.image_url
    game.release_date = summary.release_date
    game.rating = summary.rating
    game.metascore = summary.metascore
    if summary.platforms:
        # Listing gives per-platform names but no per-platform scores yet;
        # userscore comes from the top-level userScore field (set in step 3).
        pass
    game.last_crawled_at = now

    # Replace platform rows entirely — the listing doesn't carry per-platform
    # scores, so these are just name placeholders until the detail fetch.
    session.execute(
        delete(PlatformScore).where(PlatformScore.game_id == game.id)
    )
    for p in summary.platforms:
        session.add(PlatformScore(
            game_id=game.id,
            platform=p.name,
            metascore=p.metascore,
            userscore=p.userscore,
            critic_reviews=p.critic_reviews,
            user_reviews=p.user_reviews,
        ))

    # Replace genre associations.
    session.execute(
        delete(game_genres).where(game_genres.c.game_id == game.id)
    )
    if summary.genres:
        session.execute(
            insert(game_genres),
            [{"game_id": game.id, "genre": g} for g in summary.genres],
        )

    return game, created


def apply_detail(session: Session, game: Game, detail: GameDetail) -> None:
    """
    Overwrite a game's detail-level fields from a parsed GameDetail.

    Called after fetching the game page; replaces platform_scores (now with
    real per-platform Metascores), genres, and reviews.  Does NOT change
    first_seen_at or external_id.
    """
    # Scalar overrides — detail page is authoritative for these.
    if detail.title:
        game.title = detail.title
    if detail.description:
        game.description = detail.description
    if detail.release_date:
        game.release_date = detail.release_date
    if detail.rating:
        game.rating = detail.rating
    if detail.developer:
        game.developer = detail.developer
    if detail.publisher:
        game.publisher = detail.publisher
    if detail.cover_url:
        game.cover_url = detail.cover_url
    if detail.video_url:
        game.video_url = detail.video_url
    if detail.metascore is not None:
        game.metascore = detail.metascore
    if detail.userscore is not None:
        game.userscore = detail.userscore
    game.last_crawled_at = datetime.utcnow()

    # Replace platform scores with per-platform data from the detail page.
    if detail.platforms:
        session.execute(
            delete(PlatformScore).where(PlatformScore.game_id == game.id)
        )
        for p in detail.platforms:
            session.add(PlatformScore(
                game_id=game.id,
                platform=p.name,
                metascore=p.metascore,
                userscore=p.userscore,
                critic_reviews=p.critic_reviews,
                user_reviews=p.user_reviews,
            ))

    # Replace genres.
    if detail.genres:
        session.execute(
            delete(game_genres).where(game_genres.c.game_id == game.id)
        )
        session.execute(
            insert(game_genres),
            [{"game_id": game.id, "genre": g} for g in detail.genres],
        )

    # Append new reviews (avoid duplicating across re-crawls by clearing first).
    all_reviews = detail.critic_reviews + detail.user_reviews
    if all_reviews:
        session.execute(
            delete(Review).where(Review.game_id == game.id)
        )
        for r in all_reviews:
            session.add(Review(
                game_id=game.id,
                kind=r.kind,
                author=r.author,
                publication=r.publication,
                score=r.score,
                quote=r.quote,
                review_date=r.review_date,
                thumbs_up=getattr(r, "thumbs_up", None),
                thumbs_down=getattr(r, "thumbs_down", None),
            ))
