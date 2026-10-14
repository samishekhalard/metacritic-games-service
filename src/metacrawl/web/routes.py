"""
Web routes: game list, game detail, crawl trigger/status.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..config import settings
from ..db.models import Game, PlatformScore, Review, Summary, game_genres
from ..similar import find_similar

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

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
    return templates.TemplateResponse("partials/similar.html", {
        "request": request,
        "similar": similar,
        "result_platforms": result_platforms,
    })


@router.post("/api/crawl/run", response_class=HTMLResponse)
def trigger_crawl(
    request: Request,
    x_crawl_secret: str = Header(default=""),
):
    if settings.crawl_secret and x_crawl_secret != settings.crawl_secret:
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
