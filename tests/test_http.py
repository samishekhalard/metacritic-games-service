"""
Tests for metacrawl.http — rate limiting and disk cache.

No real network calls are made: the httpx transport is replaced with a fake.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from metacrawl.http import HttpClient, _DiskCache, _RateLimiter


# ---------------------------------------------------------------------------
# _DiskCache
# ---------------------------------------------------------------------------

def test_cache_miss_returns_none(tmp_path):
    cache = _DiskCache(tmp_path, ttl=3600)
    assert cache.get("https://example.com/missing") is None


def test_cache_set_then_get(tmp_path):
    cache = _DiskCache(tmp_path, ttl=3600)
    cache.set("https://example.com/a", 200, {"content-type": "application/json"}, '{"ok": true}')
    entry = cache.get("https://example.com/a")
    assert entry is not None
    assert entry["body"] == '{"ok": true}'
    assert entry["status"] == 200


def test_cache_is_fresh_within_ttl(tmp_path):
    cache = _DiskCache(tmp_path, ttl=3600)
    cache.set("https://example.com/b", 200, {}, "hello")
    entry = cache.get("https://example.com/b")
    assert cache.is_fresh(entry)


def test_cache_is_stale_after_ttl(tmp_path):
    cache = _DiskCache(tmp_path, ttl=0)   # 0-second TTL → always stale
    cache.set("https://example.com/c", 200, {}, "hello")
    entry = cache.get("https://example.com/c")
    assert not cache.is_fresh(entry)


def test_cache_different_urls_different_files(tmp_path):
    cache = _DiskCache(tmp_path, ttl=3600)
    cache.set("https://example.com/x", 200, {}, "body-x")
    cache.set("https://example.com/y", 200, {}, "body-y")
    assert cache.get("https://example.com/x")["body"] == "body-x"
    assert cache.get("https://example.com/y")["body"] == "body-y"


def test_cache_creates_directory(tmp_path):
    subdir = tmp_path / "deep" / "nested"
    cache = _DiskCache(subdir, ttl=3600)
    cache.set("https://example.com/z", 200, {}, "data")
    assert subdir.is_dir()


# ---------------------------------------------------------------------------
# _RateLimiter
# ---------------------------------------------------------------------------

def test_rate_limiter_does_not_sleep_on_first_call(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    limiter = _RateLimiter(rps=1.0)
    limiter.wait()
    assert slept == []


def test_rate_limiter_sleeps_on_second_call(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    limiter = _RateLimiter(rps=1.0)
    limiter._last = time.monotonic()   # pretend we just made a request
    limiter.wait()
    assert len(slept) == 1
    assert slept[0] > 0


# ---------------------------------------------------------------------------
# HttpClient — cache hit skips network
# ---------------------------------------------------------------------------

class _NeverCallTransport(httpx.BaseTransport):
    """Raises if any HTTP request is attempted."""
    def handle_request(self, request):
        raise AssertionError(f"unexpected network call to {request.url}")


def _client_with_prepopulated_cache(tmp_path, body: str, last_modified: str | None = None):
    """Return an HttpClient whose cache has one fresh entry and whose
    transport will raise if a network call is attempted."""
    url = "https://example.com/api"
    headers = {"content-type": "application/json"}
    if last_modified:
        headers["last-modified"] = last_modified

    client = HttpClient(cache_dir=tmp_path, cache_ttl=3600, rps=1000)
    client._cache.set(url, 200, headers, body)
    client._http = httpx.Client(transport=_NeverCallTransport())
    return client, url


def test_fresh_cache_hit_returns_body_without_network(tmp_path):
    client, url = _client_with_prepopulated_cache(tmp_path, '{"hit": true}')
    result = client.get_text(url)
    assert result == '{"hit": true}'


def test_fresh_cache_hit_parses_as_json(tmp_path):
    client, url = _client_with_prepopulated_cache(tmp_path, '{"n": 42}')
    assert client.get_json(url) == {"n": 42}


def test_fresh_cache_hit_with_params(tmp_path):
    url_base = "https://example.com/search"
    full_url = "https://example.com/search?q=elden+ring"
    client = HttpClient(cache_dir=tmp_path, cache_ttl=3600, rps=1000)
    client._cache.set(full_url, 200, {}, "cached-result")
    client._http = httpx.Client(transport=_NeverCallTransport())
    result = client.get_text(url_base, params={"q": "elden ring"})
    assert result == "cached-result"


# ---------------------------------------------------------------------------
# HttpClient — cache miss hits network and stores result
# ---------------------------------------------------------------------------

class _OnceFakeTransport(httpx.BaseTransport):
    """Returns a fixed response exactly once; raises on subsequent calls."""
    def __init__(self, body: str, headers: dict | None = None):
        self._body = body.encode()
        self._headers = headers or {"content-type": "application/json"}
        self._called = 0

    def handle_request(self, request):
        self._called += 1
        if self._called > 1:
            raise AssertionError("transport called more than once")
        return httpx.Response(200, headers=self._headers, content=self._body)


def test_cache_miss_fetches_and_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    url = "https://example.com/fresh"
    transport = _OnceFakeTransport('{"fresh": true}')
    client = HttpClient(cache_dir=tmp_path, cache_ttl=3600, rps=1000)
    client._http = httpx.Client(transport=transport)

    result = client.get_text(url)
    assert result == '{"fresh": true}'
    assert transport._called == 1

    # Second call must hit cache, not transport.
    client._http = httpx.Client(transport=_NeverCallTransport())
    result2 = client.get_text(url)
    assert result2 == '{"fresh": true}'


# ---------------------------------------------------------------------------
# HttpClient — stale cache sends If-Modified-Since
# ---------------------------------------------------------------------------

class _ConditionalTransport(httpx.BaseTransport):
    """Returns 304 if the request carries If-Modified-Since, 200 otherwise."""
    def __init__(self):
        self.received_headers = {}

    def handle_request(self, request):
        self.received_headers = dict(request.headers)
        if "if-modified-since" in request.headers:
            return httpx.Response(304)
        return httpx.Response(200, content=b'{"new": true}')


def test_stale_cache_with_last_modified_sends_conditional_request(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    url = "https://example.com/conditional"
    transport = _ConditionalTransport()

    client = HttpClient(cache_dir=tmp_path, cache_ttl=0, rps=1000)  # ttl=0 → always stale
    client._cache.set(url, 200, {"last-modified": "Wed, 01 Jan 2026 00:00:00 GMT"}, '{"old": true}')
    client._http = httpx.Client(transport=transport)

    result = client.get_text(url)
    assert "if-modified-since" in transport.received_headers
    assert result == '{"old": true}'   # 304 → returns cached body
