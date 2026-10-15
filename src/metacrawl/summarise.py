"""
LLM summary generation — critic and user kept separate.

SKIP RULES (both enforced before calling the LLM):
  1. A game with zero reviews of the relevant kind gets no summary row.
     New releases are unreleased; this is the common case, not the edge case.
  2. A game whose reviews are all missing quote text is treated as zero reviews.
     Summaries must come from actual review text, never from descriptions.

SAMPLING:
  critic: top MC_LLM_MAX_REVIEWS by score DESC (highest-rated critics first)
  user:   top MC_LLM_MAX_REVIEWS by thumbs_up DESC, then by review_date DESC
          (most-helpful users first; falls back to recency when thumbsUp is null)

  The rule name is stored in Summary.sampling_rule so downstream code can
  reason about what went into each summary.

NULL SENTINEL CONTRACT:
  The model may return null for likes or dislikes when that side genuinely
  has nothing — e.g. uniformly negative reviews → likes: null.  null is
  stored as-is and the card renders an explanatory line instead of a blank.
  Empty string or a string shorter than _MIN_SUMMARY_LEN is the truncation
  shape and is still rejected as a malformed response.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .config import settings
from .db.models import Game, Review, Summary

log = logging.getLogger(__name__)

_PROMPT = """\
Summarise the following {n} {kind} reviews for the game "{title}".

Reviews:
{reviews_text}

Reply with ONLY valid JSON (no markdown fences, no extra keys):
{{"likes": "2-3 sentences on what reviewers praised, or null if the reviews contain no praise",\
 "dislikes": "2-3 sentences on what reviewers criticised or found lacking, or null if the reviews contain no criticism"}}"""

# Sampling rule names stored in DB.
_RULE_CRITIC = "top{n}_score"
_RULE_USER = "top{n}_thumbsup_then_recent"


def _sample_critic(reviews: list[Review], n: int) -> tuple[list[Review], str]:
    picked = sorted(
        (r for r in reviews if r.kind == "critic" and r.quote),
        key=lambda r: r.score or 0,
        reverse=True,
    )[:n]
    return picked, _RULE_CRITIC.format(n=len(picked))


def _sample_user(reviews: list[Review], n: int) -> tuple[list[Review], str]:
    def _key(r: Review):
        return (-(r.thumbs_up or 0), -(len(r.review_date or "")), r.review_date or "")

    picked = sorted(
        (r for r in reviews if r.kind == "user" and r.quote),
        key=_key,
    )[:n]
    return picked, _RULE_USER.format(n=len(picked))


_MIN_SUMMARY_LEN = 20
_REVIEW_MAX_CHARS = 1200   # max chars per review quote sent to the LLM
_PROMPT_MAX_REVIEWS = 25   # hard ceiling regardless of MC_LLM_MAX_REVIEWS


def _strip_fence(text: str) -> str:
    """Remove ```json ... ``` or ``` ... ``` wrappers that some models add."""
    t = text.strip()
    if t.startswith("```"):
        # Drop opening fence line (```json or just ```)
        t = t[t.index("\n") + 1:] if "\n" in t else t[3:]
        # Drop closing fence
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3].rstrip()
    return t


def _parse_llm_response(text: str) -> dict | None:
    """
    Return {"likes": str|None, "dislikes": str|None} or None if invalid.

    Valid:   both fields are strings ≥ _MIN_SUMMARY_LEN chars after strip.
    Valid:   either field is JSON null — the model's sentinel for a side that
             genuinely has nothing (e.g. uniformly negative reviews → likes: null).
    Invalid: empty string or string shorter than _MIN_SUMMARY_LEN — that is
             the truncation/hallucination shape, not the null sentinel.
    Invalid: missing key, non-string/non-null value, unparseable JSON.
    """
    try:
        d = json.loads(_strip_fence(text))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or "likes" not in d or "dislikes" not in d:
        return None
    for val in (d["likes"], d["dislikes"]):
        if val is not None:
            if not isinstance(val, str) or len(val.strip()) < _MIN_SUMMARY_LEN:
                return None
    return {"likes": d["likes"], "dislikes": d["dislikes"]}


def _build_reviews_text(reviews: list[Review]) -> str:
    lines = []
    for i, r in enumerate(reviews, 1):
        pub = f" ({r.publication})" if r.publication else ""
        quote = (r.quote or "")[:_REVIEW_MAX_CHARS]
        lines.append(f"[{i}] Score: {r.score}/100{pub}\n{quote}")
    return "\n\n".join(lines)


LLMCallable = Callable[[list[dict]], "str | None"]


def generate_summaries(
    session: Session,
    game: Game,
    llm: LLMCallable,
    *,
    skip_existing: bool = False,
) -> tuple:
    """
    Generate and persist critic + user summaries for a game.

    Returns (critic_written, user_written, llm_errors).
      critic_written / user_written — True if a summary row was written.
      llm_errors — count of times the LLM was called but failed (returned None
                   or malformed JSON).  Zero if there were no reviews to
                   summarise (that is a skip, not an error).
                   The crawl loop stores this in CrawlRun.llm_errors so
                   failures are visible in /health and the monitor.
      skip_existing — when True, do not re-generate summaries that are already
                      present.  Used by the backfill command to avoid wasting
                      LLM calls on games that were successfully summarised
                      during a previous crawl.
    """
    from sqlalchemy import select as _select

    all_reviews: list[Review] = (
        session.query(Review).filter_by(game_id=game.id).all()
    )

    existing_kinds: set = set()
    if skip_existing:
        existing_kinds = set(
            session.execute(
                _select(Summary.kind).where(Summary.game_id == game.id)
            ).scalars().all()
        )

    _n = min(settings.llm_max_reviews, _PROMPT_MAX_REVIEWS)
    critic_written, c_failed = _summarise_kind(
        session, game, all_reviews,
        kind="critic",
        sampler=lambda r: _sample_critic(r, _n),
        llm=llm,
        already_exists="critic" in existing_kinds,
    )
    user_written, u_failed = _summarise_kind(
        session, game, all_reviews,
        kind="user",
        sampler=lambda r: _sample_user(r, _n),
        llm=llm,
        already_exists="user" in existing_kinds,
    )
    return critic_written, user_written, int(c_failed) + int(u_failed)


def _summarise_kind(
    session: Session,
    game: Game,
    all_reviews: list[Review],
    *,
    kind: str,
    sampler: Callable,
    llm: LLMCallable,
    already_exists: bool = False,
) -> tuple:
    """Returns (written: bool, failed: bool)."""
    if already_exists:
        log.debug("skip %s summary for %s — already exists", kind, game.slug)
        return False, False
    picked, rule = sampler(all_reviews)
    if not picked:
        log.debug("skip %s summary for %s — no %s reviews with quotes", kind, game.slug, kind)
        return False, False

    prompt_text = _PROMPT.format(
        n=len(picked),
        kind=kind,
        title=game.title,
        reviews_text=_build_reviews_text(picked),
    )
    messages = [
        {"role": "system", "content": "You summarise game reviews concisely and accurately."},
        {"role": "user", "content": prompt_text},
    ]

    raw = llm(messages)
    if raw is None:
        log.warning("LLM returned None for %s %s summary — counted as llm_error", game.slug, kind)
        return False, True

    parsed = _parse_llm_response(raw)
    if parsed is None:
        log.warning(
            "unparseable LLM response for %s %s: %r — counted as llm_error",
            game.slug, kind, raw[:200],
        )
        return False, True

    session.execute(
        delete(Summary).where(
            Summary.game_id == game.id,
            Summary.kind == kind,
        )
    )
    session.add(Summary(
        game_id=game.id,
        kind=kind,
        likes=parsed["likes"],
        dislikes=parsed["dislikes"],
        model=settings.llm_model,
        source_review_count=len(picked),
        sampling_rule=rule,
        generated_at=datetime.utcnow(),
    ))
    return True, False
