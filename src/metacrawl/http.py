"""
Rate-limited httpx client with file-system response cache.

One global connection pool, 1 request/second, every raw response written to
disk so re-running parsers during development costs zero extra requests.

`If-Modified-Since` is sent whenever the server returned `Last-Modified` on a
previous fetch; on 304 the cached body is returned without touching disk.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .config import settings


class _RateLimiter:
    def __init__(self, rps: float) -> None:
        self._interval = 1.0 / rps
        self._last: float | None = None   # None = no prior request

    def wait(self) -> None:
        if self._last is not None:
            gap = self._interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
        self._last = time.monotonic()


class _DiskCache:
    def __init__(self, directory: Path, ttl: int) -> None:
        self._dir = directory
        self._ttl = ttl
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, url: str) -> Path:
        key = hashlib.sha256(url.encode()).hexdigest()
        return self._dir / f"{key}.json"

    def get(self, url: str) -> dict | None:
        p = self._path(url)
        if not p.exists():
            return None
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return entry

    def is_fresh(self, entry: dict) -> bool:
        return (time.time() - entry.get("cached_at", 0)) < self._ttl

    def set(self, url: str, status: int, headers: dict, body: str) -> None:
        p = self._path(url)
        p.write_text(
            json.dumps(
                {"url": url, "status": status, "headers": headers,
                 "body": body, "cached_at": time.time()},
            ),
            encoding="utf-8",
        )


def _build_url(base: str, params: dict | None) -> str:
    if not params:
        return base
    return f"{base}?{urllib.parse.urlencode(params)}"


class HttpClient:
    """Single httpx.Client with rate limiting and disk cache."""

    def __init__(
        self,
        *,
        cache_dir: Path | None = None,
        cache_ttl: int | None = None,
        rps: float | None = None,
    ) -> None:
        self._limiter = _RateLimiter(rps if rps is not None else settings.rate_limit_rps)
        self._cache = _DiskCache(
            cache_dir if cache_dir is not None else settings.cache_dir,
            cache_ttl if cache_ttl is not None else settings.cache_ttl,
        )
        self._http = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
            timeout=30.0,
        )

    def get_text(self, url: str, params: dict | None = None) -> str:
        full_url = _build_url(url, params)
        entry = self._cache.get(full_url)

        if entry and self._cache.is_fresh(entry):
            return entry["body"]

        req_headers: dict[str, str] = {}
        if entry:
            last_mod = entry.get("headers", {}).get("last-modified")
            if last_mod:
                req_headers["If-Modified-Since"] = last_mod

        self._limiter.wait()
        r = self._http.get(full_url, headers=req_headers)

        if r.status_code == 304 and entry:
            return entry["body"]

        r.raise_for_status()
        self._cache.set(full_url, r.status_code, dict(r.headers), r.text)
        return r.text

    def get_json(self, url: str, params: dict | None = None) -> Any:
        return json.loads(self.get_text(url, params))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
