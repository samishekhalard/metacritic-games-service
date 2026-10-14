"""
Thin OpenAI-compatible chat completions client.

Uses httpx directly so the transport can be replaced in tests without
touching the openai package.  Works with Ollama (default) and any
OpenAI-compatible API; set MC_LLM_BASE_URL / MC_LLM_API_KEY / MC_LLM_MODEL.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import settings

log = logging.getLogger(__name__)


def call_llm(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    _transport: httpx.BaseTransport | None = None,
) -> str | None:
    """
    POST to {llm_base_url}/chat/completions with JSON-mode enabled.
    Returns the raw content string, or None if the call fails for any reason.

    `_transport` is a test seam — pass a custom transport to avoid real
    network calls without monkeypatching the module.
    """
    payload: dict[str, Any] = {
        "model": model or settings.llm_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    base = settings.llm_base_url.rstrip("/")
    try:
        kwargs: dict[str, Any] = dict(
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            json=payload,
            timeout=120.0,
        )
        if _transport is not None:
            client = httpx.Client(transport=_transport)
            r = client.post(f"{base}/chat/completions", **kwargs)
        else:
            r = httpx.post(f"{base}/chat/completions", **kwargs)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        log.exception("LLM call failed (model=%s)", model or settings.llm_model)
        return None
