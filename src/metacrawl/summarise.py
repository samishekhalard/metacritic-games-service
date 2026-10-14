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

MALFORMED RESPONSE:
  If the model returns something that is not {"likes": str, "dislikes": str},
  the summary is silently dropped (logged at WARNING).  Storing garbage is
  worse than storing nothing — a missing summary is obviously absent; bad text
  would be read as fact.
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
{{"likes": "2-3 sentences on what reviewers praised",\
 "dislikes": "2-3 sentences on what reviewers criticised or found lacking"}}"""

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


def _parse_llm_response(text: str) -> dict | None:
    """Return {"likes": str, "dislikes": str} or None if unparseable/invalid."""
    try:
        d = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if (
        isinstance(d, dict)
        and isinstance(d.get("likes"), str)
        and isinstance(d.get("dislikes"), str)
        and d["likes"].strip()
        and d["dislikes"].strip()
    ):
        return {"likes": d["likes"], "dislikes": d["dislikes"]}
    return None


def _build_reviews_text(reviews: list[Review]) -> str:
    lines = []
    for i, r in enumerate(reviews, 1):
        pub = f" ({r.publication})" if r.publication else ""
        lines.append(f"[{i}] Score: {r.score}/100{pub}\n{r.quote}")
    return "\n\n".join(lines)


LLMCallable = Callable[[list[dict]], "str | None"]


def generate_summaries(
    session: Session,
    game: Game,
    llm: LLMCallable,
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
    """
    all_reviews: list[Review] = (
        session.query(Review).filter_by(game_id=game.id).all()
    )

    critic_written, c_failed = _summarise_kind(
        session, game, all_reviews,
        kind="critic",
        sampler=lambda r: _sample_critic(r, settings.llm_max_reviews),
        llm=llm,
    )
    user_written, u_failed = _summarise_kind(
        session, game, all_reviews,
        kind="user",
        sampler=lambda r: _sample_user(r, settings.llm_max_reviews),
        llm=llm,
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
) -> tuple:
    """Returns (written: bool, failed: bool)."""
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
