"""Shared fixtures — all offline, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from metacrawl.db.models import Base

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def browse_html() -> str:
    return (FIXTURES / "browse.html").read_text(encoding="utf-8")


@pytest.fixture
def game_html() -> str:
    return (FIXTURES / "game.html").read_text(encoding="utf-8")


@pytest.fixture
def critic_reviews_html() -> str:
    return (FIXTURES / "critic-reviews.html").read_text(encoding="utf-8")


@pytest.fixture
def user_reviews_html() -> str:
    return (FIXTURES / "user-reviews.html").read_text(encoding="utf-8")


@pytest.fixture
def api_new_releases() -> dict:
    return json.loads((FIXTURES / "api-new-releases.json").read_bytes())


@pytest.fixture
def api_page2() -> dict:
    return json.loads((FIXTURES / "api-page2.json").read_bytes())


@pytest.fixture
def db():
    """In-memory SQLite with all tables created.  Returns (Session factory, engine)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session, engine
