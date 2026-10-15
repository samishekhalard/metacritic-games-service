"""
Tests for POST /api/crawl/run auth — crawl secret enforcement.

All offline; no DB or scheduler involved.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from metacrawl.web.routes import router


@pytest.fixture
def crawl_client():
    """Minimal FastAPI app with just the crawl router and a mock worker."""
    app = FastAPI()
    app.include_router(router)
    worker = MagicMock()
    worker.submit.return_value = True
    worker.is_running = False
    app.state.worker = worker
    app.state.session_factory = MagicMock()
    return TestClient(app)


def test_crawl_run_refuses_when_secret_not_configured(monkeypatch, crawl_client):
    """MC_CRAWL_SECRET unset → 403, never runs open."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "")
    resp = crawl_client.post("/api/crawl/run")
    assert resp.status_code == 403


def test_crawl_run_refuses_with_wrong_secret(monkeypatch, crawl_client):
    """Correct secret configured, wrong header → 403."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "correct-secret")
    resp = crawl_client.post(
        "/api/crawl/run", headers={"X-Crawl-Secret": "wrong-secret"}
    )
    assert resp.status_code == 403


def test_crawl_run_refuses_when_secret_header_missing(monkeypatch, crawl_client):
    """Correct secret configured, no header → 403."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "correct-secret")
    resp = crawl_client.post("/api/crawl/run")
    assert resp.status_code == 403


def test_crawl_run_accepts_correct_secret(monkeypatch, crawl_client):
    """Correct secret configured and provided → 200."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "correct-secret")
    resp = crawl_client.post(
        "/api/crawl/run", headers={"X-Crawl-Secret": "correct-secret"}
    )
    assert resp.status_code == 200


def test_crawl_run_allows_valid_token(monkeypatch, crawl_client):
    """Valid HMAC token (minted server-side for the button) → 200."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "test-secret")
    from metacrawl.web.routes import mint_crawl_token
    token = mint_crawl_token()
    resp = crawl_client.post("/api/crawl/run", headers={"X-Crawl-Token": token})
    assert resp.status_code == 200


def test_crawl_run_rejects_expired_token(monkeypatch, crawl_client):
    """Token with expiry in the past → 403."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "test-secret")
    import hashlib, hmac, time
    expiry = int(time.time()) - 1
    sig = hmac.new(b"test-secret", f"crawl:{expiry}".encode(), hashlib.sha256).hexdigest()
    token = f"{expiry}:{sig}"
    resp = crawl_client.post("/api/crawl/run", headers={"X-Crawl-Token": token})
    assert resp.status_code == 403


def test_crawl_run_rejects_forged_token(monkeypatch, crawl_client):
    """Correct expiry but wrong signature → 403."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "test-secret")
    import time
    expiry = int(time.time()) + 300
    token = f"{expiry}:deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    resp = crawl_client.post("/api/crawl/run", headers={"X-Crawl-Token": token})
    assert resp.status_code == 403


def test_crawl_run_rejects_bare_hx_request(monkeypatch, crawl_client):
    """HX-Request: true alone is not authentication — the HMAC token is required."""
    monkeypatch.setattr("metacrawl.web.routes.settings.crawl_secret", "test-secret")
    resp = crawl_client.post("/api/crawl/run", headers={"HX-Request": "true"})
    assert resp.status_code == 403
