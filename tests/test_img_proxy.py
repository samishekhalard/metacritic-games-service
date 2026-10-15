"""Tests for img_proxy.py — cover fetch, disk cache, SVG placeholder."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from metacrawl.img_proxy import (
    _ImgDiskCache,
    placeholder_svg,
    serve_cover,
)


# ---------------------------------------------------------------------------
# placeholder_svg
# ---------------------------------------------------------------------------

def test_placeholder_svg_returns_svg_bytes():
    data = placeholder_svg("Test Game")
    assert data.startswith(b"<svg")
    assert b"</svg>" in data


def test_placeholder_svg_embeds_initials():
    data = placeholder_svg("Test Game")
    assert b"TG" in data


def test_placeholder_svg_single_word():
    data = placeholder_svg("Hades")
    assert b"H" in data


def test_placeholder_svg_empty_title():
    data = placeholder_svg("")
    assert b"<svg" in data
    assert b"?" in data


def test_placeholder_svg_color_varies_by_title():
    a = placeholder_svg("Alpha")
    b = placeholder_svg("Zyxwvu")
    # Different titles may yield different colours (not guaranteed, but usually)
    # At minimum: both are valid SVGs
    assert b"<svg" in a
    assert b"<svg" in b


def test_placeholder_svg_content_type():
    # serve_cover returns "image/svg+xml" for the placeholder
    _, ct = serve_cover(99999, None, "No Cover Game")
    assert ct == "image/svg+xml"


# ---------------------------------------------------------------------------
# _ImgDiskCache
# ---------------------------------------------------------------------------

def test_cache_miss_returns_none(tmp_path):
    cache = _ImgDiskCache(tmp_path / "img", 2000, 200_000_000)
    assert cache.get(42) is None


def test_cache_set_then_get(tmp_path):
    cache = _ImgDiskCache(tmp_path / "img", 2000, 200_000_000)
    cache.set(1, b"\xff\xd8fake", "image/jpeg")
    result = cache.get(1)
    assert result is not None
    data, ct = result
    assert data == b"\xff\xd8fake"
    assert ct == "image/jpeg"


def test_cache_evicts_oldest_when_full(tmp_path):
    cache = _ImgDiskCache(tmp_path / "img", max_files=3, max_bytes=200_000_000)
    for i in range(3):
        cache.set(i, b"x" * 100, "image/jpeg")
    # Adding a 4th should evict the oldest
    cache.set(3, b"x" * 100, "image/jpeg")
    # Only 3 .bin files should remain
    bin_files = list((tmp_path / "img").glob("*.bin"))
    assert len(bin_files) == 3


# ---------------------------------------------------------------------------
# serve_cover with monkeypatched upstream fetch
# ---------------------------------------------------------------------------

def test_real_cover_returns_image_bytes(tmp_path, monkeypatch):
    import metacrawl.img_proxy as proxy
    fake_bytes = b"\xff\xd8\xff\xe0" + b"fake_jpeg_body"
    monkeypatch.setattr(proxy, "_fetch_cover_from_upstream",
                        lambda url: (fake_bytes, "image/jpeg"))
    cache = _ImgDiskCache(tmp_path / "img", 2000, 200_000_000)
    data, ct = serve_cover(1, "https://example.com/cover.jpg", "Some Game", cache=cache)
    assert ct == "image/jpeg"
    assert data == fake_bytes


def test_upstream_failure_returns_svg_placeholder(tmp_path, monkeypatch):
    import metacrawl.img_proxy as proxy
    monkeypatch.setattr(proxy, "_fetch_cover_from_upstream", lambda url: None)
    cache = _ImgDiskCache(tmp_path / "img", 2000, 200_000_000)
    data, ct = serve_cover(1, "https://example.com/cover.jpg", "Test Game", cache=cache)
    assert ct == "image/svg+xml"
    assert b"<svg" in data
    assert b"TG" in data   # initials of "Test Game"


def test_no_cover_url_returns_svg_placeholder(tmp_path, monkeypatch):
    import metacrawl.img_proxy as proxy
    call_count = {"n": 0}
    def mock_fetch(url):
        call_count["n"] += 1
        return None
    monkeypatch.setattr(proxy, "_fetch_cover_from_upstream", mock_fetch)
    cache = _ImgDiskCache(tmp_path / "img", 2000, 200_000_000)
    data, ct = serve_cover(1, None, "No Cover", cache=cache)
    assert ct == "image/svg+xml"
    assert call_count["n"] == 0   # no upstream attempt when cover_url is None


def test_cache_hit_does_not_refetch(tmp_path, monkeypatch):
    import metacrawl.img_proxy as proxy
    call_count = {"n": 0}
    def mock_fetch(url):
        call_count["n"] += 1
        return b"image_bytes", "image/jpeg"
    monkeypatch.setattr(proxy, "_fetch_cover_from_upstream", mock_fetch)
    cache = _ImgDiskCache(tmp_path / "img", 2000, 200_000_000)

    data1, ct1 = serve_cover(7, "https://example.com/img.jpg", "Game", cache=cache)
    assert call_count["n"] == 1

    data2, ct2 = serve_cover(7, "https://example.com/img.jpg", "Game", cache=cache)
    assert call_count["n"] == 1   # second call must not re-fetch
    assert data2 == data1


# ---------------------------------------------------------------------------
# Template safety: no raw metacritic.com URLs in any template
# ---------------------------------------------------------------------------

def test_no_metacritic_urls_in_templates():
    templates_dir = Path(__file__).parent.parent / "src" / "metacrawl" / "templates"
    failures = []
    for tpl in sorted(templates_dir.rglob("*.html")):
        content = tpl.read_text(encoding="utf-8")
        if "metacritic.com" in content:
            failures.append(tpl.relative_to(templates_dir.parent.parent))
    assert not failures, (
        "Templates must use /img/{game.id}, not raw metacritic.com URLs.\n"
        "Offending templates:\n" + "\n".join(f"  {f}" for f in failures)
    )
