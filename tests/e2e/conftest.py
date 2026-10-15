"""
Shared fixtures for the Playwright e2e suite.

Run against localhost (default):
    pytest tests/e2e/ -v

Run against the live deployment:
    BASE_URL=https://metacrawl.onrender.com pytest tests/e2e/ -v
    OR
    make e2e BASE_URL=https://metacrawl.onrender.com
"""
from __future__ import annotations

import os

import httpx
import pytest


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


@pytest.fixture(scope="session")
def http(base_url: str):
    """httpx.Client for direct HTTP assertions (not going through the browser)."""
    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        yield client


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Tall viewport so all 24 game cards on page 1 are in-view (no lazy-load skip)."""
    return {**browser_context_args, "viewport": {"width": 1280, "height": 3000}}
