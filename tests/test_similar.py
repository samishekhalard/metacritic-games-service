"""
Tests for embed.py and similar.py — all offline, no network.

Uses a fake embedder: deterministic 8-dim unit vectors from text content.
Two texts that are identical produce cosine_sim = 1.0.
Two unrelated texts produce a lower (but non-zero) score.
"""
from __future__ import annotations

import hashlib
import json
import math

import pytest
from sqlalchemy import select

from metacrawl.db.models import Embedding, Game, PlatformScore, Review, game_genres
from metacrawl.db.upsert import upsert_game
from metacrawl.embed import build_embed_text, cosine_sim, embed_game
from metacrawl.scrape.listing import GameSummary
from metacrawl.similar import SimilarResult, _jaccard, _metadata_score, find_similar


# ── Fake embedder ─────────────────────────────────────────────────────────────

def _fake_embed(text: str):
    """Deterministic 8-dim unit vector derived from text SHA-256."""
    raw = hashlib.sha256(text.encode()).digest()[:8]
    vec = [b / 128.0 - 1.0 for b in raw]
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return [0.0] * 8
    return [x / norm for x in vec]


def _null_embed(text: str):
    """Simulates an embedder that always fails (e.g. Ollama offline)."""
    return None


# ── DB helpers ────────────────────────────────────────────────────────────────

def _make_game(session, *, external_id: int, slug: str, title: str,
               description: str = None) -> Game:
    summary = GameSummary(
        external_id=external_id,
        slug=slug,
        title=title,
        url=f"https://metacritic.com/game/{slug}/",
    )
    game, _ = upsert_game(session, summary)
    if description is not None:
        game.description = description
    session.flush()
    return game


def _add_genres(session, game: Game, genres: list) -> None:
    for g in genres:
        session.execute(
            game_genres.insert().values(game_id=game.id, genre=g)
        )
    session.flush()


def _add_platform(session, game: Game, platform: str) -> None:
    session.add(PlatformScore(game_id=game.id, platform=platform))
    session.flush()


# ── cosine_sim unit tests ─────────────────────────────────────────────────────

def test_cosine_sim_identical():
    v = [1.0, 0.0, 0.0]
    assert cosine_sim(v, v) == pytest.approx(1.0)


def test_cosine_sim_orthogonal():
    assert cosine_sim([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_sim_opposite():
    assert cosine_sim([1.0], [-1.0]) == pytest.approx(-1.0)


def test_cosine_sim_zero_vector():
    assert cosine_sim([0.0, 0.0], [1.0, 0.0]) == 0.0


# ── _jaccard unit tests ───────────────────────────────────────────────────────

def test_jaccard_identical():
    s = {"Action", "RPG"}
    assert _jaccard(s, s) == pytest.approx(1.0)


def test_jaccard_disjoint():
    assert _jaccard({"Action"}, {"Sports"}) == pytest.approx(0.0)


def test_jaccard_partial():
    assert _jaccard({"A", "B"}, {"B", "C"}) == pytest.approx(1 / 3)


def test_jaccard_empty():
    assert _jaccard(set(), set()) == 0.0


# ── _metadata_score ───────────────────────────────────────────────────────────

def test_metadata_score_perfect_overlap():
    score = _metadata_score({"RPG"}, {"PC"}, {"RPG"}, {"PC"})
    assert score == pytest.approx(1.0)


def test_metadata_score_no_overlap():
    score = _metadata_score({"RPG"}, {"PC"}, {"Sports"}, {"PS5"})
    assert score == pytest.approx(0.0)


def test_metadata_score_genre_weighted_more():
    # Same genres, different platforms → score = 0.6
    score = _metadata_score({"RPG"}, {"PC"}, {"RPG"}, {"PS5"})
    assert score == pytest.approx(0.6)


# ── embed_game ────────────────────────────────────────────────────────────────

def test_embed_game_stores_vector(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=100, slug="action-rpg",
                          title="Action RPG", description="An action RPG.")
        _add_genres(s, game, ["Action", "RPG"])
        _add_platform(s, game, "PC")
        stored = embed_game(s, game, _fake_embed)
        s.commit()
        game_id = game.id

    assert stored is True
    with Session() as s:
        emb = s.execute(select(Embedding).where(Embedding.game_id == game_id)).scalar_one()
    vector = json.loads(emb.vector)
    assert len(vector) == 8
    # Verify it is indeed normalised (unit vector)
    norm = math.sqrt(sum(x * x for x in vector))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_embed_game_no_description_returns_false(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=101, slug="no-desc", title="No Desc",
                          description=None)
        stored = embed_game(s, game, _fake_embed)
        s.commit()
        game_id = game.id

    assert stored is False
    with Session() as s:
        count = s.execute(select(Embedding).where(Embedding.game_id == game_id)).scalars().all()
    assert len(count) == 0


def test_embed_game_failed_embedder_returns_false(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=102, slug="fail-game", title="Fail",
                          description="Some description.")
        stored = embed_game(s, game, _null_embed)
        s.commit()
        game_id = game.id

    assert stored is False
    with Session() as s:
        count = s.execute(select(Embedding).where(Embedding.game_id == game_id)).scalars().all()
    assert len(count) == 0


def test_embed_game_idempotent(db):
    """Calling embed_game twice replaces the vector, no duplicates."""
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=103, slug="idem-game", title="Idem",
                          description="First description.")
        embed_game(s, game, _fake_embed)
        game.description = "Second description — different vector."
        embed_game(s, game, _fake_embed)
        s.commit()
        game_id = game.id

    with Session() as s:
        rows = s.execute(select(Embedding).where(Embedding.game_id == game_id)).scalars().all()
    assert len(rows) == 1


# ── find_similar — embedding path ────────────────────────────────────────────

def test_find_similar_uses_embeddings_when_available(db):
    """
    Both games have embeddings → method=embedding.

    Use the same title + description so the embed text is identical,
    which guarantees cosine_sim = 1.0 with a deterministic embedder.
    """
    Session, _ = db
    shared_title = "Dragon Quest"
    shared_desc  = "An epic open-world action RPG with dragons."
    with Session() as s:
        ga = _make_game(s, external_id=200, slug="game-a", title=shared_title, description=shared_desc)
        gb = _make_game(s, external_id=201, slug="game-b", title=shared_title, description=shared_desc)
        _add_genres(s, ga, ["RPG"])
        _add_genres(s, gb, ["RPG"])
        embed_game(s, ga, _fake_embed)
        embed_game(s, gb, _fake_embed)
        s.commit()
        ga_id, gb_id = ga.id, gb.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    assert len(results) >= 1
    top = results[0]
    assert top.game.id == gb_id
    assert top.method == "embedding"
    assert top.score == pytest.approx(1.0, abs=1e-6)


def test_find_similar_sorted_by_score_descending(db):
    Session, _ = db
    with Session() as s:
        ga = _make_game(s, external_id=210, slug="query-game", title="Query",
                        description="Fantasy RPG with magic spells and dungeons.")
        gb = _make_game(s, external_id=211, slug="close-game", title="Close",
                        description="Fantasy RPG with magic spells and dungeons.")  # same
        gc = _make_game(s, external_id=212, slug="far-game", title="Far",
                        description="Sports racing simulator with fast cars.")
        for g in (ga, gb, gc):
            embed_game(s, g, _fake_embed)
        s.commit()
        ga_id = ga.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    # gb (identical text) should be first
    assert results[0].game.slug == "close-game"


def test_find_similar_score_is_visible(db):
    """Score is exposed on SimilarResult and as score_pct int."""
    Session, _ = db
    with Session() as s:
        ga = _make_game(s, external_id=220, slug="vis-a", title="A",
                        description="Some game about adventures.")
        gb = _make_game(s, external_id=221, slug="vis-b", title="B",
                        description="Some game about adventures.")
        embed_game(s, ga, _fake_embed)
        embed_game(s, gb, _fake_embed)
        s.commit()
        ga_id = ga.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    r = results[0]
    assert isinstance(r.score, float)
    assert 0.0 <= r.score <= 1.0
    assert isinstance(r.score_pct, int)
    assert 0 <= r.score_pct <= 100


# ── find_similar — metadata fallback ─────────────────────────────────────────

def test_find_similar_no_description_uses_metadata(db):
    """Query game has no description → all results use metadata_overlap."""
    Session, _ = db
    with Session() as s:
        ga = _make_game(s, external_id=300, slug="nodesc-a", title="No Desc A",
                        description=None)
        gb = _make_game(s, external_id=301, slug="nodesc-b", title="No Desc B",
                        description="Some description.")
        _add_genres(s, ga, ["RPG"])
        _add_genres(s, gb, ["RPG"])
        _add_platform(s, ga, "PC")
        _add_platform(s, gb, "PC")
        embed_game(s, ga, _fake_embed)   # returns False — no description
        embed_game(s, gb, _fake_embed)   # stores embedding for gb
        s.commit()
        ga_id = ga.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    assert all(r.method == "metadata_overlap" for r in results)


def test_find_similar_candidate_no_embedding_uses_metadata(db):
    """Query has embedding, candidate does not → that pair uses metadata_overlap."""
    Session, _ = db
    with Session() as s:
        ga = _make_game(s, external_id=310, slug="has-emb", title="Has Embedding",
                        description="An action game.")
        gb = _make_game(s, external_id=311, slug="no-emb", title="No Embedding",
                        description=None)
        _add_genres(s, ga, ["Action"])
        _add_genres(s, gb, ["Action"])
        _add_platform(s, ga, "PC")
        _add_platform(s, gb, "PC")
        embed_game(s, ga, _fake_embed)
        embed_game(s, gb, _fake_embed)   # returns False — no description
        s.commit()
        ga_id = ga.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    gb_result = next(r for r in results if r.game.slug == "no-emb")
    assert gb_result.method == "metadata_overlap"


def test_metadata_same_genres_platforms_score_high(db):
    Session, _ = db
    with Session() as s:
        ga = _make_game(s, external_id=320, slug="meta-a", title="Meta A", description=None)
        gb = _make_game(s, external_id=321, slug="meta-b", title="Meta B", description=None)
        _add_genres(s, ga, ["RPG", "Action"])
        _add_genres(s, gb, ["RPG", "Action"])
        _add_platform(s, ga, "PC")
        _add_platform(s, gb, "PC")
        s.commit()
        ga_id = ga.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    top = results[0]
    assert top.game.slug == "meta-b"
    assert top.score == pytest.approx(1.0)
    assert top.method == "metadata_overlap"


def test_metadata_no_shared_genres_platforms_score_zero(db):
    Session, _ = db
    with Session() as s:
        ga = _make_game(s, external_id=330, slug="zero-a", title="Zero A", description=None)
        gb = _make_game(s, external_id=331, slug="zero-b", title="Zero B", description=None)
        _add_genres(s, ga, ["RPG"])
        _add_genres(s, gb, ["Sports"])
        _add_platform(s, ga, "PC")
        _add_platform(s, gb, "PlayStation 5")
        s.commit()
        ga_id = ga.id

    with Session() as s:
        ga = s.get(Game, ga_id)
        results = find_similar(s, ga, k=5)

    zero_b = next(r for r in results if r.game.slug == "zero-b")
    assert zero_b.score == pytest.approx(0.0)
    assert zero_b.is_weak is True


# ── build_embed_text ──────────────────────────────────────────────────────────

def test_build_embed_text_includes_all_parts():
    text = build_embed_text("My Game", "A cool game.", ["RPG", "Action"], ["PC"])
    assert "My Game" in text
    assert "A cool game." in text
    assert "RPG" in text
    assert "PC" in text


def test_build_embed_text_no_description():
    # description is not included when empty/None
    text = build_embed_text("My Game", "", ["RPG"], ["PC"])
    assert "My Game" in text
    # Empty description should not add blank lines
    assert "\n\n" not in text
