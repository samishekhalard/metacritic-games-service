"""
Similar-game lookup — candidates from our database only.

Two methods, chosen per-pair based on available data:

  embedding        cosine similarity between stored text-embedding vectors.
                   Requires BOTH games to have a stored embedding (i.e. both
                   must have a non-null description).

  metadata_overlap weighted Jaccard over genres (60 %) + platforms (40 %).
                   Used when the query game has no embedding, OR when a
                   candidate game has no embedding.  Never relies on the
                   Metacritic related-carousel.

The `method` field on each result tells the caller (and the UI) which path
produced that score, so a user can see when a match is thin.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from .db.models import Embedding, Game, PlatformScore, game_genres
from .embed import cosine_sim

_TOP_K = 5


@dataclass
class SimilarResult:
    game: Game
    score: float            # 0.0 – 1.0
    method: str             # "embedding" | "metadata_overlap"

    @property
    def score_pct(self) -> int:
        """Score as integer percentage (0–100) for display."""
        return round(self.score * 100)

    @property
    def is_weak(self) -> bool:
        """True when the score is low enough to warn the user."""
        return self.score < 0.30


# ── Internal helpers ──────────────────────────────────────────────────────────

def _jaccard(a: set, b: set) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _metadata_score(genres_a: set, platforms_a: set, genres_b: set, platforms_b: set) -> float:
    """Weighted Jaccard: genres 60 %, platforms 40 %."""
    return _jaccard(genres_a, genres_b) * 0.6 + _jaccard(platforms_a, platforms_b) * 0.4


def _load_genres(session, game_id: int) -> set:
    return set(session.execute(
        select(game_genres.c.genre).where(game_genres.c.game_id == game_id)
    ).scalars().all())


def _load_platforms(session, game_id: int) -> set:
    return set(session.execute(
        select(PlatformScore.platform).where(PlatformScore.game_id == game_id)
    ).scalars().all())


# ── Public API ────────────────────────────────────────────────────────────────

def find_similar(session, game: Game, k: int = _TOP_K) -> list:
    """
    Return up to k SimilarResults for `game`, sorted by score descending.

    All candidates come from the local database.  The Metacritic related-
    carousel (if present in scraped data) is only cross-checked in crawl.py
    and never used here.
    """
    # ── Query game metadata ───────────────────────────────────────────────
    q_emb_row = session.execute(
        select(Embedding).where(Embedding.game_id == game.id)
    ).scalar_one_or_none()
    q_vector: Optional[list] = json.loads(q_emb_row.vector) if q_emb_row else None
    q_genres = _load_genres(session, game.id)
    q_platforms = _load_platforms(session, game.id)

    # ── Load ALL candidate embeddings in one query ────────────────────────
    all_embs: dict = {
        row.game_id: json.loads(row.vector)
        for row in session.execute(select(Embedding)).scalars().all()
        if row.game_id != game.id
    }

    # ── Score every other game ────────────────────────────────────────────
    others = session.execute(
        select(Game).where(Game.id != game.id)
    ).scalars().all()

    results = []
    for other in others:
        if q_vector and other.id in all_embs:
            score = cosine_sim(q_vector, all_embs[other.id])
            method = "embedding"
        else:
            o_genres = _load_genres(session, other.id)
            o_platforms = _load_platforms(session, other.id)
            score = _metadata_score(q_genres, q_platforms, o_genres, o_platforms)
            method = "metadata_overlap"

        results.append(SimilarResult(game=other, score=score, method=method))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:k]
