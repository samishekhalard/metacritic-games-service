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

    # ── LLM provider ─────────────────────────────────────────────────────────
    # MC_LLM_PROVIDER = "ollama" | "openai-compatible"
    # Both values use the same OpenAI-compatible chat-completions API, so no
    # code changes are needed to switch.  Groq example:
    #   MC_LLM_PROVIDER=openai-compatible
    #   MC_LLM_BASE_URL=https://api.groq.com/openai/v1
    #   MC_LLM_API_KEY=gsk_...
    #   MC_LLM_MODEL=mixtral-8x7b-32768
    llm_provider: str = "ollama"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:7b"
    llm_max_reviews: int = 25     # cap per review kind before sending to LLM

    # ── Embedder provider ─────────────────────────────────────────────────────
    # MC_EMBED_PROVIDER = "" | "ollama" | "openai-compatible"
    # Empty string disables the embedder entirely.  When disabled, find_similar()
    # falls back to weighted Jaccard (genre 60 % + platform 40 %) for ALL games
    # and marks every result method="metadata_overlap".  The API response and UI
    # both say the embedder is not configured so the degradation is visible.
    embed_provider: str = "ollama"
    llm_embed_model: str = "nomic-embed-text"   # nomic-embed-text via Ollama

    # ── Scheduler ────────────────────────────────────────────────────────────
    # Set MC_SCHEDULER_ENABLED=false on Render; the hourly crawl is driven by
    # an external cron hitting POST /api/crawl/run instead.
    scheduler_enabled: bool = True

    # ── Shared secret ─────────────────────────────────────────────────────────
    # POST /api/crawl/run requires X-Crawl-Secret: <value>.
    # Unset (empty) = the endpoint refuses all requests.  Always set on Render.
    crawl_secret: str = ""

    model_config = {"env_prefix": "MC_"}


settings = Settings()
