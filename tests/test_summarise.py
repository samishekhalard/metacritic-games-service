"""
Tests for summarise.py — LLM summary generation.

Uses a fake LLM callable instead of real network calls.
All tests are offline.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from metacrawl.db.models import Game, Review, Summary
from metacrawl.db.upsert import upsert_game
from metacrawl.scrape.listing import GameSummary
from metacrawl.summarise import (
    _parse_llm_response,
    _sample_critic,
    _sample_user,
    generate_summaries,
)


# ── Fake LLM callables ────────────────────────────────────────────────────────

def _good_llm(messages):
    """Always returns a well-formed JSON summary."""
    return json.dumps({"likes": "Great gameplay.", "dislikes": "Short campaign."})


def _malformed_llm(messages):
    """Returns invalid JSON."""
    return "I liked it a lot!"


def _wrong_keys_llm(messages):
    """Returns JSON with wrong keys."""
    return json.dumps({"pros": "fun", "cons": "short"})


def _none_llm(messages):
    """Simulates a total LLM failure."""
    return None


def _empty_values_llm(messages):
    """Returns JSON with empty string values (invalid)."""
    return json.dumps({"likes": "", "dislikes": ""})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_game(session, external_id=1, slug="test-game"):
    summary = GameSummary(
        external_id=external_id, slug=slug, title="Test Game",
        url=f"https://www.metacritic.com/game/{slug}/",
    )
    game, _ = upsert_game(session, summary)
    session.flush()
    return game


def _add_reviews(session, game, kind="critic", n=5, base_score=80):
    for i in range(n):
        session.add(Review(
            game_id=game.id,
            kind=kind,
            author=f"Reviewer {i}",
            publication="IGN" if kind == "critic" else None,
            score=base_score + i,
            quote=f"This is review number {i} with enough text to be useful.",
            review_date=f"2024-01-{i+1:02d}",
            thumbs_up=i * 10 if kind == "user" else None,
        ))
    session.flush()


# ── _parse_llm_response ───────────────────────────────────────────────────────

def test_parse_valid_response():
    result = _parse_llm_response('{"likes": "fun", "dislikes": "short"}')
    assert result == {"likes": "fun", "dislikes": "short"}


def test_parse_invalid_json_returns_none():
    assert _parse_llm_response("not json") is None


def test_parse_missing_key_returns_none():
    assert _parse_llm_response('{"likes": "fun"}') is None


def test_parse_wrong_types_returns_none():
    assert _parse_llm_response('{"likes": 42, "dislikes": "short"}') is None


def test_parse_empty_values_returns_none():
    assert _parse_llm_response('{"likes": "", "dislikes": "short"}') is None


def test_parse_extra_keys_ok():
    # Extra keys are ignored; the required ones are present.
    result = _parse_llm_response('{"likes": "a", "dislikes": "b", "score": 9}')
    assert result is not None


# ── Sampling ──────────────────────────────────────────────────────────────────

def _make_reviews(kind, scores, thumbs=None):
    """Return a list of Review objects without a DB session."""
    reviews = []
    for i, score in enumerate(scores):
        r = Review()
        r.kind = kind
        r.score = score
        r.quote = f"Quote {i}"
        r.review_date = f"2024-01-{i+1:02d}"
        r.thumbs_up = (thumbs[i] if thumbs else None)
        r.thumbs_down = None
        reviews.append(r)
    return reviews


def test_sample_critic_sorted_by_score():
    reviews = _make_reviews("critic", [70, 90, 80])
    picked, rule = _sample_critic(reviews, 10)
    assert [r.score for r in picked] == [90, 80, 70]


def test_sample_critic_caps_at_n():
    reviews = _make_reviews("critic", list(range(50)))
    picked, _ = _sample_critic(reviews, 10)
    assert len(picked) == 10


def test_sample_critic_skips_no_quote():
    reviews = _make_reviews("critic", [80, 90])
    reviews[0].quote = None
    picked, _ = _sample_critic(reviews, 10)
    assert len(picked) == 1


def test_sample_user_sorted_by_thumbsup():
    reviews = _make_reviews("user", [8, 9, 7], thumbs=[5, 100, 1])
    picked, rule = _sample_user(reviews, 10)
    assert picked[0].thumbs_up == 100
    assert picked[1].thumbs_up == 5


def test_sample_user_caps_at_n():
    reviews = _make_reviews("user", list(range(60)), thumbs=[0] * 60)
    picked, _ = _sample_user(reviews, 40)
    assert len(picked) == 40


def test_sample_critic_rule_name_includes_n():
    reviews = _make_reviews("critic", [80] * 5)
    _, rule = _sample_critic(reviews, 10)
    assert "5" in rule   # picked 5 reviews, n=5 in rule name


# ── generate_summaries — zero reviews ────────────────────────────────────────

def test_no_reviews_creates_no_summary(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s)
        c, u = generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    assert c is False
    assert u is False
    with Session() as s:
        assert s.query(Summary).filter_by(game_id=game_id).count() == 0


def test_zero_critic_reviews_skips_critic_summary(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=2)
        _add_reviews(s, game, kind="user", n=5)
        c, u = generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    assert c is False
    assert u is True
    with Session() as s:
        kinds = {r.kind for r in s.query(Summary).filter_by(game_id=game_id).all()}
    assert "critic" not in kinds
    assert "user" in kinds


def test_zero_user_reviews_skips_user_summary(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=3)
        _add_reviews(s, game, kind="critic", n=5)
        c, u = generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    assert c is True
    assert u is False


# ── generate_summaries — success path ────────────────────────────────────────

def test_generates_both_summaries_when_both_have_reviews(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=4)
        _add_reviews(s, game, kind="critic", n=5)
        _add_reviews(s, game, kind="user", n=5)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        sums = s.query(Summary).filter_by(game_id=game_id).all()
    assert len(sums) == 2
    kinds = {s.kind for s in sums}
    assert kinds == {"critic", "user"}


def test_summary_stores_model_name(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=5)
        _add_reviews(s, game, kind="critic", n=3)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="critic").one()
    # Model comes from settings.llm_model
    assert summ.model is not None and len(summ.model) > 0


def test_summary_stores_source_review_count(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=6)
        _add_reviews(s, game, kind="critic", n=7)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="critic").one()
    assert summ.source_review_count == 7


def test_summary_stores_sampling_rule(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=7)
        _add_reviews(s, game, kind="critic", n=3)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="critic").one()
    assert summ.sampling_rule is not None


def test_summary_stores_generated_at(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=8)
        _add_reviews(s, game, kind="user", n=3)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="user").one()
    assert isinstance(summ.generated_at, datetime)


def test_summary_stores_likes_and_dislikes(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=9)
        _add_reviews(s, game, kind="critic", n=3)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="critic").one()
    assert summ.likes == "Great gameplay."
    assert summ.dislikes == "Short campaign."


# ── generate_summaries — failure paths ───────────────────────────────────────

def test_malformed_llm_response_creates_no_summary(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=10)
        _add_reviews(s, game, kind="critic", n=3)
        generate_summaries(s, game, _malformed_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        assert s.query(Summary).filter_by(game_id=game_id).count() == 0


def test_wrong_keys_creates_no_summary(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=11)
        _add_reviews(s, game, kind="user", n=3)
        generate_summaries(s, game, _wrong_keys_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        assert s.query(Summary).filter_by(game_id=game_id).count() == 0


def test_llm_none_creates_no_summary(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=12)
        _add_reviews(s, game, kind="critic", n=3)
        generate_summaries(s, game, _none_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        assert s.query(Summary).filter_by(game_id=game_id).count() == 0


# ── generate_summaries — idempotency ─────────────────────────────────────────

def test_re_summarise_replaces_not_duplicates(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=13)
        _add_reviews(s, game, kind="critic", n=3)
        generate_summaries(s, game, _good_llm)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        count = s.query(Summary).filter_by(game_id=game_id, kind="critic").count()
    assert count == 1


# ── reviews without quotes are excluded ──────────────────────────────────────

def test_reviews_without_quotes_not_counted(db):
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=14)
        # All reviews have no quote
        for i in range(5):
            s.add(Review(
                game_id=game.id, kind="critic", score=80,
                quote=None, review_date="2024-01-01",
            ))
        s.flush()
        c, _ = generate_summaries(s, game, _good_llm)
        s.commit()

    assert c is False


# ── cap applied correctly ─────────────────────────────────────────────────────

def test_source_review_count_capped_at_max(db, monkeypatch):
    """source_review_count <= MC_LLM_MAX_REVIEWS even with many reviews."""
    monkeypatch.setattr("metacrawl.summarise.settings.llm_max_reviews", 5)
    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=15)
        _add_reviews(s, game, kind="critic", n=30)
        generate_summaries(s, game, _good_llm)
        s.commit()
        game_id = game.id

    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="critic").one()
    assert summ.source_review_count == 5
