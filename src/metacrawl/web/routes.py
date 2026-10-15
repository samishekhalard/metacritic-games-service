"""
Web routes: game list, game detail, crawl trigger/status, health check.
"""
from __future__ import annotations

import hashlib as _hashlib
import hmac as _hmac
import time as _time
from pathlib import Path

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..config import settings
from ..db.models import CrawlRun, Embedding, Game, PlatformScore, Review, Summary, game_genres
from ..embed import embedder_enabled
from ..similar import find_similar

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_TOKEN_TTL = 300  # seconds


def mint_crawl_token(ttl: int = _TOKEN_TTL) -> str:
    """
    Mint a short-lived HMAC-SHA256 token for the browser crawl button.
    Format: "{expiry_unix}:{sha256_hex}"
    Returns "" when MC_CRAWL_SECRET is not configured.
    """
    if not settings.crawl_secret:
        return ""
    expiry = int(_time.time()) + ttl
    sig = _hmac.new(
        settings.crawl_secret.encode(),
        f"crawl:{expiry}".encode(),
        _hashlib.sha256,
    ).hexdigest()
    return f"{expiry}:{sig}"


def _verify_crawl_token(token: str) -> bool:
    """Return True iff the token has a valid signature and has not expired."""
    if not settings.crawl_secret or not token:
        return False
    parts = token.split(":", 1)
    if len(parts) != 2:
        return False
    try:
        expiry = int(parts[0])
    except ValueError:
        return False
    if _time.time() > expiry:
        return False
    expected = _hmac.new(
        settings.crawl_secret.encode(),
        f"crawl:{expiry}".encode(),
        _hashlib.sha256,
    ).hexdigest()
    return _hmac.compare_digest(expected, parts[1])


# Expose token minting to Jinja2 templates so base.html can embed it in
# the crawl button's hx-headers without the secret ever reaching the browser.
templates.env.globals["mint_crawl_token"] = mint_crawl_token

router = APIRouter()

_PER_PAGE = 24


@router.get("/", response_class=HTMLResponse)
def games_list(
    request: Request,
    q: str = "",
    platform: str = "",
    sort: str = "score",
    page: int = 1,
):
    Session = request.app.state.session_factory

    with Session() as session:
        all_platforms = session.execute(
            select(PlatformScore.platform)
            .distinct()
            .order_by(PlatformScore.platform)
        ).scalars().all()

        query = session.query(Game)
        if q:
            query = query.filter(Game.title.ilike(f"%{q}%"))
        if platform:
            query = (
                query.join(PlatformScore, PlatformScore.game_id == Game.id)
                .filter(PlatformScore.platform == platform)
                .distinct()
            )

        sort_expr = {
            "score": Game.metascore.desc().nullslast(),
            "title": Game.title.asc(),
            "date": Game.release_date.desc().nullslast(),
        }.get(sort, Game.metascore.desc().nullslast())
        query = query.order_by(sort_expr)

        total = query.count()
        games = query.offset((page - 1) * _PER_PAGE).limit(_PER_PAGE).all()

        game_platforms: dict = {}
        for game in games:
            game_platforms[game.id] = session.execute(
                select(PlatformScore.platform).where(PlatformScore.game_id == game.id)
            ).scalars().all()

    pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
    ctx = {
        "request": request,
        "games": games,
        "game_platforms": game_platforms,
        "all_platforms": all_platforms,
        "q": q,
        "platform": platform,
        "sort": sort,
        "page": page,
        "total": total,
        "per_page": _PER_PAGE,
        "pages": pages,
    }

    is_htmx = bool(request.headers.get("HX-Request"))
    tpl = "partials/results.html" if is_htmx else "games.html"
    return templates.TemplateResponse(tpl, ctx)


@router.get("/games/{slug}", response_class=HTMLResponse)
def game_detail(request: Request, slug: str):
    Session = request.app.state.session_factory

    with Session() as session:
        game = session.query(Game).filter_by(slug=slug).first()
        if game is None:
            return HTMLResponse(
                "<h1 style='font-family:sans-serif;padding:2rem'>Game not found</h1>",
                status_code=404,
            )

        genres = session.execute(
            select(game_genres.c.genre).where(game_genres.c.game_id == game.id)
        ).scalars().all()

        platform_scores = session.execute(
            select(PlatformScore).where(PlatformScore.game_id == game.id)
        ).scalars().all()

        critic_reviews = session.execute(
            select(Review)
            .where(Review.game_id == game.id, Review.kind == "critic")
            .order_by(Review.score.desc())
            .limit(20)
        ).scalars().all()

        user_reviews = session.execute(
            select(Review)
            .where(Review.game_id == game.id, Review.kind == "user")
            .order_by(Review.thumbs_up.desc().nullslast())
            .limit(20)
        ).scalars().all()

        summaries = {
            s.kind: s
            for s in session.execute(
                select(Summary).where(Summary.game_id == game.id)
            ).scalars().all()
        }

    return templates.TemplateResponse("game.html", {
        "request": request,
        "game": game,
        "genres": list(genres),
        "platform_scores": list(platform_scores),
        "critic_reviews": list(critic_reviews),
        "user_reviews": list(user_reviews),
        "summaries": summaries,
    })


@router.get("/games/{slug}/similar", response_class=HTMLResponse)
def game_similar(request: Request, slug: str):
    Session = request.app.state.session_factory
    with Session() as session:
        game = session.query(Game).filter_by(slug=slug).first()
        if game is None:
            return HTMLResponse("")
        similar = find_similar(session, game, k=5)
        # Load platforms for each result while the session is open
        result_platforms: dict = {}
        for r in similar:
            result_platforms[r.game.id] = session.execute(
                select(PlatformScore.platform).where(PlatformScore.game_id == r.game.id)
            ).scalars().all()
    # Note shown in UI when embedder is disabled or no embeddings exist yet
    embed_note = None
    if not embedder_enabled():
        embed_note = (
            "Embedder not configured — results use genre/platform overlap only. "
            "Set MC_EMBED_PROVIDER to enable text-similarity."
        )
    elif similar and all(r.method == "metadata_overlap" for r in similar):
        embed_note = "No text embeddings yet — run a crawl to generate them."

    return templates.TemplateResponse("partials/similar.html", {
        "request": request,
        "similar": similar,
        "result_platforms": result_platforms,
        "embed_note": embed_note,
    })


@router.post("/api/crawl/run", response_class=HTMLResponse)
def trigger_crawl(
    request: Request,
    x_crawl_secret: str = Header(default=""),
    x_crawl_token: str = Header(default=""),
):
    if x_crawl_token:
        # Browser path: page-render HMAC token, valid for _TOKEN_TTL seconds.
        # The token is minted server-side and embedded in hx-headers; the raw
        # secret never reaches the browser.
        if not settings.crawl_secret:
            raise HTTPException(status_code=403, detail="Crawl secret not configured")
        if not _verify_crawl_token(x_crawl_token):
            raise HTTPException(status_code=403, detail="Invalid or expired crawl token")
    else:
        # Programmatic path: static shared secret (GitHub Actions, curl).
        if not settings.crawl_secret:
            raise HTTPException(status_code=403, detail="Crawl secret not configured")
        if x_crawl_secret != settings.crawl_secret:
            raise HTTPException(status_code=403, detail="Invalid crawl secret")
    worker = request.app.state.worker
    started = worker.submit(trigger="manual")
    cls = "badge-running" if started else "badge-idle"
    label = "Running..." if started else "Already running"
    return HTMLResponse(
        f'<span id="crawl-badge" class="badge {cls}"'
        f' hx-get="/api/crawl/status" hx-trigger="every 3s" hx-swap="outerHTML"'
        f' hx-target="#crawl-badge">{label}</span>'
    )


@router.get("/api/crawl/status", response_class=HTMLResponse)
def crawl_status(request: Request):
    worker = request.app.state.worker
    running = worker.is_running
    cls = "badge-running" if running else "badge-idle"
    label = "Running..." if running else "Idle"
    trigger = "every 3s" if running else "every 10s"
    return HTMLResponse(
        f'<span id="crawl-badge" class="badge {cls}"'
        f' hx-get="/api/crawl/status" hx-trigger="{trigger}" hx-swap="outerHTML"'
        f' hx-target="#crawl-badge">{label}</span>'
    )


# ── Health check ──────────────────────────────────────────────────────────────

def _ping(base_url: str, api_key: str, timeout: float = 3.0) -> bool:
    """
    Check whether an OpenAI-compatible provider is reachable.
    Hits GET /models (returns any response including 4xx = reachable).
    Returns False only on connection error or timeout.
    """
    try:
        url = base_url.rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}"}
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            return resp.status_code < 500
    except Exception:
        return False


def _health_payload(request: Request) -> dict:
    Session = request.app.state.session_factory

    with Session() as session:
        game_count    = session.query(Game).count()
        summary_count = session.query(Summary).count()
        review_count  = session.query(Review).count()
        embed_count   = session.query(Embedding).count()
        last_run = (
            session.query(CrawlRun)
            .order_by(CrawlRun.id.desc())
            .first()
        )
        last_ok_run = (
            session.query(CrawlRun)
            .filter(CrawlRun.status == "done")
            .order_by(CrawlRun.id.desc())
            .first()
        )

    llm_reachable = _ping(settings.llm_base_url, settings.llm_api_key)
    embed_cfg = embedder_enabled()
    embed_reachable = _ping(settings.llm_base_url, settings.llm_api_key) if embed_cfg else None

    return {
        "db": {
            "ok": True,
            "games": game_count,
            "summaries": summary_count,
            "reviews": review_count,
            "embeddings": embed_count,
        },
        "llm": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "reachable": llm_reachable,
        },
        "embedder": {
            "enabled": embed_cfg,
            "provider": settings.embed_provider if embed_cfg else None,
            "model": settings.llm_embed_model if embed_cfg else None,
            "reachable": embed_reachable,
            "note": None if embed_cfg else (
                "Set MC_EMBED_PROVIDER to enable text-similarity; "
                "similarity falls back to genre/platform overlap."
            ),
        },
        "last_crawl": {
            "id": last_run.id,
            "started_at": str(last_run.started_at),
            "status": last_run.status,
            "source": last_run.source,
            "games_created": last_run.games_created,
            "errors": last_run.errors,
            "llm_errors": getattr(last_run, "llm_errors", 0),
            "embed_errors": getattr(last_run, "embed_errors", 0),
        } if last_run else None,
        "last_successful_crawl": {
            "id": last_ok_run.id,
            "started_at": str(last_ok_run.started_at),
            "games_created": last_ok_run.games_created,
        } if last_ok_run else None,
        "crawl_worker_running": request.app.state.worker.is_running,
    }


@router.get("/health")
def health(request: Request):
    """Liveness check — always HTTP 200 when the app is serving."""
    return JSONResponse(content=_health_payload(request), status_code=200)


@router.get("/health/ready")
def health_ready(request: Request):
    """Readiness check — HTTP 503 when the LLM provider is unreachable."""
    payload = _health_payload(request)
    status_code = 200 if payload["llm"]["reachable"] else 503
    return JSONResponse(content=payload, status_code=status_code)
