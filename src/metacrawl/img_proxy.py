"""
Image proxy: fetch upstream game covers, cache to disk, always return 200.

WHY THIS EXISTS
───────────────
Metacritic CDN URLs require both a content hash AND query parameters
(?auto=webp&fit=cover&height=…&quality=70&width=…). Both must match the
same resize operation — the CDN returns 403 without them. Serving covers
directly from templates means every broken URL is a broken image in the
browser; there is no fallback path. This proxy:

  - fetches with browser-like headers that satisfy the CDN
  - caches bytes to disk so repeated loads cost nothing
  - returns a generated SVG placeholder (game initials + colour) on any
    failure, so the browser always sees a valid image

ENDPOINT
────────
  GET /img/{game_id}   — served from routes.py
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import httpx

from .config import settings

_MAX_FILES = 2000
_MAX_SIZE_BYTES = 200 * 1024 * 1024  # 200 MB

# The Metacritic CDN requires these headers; without Accept: image/* and
# the Referer it returns 403 even for URLs with valid query params.
_CDN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.metacritic.com/",
}

_PALETTE = [
    "#3a6186", "#5c3d6e", "#1a6b4a", "#7a3c1e",
    "#2d5a8e", "#6e4a1a", "#1e6b5c", "#5a1e5c",
]


def placeholder_svg(title: str) -> bytes:
    """SVG placeholder with the game's initials on a colour derived from its title."""
    words = title.split() if title else []
    sig = [w for w in words if w and w[0].isalpha()]
    initials = "".join(w[0].upper() for w in sig[:2]) or (title[:1].upper() if title else "?")
    colour = _PALETTE[sum(ord(c) for c in title) % len(_PALETTE)] if title else _PALETTE[0]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="96" height="144" viewBox="0 0 96 144">'
        f'<rect width="96" height="144" fill="{colour}"/>'
        '<text x="48" y="72" font-family="sans-serif" font-size="32" '
        'font-weight="bold" fill="white" text-anchor="middle" '
        f'dominant-baseline="middle">{initials}</text>'
        "</svg>"
    )
    return svg.encode()


class _ImgDiskCache:
    """
    File-based LRU image cache.

    Each entry is two files: {game_id}.bin (bytes) and {game_id}.ct (MIME type).
    Recency is tracked by mtime — every cache hit updates mtime via touch().
    Eviction removes the oldest files until both count and size are within limits.
    """

    def __init__(self, directory: Path, max_files: int, max_bytes: int) -> None:
        self._dir = directory
        self._max_files = max_files
        self._max_bytes = max_bytes
        self._dir.mkdir(parents=True, exist_ok=True)

    def _bin(self, game_id: int) -> Path:
        return self._dir / f"{game_id}.bin"

    def _ct(self, game_id: int) -> Path:
        return self._dir / f"{game_id}.ct"

    def get(self, game_id: int) -> "tuple[bytes, str] | None":
        b = self._bin(game_id)
        if not b.exists():
            return None
        try:
            data = b.read_bytes()
            m = self._ct(game_id)
            ct = m.read_text().strip() if m.exists() else "image/jpeg"
            b.touch()
            if m.exists():
                m.touch()
            return data, ct
        except OSError:
            return None

    def set(self, game_id: int, data: bytes, content_type: str) -> None:
        self._evict_if_needed()
        self._bin(game_id).write_bytes(data)
        self._ct(game_id).write_text(content_type)

    def _evict_if_needed(self) -> None:
        bins = sorted(self._dir.glob("*.bin"), key=lambda f: f.stat().st_mtime)
        total = sum(f.stat().st_size for f in bins)
        while (len(bins) >= self._max_files or total > self._max_bytes) and bins:
            old = bins.pop(0)
            try:
                total -= old.stat().st_size
            except OSError:
                pass
            old.unlink(missing_ok=True)
            old.with_suffix(".ct").unlink(missing_ok=True)


def _fetch_cover_from_upstream(url: str) -> "tuple[bytes, str] | None":
    """GET the cover URL with browser headers. Returns (bytes, content_type) or None."""
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            r = client.get(url, headers=_CDN_HEADERS)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and ct.startswith("image/"):
            return r.content, ct.split(";")[0].strip()
    except Exception:
        pass
    return None


_default_cache = _ImgDiskCache(
    settings.cache_dir.parent / "img", _MAX_FILES, _MAX_SIZE_BYTES
)


def serve_cover(
    game_id: int,
    cover_url: "str | None",
    title: str,
    *,
    cache: "Optional[_ImgDiskCache]" = None,
) -> "tuple[bytes, str]":
    """
    Return (bytes, content_type) for a game cover.

    Order: disk cache → upstream fetch → SVG placeholder.
    Always succeeds; never raises.
    """
    c = cache if cache is not None else _default_cache

    hit = c.get(game_id)
    if hit:
        return hit

    if cover_url:
        result = _fetch_cover_from_upstream(cover_url)
        if result:
            data, ct = result
            c.set(game_id, data, ct)
            return data, ct

    return placeholder_svg(title), "image/svg+xml"
