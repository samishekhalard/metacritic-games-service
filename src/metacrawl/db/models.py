"""
SQLAlchemy 2.0 models for the full schema described in BRIEF.md §3.
All columns that come from external sources are nullable — unreleased games
have nulls everywhere and that is the normal case.
"""
from __future__ import annotations

from sqlalchemy import (
    Column, DateTime, Float, ForeignKey, Integer, String, Table, Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


# Simple join table: game_id → genre name string.  No separate genres entity
# because Metacritic genres have null ids and names are the stable key anyway.
game_genres = Table(
    "game_genres",
    Base.metadata,
    Column("game_id", Integer, ForeignKey("games.id", ondelete="CASCADE"), primary_key=True),
    Column("genre", String, primary_key=True),
)


class Game(Base):
    __tablename__ = "games"

    id = Column(Integer, primary_key=True)
    external_id = Column(Integer, unique=True, nullable=False)
    slug = Column(String, unique=True, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text)
    developer = Column(String)
    publisher = Column(String)
    cover_url = Column(String)
    video_url = Column(String)
    release_date = Column(String)        # "YYYY-MM-DD" or null
    rating = Column(String)              # "M", "E10+", …
    metascore = Column(Integer)
    userscore = Column(Float)
    first_seen_at = Column(DateTime, nullable=False)
    last_crawled_at = Column(DateTime, nullable=False)

    platform_scores = relationship(
        "PlatformScore", back_populates="game",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    reviews = relationship(
        "Review", back_populates="game",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    summaries = relationship(
        "Summary", back_populates="game",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    embeddings = relationship(
        "Embedding", back_populates="game",
        cascade="all, delete-orphan", passive_deletes=True,
    )


class PlatformScore(Base):
    __tablename__ = "platform_scores"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    platform = Column(String, nullable=False)
    metascore = Column(Integer)
    userscore = Column(Float)
    critic_reviews = Column(Integer, default=0)
    user_reviews = Column(Integer, default=0)

    game = relationship("Game", back_populates="platform_scores")


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String, nullable=False)    # 'critic' | 'user'
    author = Column(String)
    publication = Column(String)
    score = Column(Integer)
    quote = Column(Text)
    review_date = Column(String)
    thumbs_up = Column(Integer)
    thumbs_down = Column(Integer)

    game = relationship("Game", back_populates="reviews")


class Summary(Base):
    __tablename__ = "summaries"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    kind = Column(String, nullable=False)    # 'critic' | 'user'
    likes = Column(Text)
    dislikes = Column(Text)
    model = Column(String)
    source_review_count = Column(Integer)
    sampling_rule = Column(String)           # e.g. "top40_score", "top40_recent"
    generated_at = Column(DateTime)

    game = relationship("Game", back_populates="summaries")


class Embedding(Base):
    __tablename__ = "embeddings"

    id = Column(Integer, primary_key=True)
    game_id = Column(Integer, ForeignKey("games.id", ondelete="CASCADE"), nullable=False)
    vector = Column(Text)    # JSON-serialised float list
    model = Column(String)

    game = relationship("Game", back_populates="embeddings")


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    trigger = Column(String)          # 'schedule' | 'manual'
    source = Column(String)           # 'new-releases' | 'browse'
    page = Column(Integer)
    games_seen = Column(Integer, default=0)
    games_created = Column(Integer, default=0)
    games_updated = Column(Integer, default=0)
    games_skipped = Column(Integer, default=0)   # already processed today
    errors = Column(Integer, default=0)
    status = Column(String)           # 'running' | 'done' | 'failed'
    next_browse_url = Column(String)  # links.next.href; read by next browse run
