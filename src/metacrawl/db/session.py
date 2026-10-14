from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from ..config import settings
from .models import Base


def make_engine(url: str | None = None):
    return create_engine(url or settings.db_url)


def init_db(engine=None) -> None:
    """Create all tables (if absent) then apply incremental column migrations."""
    e = engine or make_engine()
    Base.metadata.create_all(e)
    _migrate(e)


def _migrate(engine) -> None:
    """
    Add columns introduced after the initial schema without dropping existing data.

    SQLAlchemy's create_all only creates missing *tables*, not missing *columns*
    inside existing tables.  For each new column we attempt an ALTER TABLE; the
    exception is swallowed when the column already exists (SQLite has no
    IF NOT EXISTS clause for ALTER TABLE ADD COLUMN).
    """
    new_cols = [
        ("crawl_runs", "llm_errors",   "INTEGER NOT NULL DEFAULT 0"),
        ("crawl_runs", "embed_errors",  "INTEGER NOT NULL DEFAULT 0"),
    ]
    with engine.begin() as conn:
        for table, col, ddl in new_cols:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            except Exception:
                pass  # column already exists


def make_session_factory(engine=None):
    return sessionmaker(bind=engine or make_engine())
