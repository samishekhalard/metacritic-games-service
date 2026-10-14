from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from ..config import settings
from .models import Base


def make_engine(url: str | None = None):
    return create_engine(url or settings.db_url)


def init_db(engine=None) -> None:
    """Create all tables if they don't exist yet."""
    Base.metadata.create_all(engine or make_engine())


def make_session_factory(engine=None):
    return sessionmaker(bind=engine or make_engine())
