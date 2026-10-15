"""Test 14 — /health endpoint."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_health_returns_200_and_db_ok(http):
    resp = http.get("/health")
    assert resp.status_code == 200, (
        f"GET /health returned {resp.status_code}, expected 200"
    )
    data = resp.json()
    assert data.get("db", {}).get("ok") is True, (
        f"Expected health.db.ok=true, got: {data.get('db')}"
    )


def test_health_reports_games(http):
    resp = http.get("/health")
    data = resp.json()
    count = data.get("db", {}).get("games", 0)
    assert count > 0, f"Expected db.games > 0, got {count}"
