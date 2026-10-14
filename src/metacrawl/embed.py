"""
Text embeddings for game similarity.

COMPUTE STRATEGY
────────────────
Embeddings are computed during the crawl (on write) and stored in the
`embeddings` table as a JSON-serialised float list.  Similarity is computed
on read by loading all stored vectors and doing a linear cosine-similarity
scan — no pre-built index.

Why on-read for similarity: new games are immediately visible as neighbours
without reprocessing existing games.  At O(hundreds) games a linear scan over
768-dim vectors is negligible (~milliseconds).  If the catalogue grows to
O(tens of thousands) this should be replaced with an ANN index (e.g. FAISS).

FALLBACK
────────
Games without a description (common for unreleased titles) are not embedded.
When computing similarity against such a game, `find_similar` (similar.py)
falls back to weighted Jaccard over genres (60 %) and platforms (40 %).
The method field in each SimilarResult records which path was taken so the
UI can show it to the user.

CROSS-CHECK: Metacritic related-carousel
────────────────────────────────────────
The related-games carousel on the Metacritic game page is parsed in
scrape/detail.py (RelatedItem) and logged at DEBUG level in crawl.py.
It is never used as the answer shown in the UI — the assignment requires
candidates from OUR database only.
"""
from __future__ import annotations

import json
import logging
import math
from typing import Callable, List, Optional

import httpx
from sqlalchemy import delete, select

from .config import settings
from .db.models import Embedding, PlatformScore, game_genres

log = logging.getLogger(__name__)

# Type alias — avoids `Optional[List[float]]` runtime issue on Py 3.9
EmbedCallable = Callable[[str], "Optional[List[float]]"]


# ── Text construction ─────────────────────────────────────────────────────────

def build_embed_text(title: str, description: str, genres: list, platforms: list) -> str:
    """
    Construct the input text for the embedding model.

    Layout:
        {title}
        {description}
        Genres: {g1}, {g2}
        Platforms: {p1}, {p2}

    Genres and platforms broaden the signal for games with terse descriptions.
    """
    parts = [title]
    if description:
        parts.append(description)
    if genres:
        parts.append("Genres: " + ", ".join(sorted(genres)))
    if platforms:
        parts.append("Platforms: " + ", ".join(sorted(platforms)))
    return "\n".join(parts)


# ── Math ──────────────────────────────────────────────────────────────────────

def cosine_sim(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ── Embedder — OpenAI-compatible /embeddings endpoint ────────────────────────

def call_embedder(text: str, *, _transport=None) -> "Optional[List[float]]":
    """
    Call the embeddings API and return the vector, or None on any error.

    _transport is a test seam — pass a custom httpx.BaseTransport to avoid
    real network calls (same pattern as llm.call_llm).
    """
    url = settings.llm_base_url.rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    payload = {"model": settings.llm_embed_model, "input": text}
    try:
        kwargs: dict = {}
        if _transport is not None:
            kwargs["transport"] = _transport
        with httpx.Client(**kwargs, timeout=30) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception:
        log.warning("embedding call failed for model=%s", settings.llm_embed_model, exc_info=True)
        return None


# ── DB storage ────────────────────────────────────────────────────────────────

def _store(session, game_id: int, vector: list, model: str) -> None:
    session.execute(delete(Embedding).where(Embedding.game_id == game_id))
    session.add(Embedding(game_id=game_id, vector=json.dumps(vector), model=model))


# ── Public API ────────────────────────────────────────────────────────────────

def embed_game(session, game, embedder: EmbedCallable) -> bool:
    """
    Compute and store an embedding for `game`.

    Returns True if the embedding was stored.
    Returns False without storing if:
    - game.description is None  (fallback to metadata overlap at query time)
    - the embedder returns None  (e.g. Ollama is offline; logged as WARNING)

    The caller must flush/commit after this returns True.
    """
    if not game.description:
        log.debug("skip embedding for %s — no description (will use metadata fallback)", game.slug)
        return False

    genres = session.execute(
        select(game_genres.c.genre).where(game_genres.c.game_id == game.id)
    ).scalars().all()
    platforms = session.execute(
        select(PlatformScore.platform).where(PlatformScore.game_id == game.id)
    ).scalars().all()

    text = build_embed_text(game.title, game.description, list(genres), list(platforms))
    vector = embedder(text)
    if vector is None:
        log.warning("embedder returned None for %s; no embedding stored", game.slug)
        return False

    _store(session, game.id, vector, settings.llm_embed_model)
    log.debug("stored %d-dim embedding for %s", len(vector), game.slug)
    return True
