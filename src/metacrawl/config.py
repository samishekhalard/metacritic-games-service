from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_url: str = "sqlite:///metacrawl.db"
    cache_dir: Path = Path(".cache/http")
    # seconds before a cached entry is considered stale and revalidated
    cache_ttl: int = 3600
    rate_limit_rps: float = 1.0
    user_agent: str = (
        "metacrawl/0.1 (github.com/skytec-test; "
        "rate-limited 1 req/s, disk-cached; not a banned bot)"
    )

    # LLM — Ollama default; override with any OpenAI-compatible endpoint.
    # DAY_TZ note: "today" is defined in UTC throughout this service.
    # Midnight UTC is the day boundary; storing as naive datetimes (utcnow()).
    # To shift to another timezone set MC_DAY_TZ and update schedule.py.
    llm_base_url: str = "http://localhost:11434/v1"   # Ollama
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:7b"
    # cap per review kind before sending to LLM
    llm_max_reviews: int = 40

    model_config = {"env_prefix": "MC_"}


settings = Settings()
