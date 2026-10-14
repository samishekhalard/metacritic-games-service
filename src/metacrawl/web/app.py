"""
FastAPI application.

Startup: initialise DB, start CrawlWorker + hourly APScheduler job.
Shutdown: stop scheduler without waiting for jobs to finish.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..db.session import init_db, make_engine, make_session_factory
from ..schedule import make_scheduler
from ..worker import CrawlWorker
from .routes import router

_STATIC = Path(__file__).parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = make_engine()
    init_db(engine)
    session_factory = make_session_factory(engine)
    worker = CrawlWorker(session_factory)
    scheduler = make_scheduler(worker)
    scheduler.start()

    app.state.session_factory = session_factory
    app.state.worker = worker

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(title="Metacrawl", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
app.include_router(router)
