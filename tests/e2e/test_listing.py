"""
Tests 1-8 — listing page (/).

Each failure names the game and URL involved.
"""
from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.e2e

# Known seed game used for search test.
_SEARCH_TITLE = "The Witcher 3: Wild Hunt - Remastered"
_SEARCH_SLUG = "the-witcher-3-wild-hunt-remastered"
_SEARCH_TERM = "Witcher"
_NONEXISTENT_QUERY = "xyzqqqnonexistentgame999"


# ---------------------------------------------------------------------------
# 1. Page loads, game count on page matches /health db.games
# ---------------------------------------------------------------------------

def test_listing_page_loads_200(http, base_url):
    """Test 1a: HTTP 200 from the listing route."""
    resp = http.get("/")
    assert resp.status_code == 200, (
        f"GET {base_url}/ returned {resp.status_code}, expected 200"
    )


def test_listing_count_matches_health(page, base_url, http):
    """
    Test 1b: The 'N games' counter on the page must match /health's db.games.

    On the live deployment /health says 198 and the page says 172 — this test
    documents and catches that discrepancy.  Both numbers come from the same
    unfiltered Game table count; they must agree.
    """
    health_resp = http.get("/health")
    assert health_resp.status_code == 200
    db_games = health_resp.json()["db"]["games"]

    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    count_text = page.locator(".results-count").inner_text()
    # "591 games" or "1 game"
    match = re.search(r"(\d+)\s+game", count_text)
    assert match, f"Could not parse game count from {count_text!r}"
    page_count = int(match.group(1))

    assert page_count == db_games, (
        f"Page shows {page_count} games but /health reports {db_games} games.\n"
        f"  URL: {base_url}/\n"
        f"  Results count text: {count_text!r}\n"
        f"These must agree — both are unfiltered counts of the Game table."
    )


# ---------------------------------------------------------------------------
# 2. Every cover <img> loads (naturalWidth > 0)
# ---------------------------------------------------------------------------

def test_listing_images_load(page, base_url):
    """Test 2: Every cover <img> on page 1 must load successfully (naturalWidth > 0)."""
    page.goto(base_url + "/", wait_until="networkidle")

    # Force all lazy images to load (some may be below the fold despite tall viewport).
    page.evaluate("""() => {
        document.querySelectorAll('img').forEach(img => {
            img.loading = 'eager';
        });
    }""")
    page.wait_for_load_state("networkidle")

    broken = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('.game-grid img')).filter(img =>
            !img.complete || img.naturalWidth === 0
        ).map(img => ({ src: img.getAttribute('src'), alt: img.alt }));
    }""")

    assert not broken, (
        f"Images failed to load (naturalWidth=0 or not complete):\n"
        + "\n".join(f"  game={r['alt']!r}  src={r['src']}" for r in broken)
    )


# ---------------------------------------------------------------------------
# 3. Every distinct image src → HTTP 200 + image/* content-type
# ---------------------------------------------------------------------------

def test_listing_image_srcs_return_200(page, base_url, http):
    """Test 3: Each distinct cover img src must return 200 with image/* content-type."""
    page.goto(base_url + "/", wait_until="domcontentloaded")

    srcs = page.evaluate("""() => {
        return [...new Set(
            Array.from(document.querySelectorAll('.game-grid img'))
                .map(img => img.getAttribute('src'))
                .filter(Boolean)
        )];
    }""")

    assert srcs, "No cover images found on the listing page"

    failures = []
    for src in srcs:
        url = src if src.startswith("http") else base_url + src
        try:
            r = http.get(src)   # httpx client has base_url set
        except Exception as exc:
            failures.append(f"  {url}  ERROR: {exc}")
            continue
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or not ct.startswith("image/"):
            failures.append(
                f"  {url}  HTTP {r.status_code}  content-type: {ct!r}"
            )

    assert not failures, (
        f"{len(failures)} image src(s) did not return 200 image/*:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# 4. No <img> has empty, null or external src
# ---------------------------------------------------------------------------

def test_listing_no_external_or_empty_img_src(page, base_url):
    """
    Test 4: Every <img> src must be an internal /img/ path.
    No empty src, no direct metacritic.com or other external URL.
    """
    page.goto(base_url + "/", wait_until="domcontentloaded")

    violations = page.evaluate("""(baseUrl) => {
        return Array.from(document.querySelectorAll('.game-grid img')).filter(img => {
            const src = img.getAttribute('src') || '';
            if (!src) return true;
            if (src.startsWith('http') && !src.startsWith(baseUrl)) return true;
            return false;
        }).map(img => ({ src: img.getAttribute('src'), alt: img.alt }));
    }""", base_url)

    assert not violations, (
        "Game card images must use internal /img/{id} URLs only.\n"
        + "\n".join(f"  game={r['alt']!r}  src={r['src']!r}" for r in violations)
    )


# ---------------------------------------------------------------------------
# 5. Search
# ---------------------------------------------------------------------------

def test_listing_search_known_title_appears(page, base_url):
    """Test 5a: Searching for a known seeded title returns it."""
    page.goto(base_url + f"/?q={_SEARCH_TERM}", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    # Confirm the target game is in the results
    titles = page.locator(".game-title").all_inner_texts()
    assert any(_SEARCH_TERM.lower() in t.lower() for t in titles), (
        f"Search for {_SEARCH_TERM!r} did not return {_SEARCH_TITLE!r}.\n"
        f"URL: {base_url}/?q={_SEARCH_TERM}\n"
        f"Found titles: {titles[:10]}"
    )


def test_listing_search_nonexistent_returns_nothing(page, base_url):
    """Test 5b: Searching for a non-existent title returns zero results."""
    page.goto(base_url + f"/?q={_NONEXISTENT_QUERY}", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    count_text = page.locator(".results-count").inner_text()
    assert "0 games" in count_text or "no results" in count_text.lower(), (
        f"Expected zero results for query {_NONEXISTENT_QUERY!r}, got: {count_text!r}\n"
        f"URL: {base_url}/?q={_NONEXISTENT_QUERY}"
    )


# ---------------------------------------------------------------------------
# 6. Platform filter: every visible card must list the selected platform
# ---------------------------------------------------------------------------

def test_listing_platform_filter_ps5(page, base_url):
    """Test 6: Filtering by PlayStation 5 shows only PS5 games."""
    page.goto(
        base_url + "/?platform=PlayStation+5",
        wait_until="domcontentloaded",
    )
    page.wait_for_selector(".results-count")

    cards = page.locator(".game-card").all()
    assert cards, "No game cards visible after PS5 filter"

    violations = []
    for card in cards:
        title_el = card.locator(".game-title")
        title = title_el.inner_text() if title_el.count() else "(unknown)"
        # CSS may apply text-transform:uppercase — compare case-insensitively.
        chips = [c.strip() for c in card.locator(".platform-chip").all_inner_texts()]
        chips_lower = [c.lower() for c in chips]
        if "playstation 5" not in chips_lower:
            violations.append(f"  {title!r} — platforms shown: {chips}")

    assert not violations, (
        f"These games appeared in PS5 filter but don't show PlayStation 5 chip:\n"
        + "\n".join(violations)
        + f"\n  URL: {base_url}/?platform=PlayStation+5"
    )


# ---------------------------------------------------------------------------
# 7. Sort order
# ---------------------------------------------------------------------------

def test_listing_sort_best_score(page, base_url):
    """Test 7a: Sort=score gives non-increasing metascores down the page."""
    page.goto(base_url + "/?sort=score", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    score_texts = page.locator(".game-card .score").all_inner_texts()
    scores = []
    for t in score_texts:
        t = t.strip()
        if t.lstrip("-").isdigit():
            scores.append(int(t))
        else:
            scores.append(None)   # "—" for games without a score

    # Scored games must come in non-increasing order before unscored games.
    scored = [s for s in scores if s is not None]
    for i in range(len(scored) - 1):
        assert scored[i] >= scored[i + 1], (
            f"Sort=score is not non-increasing at position {i}: "
            f"{scored[i]} > {scored[i+1]}\n"
            f"Full score sequence: {scores}\n"
            f"URL: {base_url}/?sort=score"
        )


def test_listing_sort_title_az(page, base_url):
    """Test 7b: Sort=title gives alphabetical order (case-insensitive)."""
    page.goto(base_url + "/?sort=title", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    titles = page.locator(".game-card .game-title").all_inner_texts()
    titles = [t.strip() for t in titles if t.strip()]
    titles_lower = [t.lower() for t in titles]

    for i in range(len(titles_lower) - 1):
        assert titles_lower[i] <= titles_lower[i + 1], (
            f"Sort=title not alphabetical at position {i}: "
            f"{titles[i]!r} > {titles[i+1]!r}\n"
            f"URL: {base_url}/?sort=title"
        )


# ---------------------------------------------------------------------------
# 8. Pagination
# ---------------------------------------------------------------------------

def test_listing_page2_loads_different_games(page, base_url):
    """Test 8: Page 2 loads and shows different games than page 1."""
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    # Check pagination exists
    page_info = page.locator(".page-info")
    if page_info.count() == 0:
        # Only 1 page of results — nothing to test
        total_text = page.locator(".results-count").inner_text()
        pytest.skip(f"Only one page of results: {total_text}")

    titles_p1 = set(page.locator(".game-title").all_inner_texts())

    page.goto(base_url + "/?page=2", wait_until="domcontentloaded")
    page.wait_for_selector(".results-count")

    titles_p2 = set(page.locator(".game-title").all_inner_texts())

    overlap = titles_p1 & titles_p2
    assert not overlap, (
        f"Page 1 and Page 2 share {len(overlap)} game(s) — pagination is broken:\n"
        + "\n".join(f"  {t}" for t in sorted(overlap))
    )
    assert titles_p2, f"Page 2 is empty (URL: {base_url}/?page=2)"
