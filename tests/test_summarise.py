"""
Tests for summarise.py — LLM summary generation.

Uses a fake LLM callable instead of real network calls.
All tests are offline.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import pytest

from metacrawl.db.models import Game, Review, Summary
from metacrawl.db.upsert import upsert_game
from metacrawl.scrape.listing import GameSummary
from metacrawl.summarise import (
    _PROMPT_MAX_REVIEWS,
    _REVIEW_MAX_CHARS,
    _parse_llm_response,
    _sample_critic,
    _sample_user,
    _strip_fence,
    generate_summaries,
)


# ── Fake LLM callables ────────────────────────────────────────────────────────

def _good_llm(messages):
    """Always returns a well-formed JSON summary (both fields ≥ 20 chars)."""
    return json.dumps({"likes": "The gameplay is excellent.", "dislikes": "The campaign is too short."})


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
    result = _parse_llm_response(
        '{"likes": "Excellent mechanics and visuals.", "dislikes": "Repetitive mission design."}'
    )
    assert result == {"likes": "Excellent mechanics and visuals.", "dislikes": "Repetitive mission design."}


def test_parse_null_likes_is_valid():
    result = _parse_llm_response(
        '{"likes": null, "dislikes": "The game has serious performance issues on all platforms."}'
    )
    assert result is not None
    assert result["likes"] is None
    assert result["dislikes"] == "The game has serious performance issues on all platforms."


def test_parse_null_dislikes_is_valid():
    result = _parse_llm_response(
        '{"likes": "Superb art direction and fluid combat throughout.", "dislikes": null}'
    )
    assert result is not None
    assert result["likes"] == "Superb art direction and fluid combat throughout."
    assert result["dislikes"] is None


def test_parse_both_null_is_valid():
    result = _parse_llm_response('{"likes": null, "dislikes": null}')
    assert result == {"likes": None, "dislikes": None}


def test_parse_invalid_json_returns_none():
    assert _parse_llm_response("not json") is None


def test_parse_missing_key_returns_none():
    assert _parse_llm_response('{"likes": "fun"}') is None


def test_parse_wrong_types_returns_none():
    assert _parse_llm_response('{"likes": 42, "dislikes": "short"}') is None


def test_parse_empty_values_returns_none():
    assert _parse_llm_response('{"likes": "", "dislikes": "short"}') is None


def test_parse_too_short_values_returns_none():
    # Both fields present but under the 20-char minimum — reject as half-summary.
    assert _parse_llm_response('{"likes": "fun", "dislikes": "short"}') is None


def test_parse_extra_keys_ok():
    # Extra keys are ignored; required fields are long enough.
    result = _parse_llm_response(
        '{"likes": "Excellent mechanics overall.", "dislikes": "Repetitive missions.", "score": 9}'
    )
    assert result is not None


# ── _strip_fence / fenced JSON (Groq llama-3.3-70b-versatile wraps output) ───

def test_strip_fence_removes_json_fence():
    raw = '```json\n{"likes": "ok", "dislikes": "bad"}\n```'
    assert _strip_fence(raw) == '{"likes": "ok", "dislikes": "bad"}'


def test_strip_fence_removes_plain_fence():
    raw = '```\n{"likes": "ok", "dislikes": "bad"}\n```'
    assert _strip_fence(raw) == '{"likes": "ok", "dislikes": "bad"}'


def test_strip_fence_passthrough_for_plain_json():
    raw = '{"likes": "ok", "dislikes": "bad"}'
    assert _strip_fence(raw) == raw


def test_parse_accepts_fenced_json():
    """Groq llama-3.3-70b-versatile wraps output in ```json fences despite the prompt."""
    raw = (
        "```json\n"
        '{"likes": "Fluid combat and stunning open-world environments.", '
        '"dislikes": "Occasional frame-rate drops on older hardware."}\n'
        "```"
    )
    result = _parse_llm_response(raw)
    assert result is not None
    assert result["likes"] == "Fluid combat and stunning open-world environments."
    assert result["dislikes"] == "Occasional frame-rate drops on older hardware."


def test_parse_fenced_null_sentinel():
    raw = '```json\n{"likes": null, "dislikes": "Relentless difficulty with no accessibility options."}\n```'
    result = _parse_llm_response(raw)
    assert result is not None
    assert result["likes"] is None


def test_parse_fenced_bad_content_still_rejected():
    """Fence-stripping must not weaken validation — short strings still rejected."""
    raw = '```json\n{"likes": "fun", "dislikes": "bad"}\n```'
    assert _parse_llm_response(raw) is None


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
        c, u, _ = generate_summaries(s, game, _good_llm)
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
        c, u, _ = generate_summaries(s, game, _good_llm)
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
        c, u, _ = generate_summaries(s, game, _good_llm)
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
    assert summ.likes == "The gameplay is excellent."
    assert summ.dislikes == "The campaign is too short."


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


# ── half-summary rejection ────────────────────────────────────────────────────

def test_half_summary_empty_likes_not_written(db):
    """
    A model response with an empty likes field must not reach the DB and
    must be counted as an LLM error, not silently skipped.
    {"likes": "", "dislikes": "..."} is the exact shape that caused a real
    production truncation (nightmare-frontier run, Oct 2026).
    """
    def _half_llm(messages):
        return json.dumps({
            "likes": "",
            "dislikes": "The game has serious performance issues on all PC platforms tested.",
        })

    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=20)
        _add_reviews(s, game, kind="critic", n=3)
        c, u, errors = generate_summaries(s, game, _half_llm)
        s.commit()
        game_id = game.id

    assert c is False
    assert errors == 1
    with Session() as s:
        assert s.query(Summary).filter_by(game_id=game_id).count() == 0


def test_half_summary_short_field_not_written(db):
    """A response where one field is present but too short (< 20 chars) is also rejected."""
    def _short_llm(messages):
        return json.dumps({
            "likes": "OK.",
            "dislikes": "The game has serious performance issues on all PC platforms tested.",
        })

    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=21)
        _add_reviews(s, game, kind="critic", n=3)
        c, u, errors = generate_summaries(s, game, _short_llm)
        s.commit()
        game_id = game.id

    assert c is False
    assert errors == 1
    with Session() as s:
        assert s.query(Summary).filter_by(game_id=game_id).count() == 0


# ── null sentinel — uniformly negative / uniformly positive reviews ───────────

def test_null_likes_stored_not_counted_as_error(db):
    """
    {"likes": null, "dislikes": "..."} is the correct sentinel for uniformly
    negative reviews.  The row must be written and errors must stay at zero.
    """
    def _null_likes_llm(messages):
        return json.dumps({
            "likes": None,
            "dislikes": "The game has serious balance issues and the combat feels unfair throughout.",
        })

    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=22)
        _add_reviews(s, game, kind="user", n=1)
        c, u, errors = generate_summaries(s, game, _null_likes_llm)
        s.commit()
        game_id = game.id

    assert u is True
    assert errors == 0
    with Session() as s:
        summ = s.query(Summary).filter_by(game_id=game_id, kind="user").one()
    assert summ.likes is None
    assert summ.dislikes is not None


def test_empty_string_still_rejected_not_null(db):
    """Empty string is not the null sentinel — it is the truncation shape and must be an error."""
    def _empty_likes_llm(messages):
        return json.dumps({
            "likes": "",
            "dislikes": "The game has serious balance issues and the combat feels unfair throughout.",
        })

    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=23)
        _add_reviews(s, game, kind="user", n=1)
        c, u, errors = generate_summaries(s, game, _empty_likes_llm)
        s.commit()
        game_id = game.id

    assert u is False
    assert errors == 1
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
        c, _, _ = generate_summaries(s, game, _good_llm)
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


# ── prompt cap (review count + per-review truncation) ────────────────────────

def test_prompt_cap_limits_reviews_and_truncates_quotes(db):
    """200-review game: LLM receives ≤ 25 reviews, each quote ≤ 1200 chars."""
    captured: list = []

    def _capture(messages):
        captured.append(messages)
        return json.dumps({"likes": "The gameplay is excellent.", "dislikes": "The campaign is too short."})

    Session, _ = db
    with Session() as s:
        game = _make_game(s, external_id=40)
        for i in range(200):
            s.add(Review(
                game_id=game.id,
                kind="user",
                author=f"User {i}",
                score=50,
                quote="x" * 2000,   # 2000-char quote — must be truncated
                review_date="2024-01-01",
                thumbs_up=i,
            ))
        s.flush()
        generate_summaries(s, game, _capture)
        s.commit()

    assert len(captured) == 1   # one call for user kind (no critic reviews)
    prompt = captured[0][1]["content"]  # role=user content

    review_markers = re.findall(r'^\[\d+\]', prompt, re.MULTILINE)
    assert len(review_markers) <= _PROMPT_MAX_REVIEWS

    for block in prompt.split("\n\n"):
        if re.match(r'^\[\d+\]', block):
            _, _, quote = block.partition("\n")
            assert len(quote) <= _REVIEW_MAX_CHARS
