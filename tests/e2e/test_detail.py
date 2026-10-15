"""
Tests 9-13 — detail page (/games/{slug}).

Covers 5 games from the seed:
  the-witcher-3-wild-hunt-remastered  — both summaries, cover, developer, video
  enshrouded                          — critic-only summary
  niph                                — unreleased (no metascore)
  warhammer-40000-boltgun-2           — has a video link
  bio-inc-classic                     — no cover (proxy returns SVG placeholder)

Each failure names the game and URL involved.
"""
from __future__ import annotations

import re

import httpx
import pytest

pytestmark = pytest.mark.e2e

# ── Game catalogue used in the tests ─────────────────────────────────────────
# Keyed by slug.  These are facts from the seeded DB — if the page disagrees,
# that is a rendering bug.

_GAMES = {
    "the-witcher-3-wild-hunt-remastered": {
        "title":       "The Witcher 3: Wild Hunt - Remastered",
        "developer":   "CD Projekt Red Studio",
        "has_desc":    True,
        "has_date":    True,
        "platforms":   {"Nintendo Switch 2", "PlayStation 5", "PC", "Xbox Series X"},
        "summaries":   {"critic", "user"},
        "has_video":   True,
        "has_cover":   True,
    },
    "enshrouded": {
        "title":       "Enshrouded",
        "developer":   "Keen Games",
        "has_desc":    True,
        "has_date":    True,
        "platforms":   {"PC", "Xbox Series X", "PlayStation 5"},
        "summaries":   {"critic"},
        "has_video":   False,
        "has_cover":   True,
    },
    "niph": {
        "title":       "Niph",
        "developer":   "Soda Studios",
        "has_desc":    True,
        "has_date":    True,       # release date present in DB
        "platforms":   set(),      # unreleased — may have no platform scores yet
        "summaries":   set(),      # no reviews → no summaries
        "has_video":   False,
        "has_cover":   True,
    },
    "warhammer-40000-boltgun-2": {
        "title":       "Warhammer 40,000: Boltgun 2",
        "developer":   "Auroch Digital",
        "has_desc":    True,
        "has_date":    True,
        "platforms":   set(),      # may vary
        "summaries":   set(),      # may or may not be present
        "has_video":   True,
        "has_cover":   True,
    },
    "bio-inc-classic": {
        "title":       "Bio Inc. Classic",
        "developer":   "DryGin Studios",
        "has_desc":    True,
        "has_date":    True,
        "platforms":   set(),
        "summaries":   set(),
        "has_video":   False,
        "has_cover":   False,      # no cover_url in DB → proxy serves SVG placeholder
    },
}


def _url(base_url: str, slug: str) -> str:
    return f"{base_url}/games/{slug}"


# ---------------------------------------------------------------------------
# Helper: navigate and wait for the detail page content
# ---------------------------------------------------------------------------

def _open_detail(page, base_url: str, slug: str):
    url = _url(base_url, slug)
    resp = page.goto(url, wait_until="domcontentloaded")
    assert resp.status == 200, (
        f"GET {url} returned HTTP {resp.status}, expected 200\n  game: {slug}"
    )
    page.wait_for_selector(".game-detail", timeout=10_000)
    return url


# ---------------------------------------------------------------------------
# 9. Title, cover, developer, description, release date
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", list(_GAMES.keys()))
def test_detail_core_fields(page, base_url, slug):
    """Test 9: Core fields match DB data (title, cover, developer, description, date)."""
    g = _GAMES[slug]
    url = _open_detail(page, base_url, slug)

    # Title
    h1 = page.locator("h1").first
    assert h1.count(), f"No <h1> on detail page  game={slug}  url={url}"
    actual_title = h1.inner_text().strip()
    assert g["title"].lower() in actual_title.lower(), (
        f"Title mismatch  game={slug}\n"
        f"  expected: {g['title']!r}\n"
        f"  got:      {actual_title!r}\n"
        f"  url: {url}"
    )

    # Cover image (proxy always serves something — should always load)
    cover_img = page.locator(".detail-cover")
    assert cover_img.count(), (
        f"No .detail-cover element  game={slug}  url={url}"
    )
    # The element should be an <img> with /img/{id} src (not a broken img and not a div placeholder)
    tag = page.evaluate(
        "el => el.tagName.toLowerCase()",
        cover_img.element_handle(),
    )
    assert tag == "img", (
        f"Detail cover for {slug!r} is a <{tag}>, expected <img>.\n"
        f"  The proxy must serve SVG placeholder instead of a <div>.\n"
        f"  url: {url}"
    )
    cover_src = cover_img.get_attribute("src") or ""
    assert cover_src.startswith("/img/"), (
        f"Cover src must start with /img/  game={slug}\n"
        f"  got: {cover_src!r}  url: {url}"
    )

    # Developer (required when DB has it)
    if g["developer"]:
        meta_text = page.locator(".detail-meta").inner_text()
        assert g["developer"].lower() in meta_text.lower(), (
            f"Developer not shown on page  game={slug}\n"
            f"  expected: {g['developer']!r}\n"
            f"  .detail-meta text: {meta_text!r}\n"
            f"  url: {url}"
        )

    # Description
    if g["has_desc"]:
        assert page.locator(".detail-desc").count(), (
            f"Description missing  game={slug}  url={url}"
        )
        desc = page.locator(".detail-desc").inner_text().strip()
        assert len(desc) > 10, (
            f"Description is suspiciously short ({len(desc)} chars)  game={slug}  url={url}"
        )

    # Release date
    if g["has_date"]:
        meta_text = page.locator(".detail-meta").inner_text()
        assert re.search(r"\d{4}", meta_text), (
            f"Release date (year) missing from .detail-meta  game={slug}\n"
            f"  .detail-meta text: {meta_text!r}\n"
            f"  url: {url}"
        )


# ---------------------------------------------------------------------------
# 10. Platform score table lists all platforms the DB holds for this game
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [
    "the-witcher-3-wild-hunt-remastered",
    "enshrouded",
])
def test_detail_platform_table(page, base_url, slug):
    """Test 10: Every platform in the DB appears in the scores table."""
    g = _GAMES[slug]
    expected_platforms = g["platforms"]
    if not expected_platforms:
        pytest.skip(f"No platforms defined for {slug}")

    url = _open_detail(page, base_url, slug)

    table = page.locator(".scores-table")
    assert table.count(), (
        f"Platform scores table not rendered  game={slug}  url={url}"
    )

    platform_cells = table.locator("tbody td:first-child").all_inner_texts()
    platform_cells_clean = {p.strip() for p in platform_cells}

    missing = expected_platforms - platform_cells_clean
    assert not missing, (
        f"Platforms missing from score table  game={slug}\n"
        f"  missing: {missing}\n"
        f"  shown:   {platform_cells_clean}\n"
        f"  url: {url}"
    )


# ---------------------------------------------------------------------------
# 11. Video link present when DB has one, and href resolves (not 404)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [
    "the-witcher-3-wild-hunt-remastered",
    "warhammer-40000-boltgun-2",
])
def test_detail_video_link(page, base_url, slug):
    """Test 11: Video link is rendered and the target URL does not 404."""
    g = _GAMES[slug]
    if not g["has_video"]:
        pytest.skip(f"No video expected for {slug}")

    url = _open_detail(page, base_url, slug)

    video_link = page.locator("a.video-link, .detail-video a, a[href*='jwplayer'], a[href*='youtube']")
    assert video_link.count(), (
        f"Video link not rendered but DB has video_url  game={slug}\n"
        f"  url: {url}\n"
        f"  Hint: add a video link to game.html"
    )

    href = video_link.first.get_attribute("href") or ""
    assert href.startswith("http"), (
        f"Video link href is not an absolute URL  game={slug}\n"
        f"  href: {href!r}  url: {url}"
    )

    with httpx.Client(timeout=10.0, follow_redirects=True) as client:
        r = client.head(href)
    assert r.status_code != 404, (
        f"Video link 404s  game={slug}\n"
        f"  href: {href}\n"
        f"  HTTP {r.status_code}"
    )


# ---------------------------------------------------------------------------
# 12. AI summaries section — content or explicit absent message
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug,expected_kinds", [
    ("the-witcher-3-wild-hunt-remastered", {"critic", "user"}),
    ("enshrouded", {"critic"}),
])
def test_detail_ai_summaries(page, base_url, slug, expected_kinds):
    """Test 12: AI summary cards present with valid liked/disliked text."""
    url = _open_detail(page, base_url, slug)

    summaries_section = page.locator(".detail-section:has(h2:text('AI Summaries'))")
    assert summaries_section.count(), (
        f"AI Summaries section not rendered  game={slug}  url={url}"
    )

    cards = summaries_section.locator(".summary-card").all()
    found_kinds = set()
    for card in cards:
        heading = card.locator("h3").inner_text().lower()
        if "critic" in heading:
            found_kinds.add("critic")
        elif "user" in heading:
            found_kinds.add("user")

        # Every summary card must have either real liked text (>20 chars)
        # or the explicit "no positives mentioned" line — a blank heading is a bug.
        likes_el = card.locator(".summary-likes")
        dislikes_el = card.locator(".summary-dislikes")
        assert likes_el.count(), (
            f"Summary card has no .summary-likes element  game={slug}  url={url}"
        )
        assert dislikes_el.count(), (
            f"Summary card has no .summary-dislikes element  game={slug}  url={url}"
        )

        likes_text = likes_el.inner_text().strip()
        dislikes_text = dislikes_el.inner_text().strip()

        valid_likes = (
            len(likes_text) > 20
            or "no positives mentioned" in likes_text.lower()
        )
        assert valid_likes, (
            f"Liked text is blank or too short  game={slug}  kind={heading!r}\n"
            f"  likes text: {likes_text!r}\n  url: {url}"
        )

        valid_dislikes = (
            len(dislikes_text) > 20
            or "no criticisms mentioned" in dislikes_text.lower()
        )
        assert valid_dislikes, (
            f"Criticised text is blank or too short  game={slug}  kind={heading!r}\n"
            f"  dislikes text: {dislikes_text!r}\n  url: {url}"
        )

    missing = expected_kinds - found_kinds
    assert not missing, (
        f"Expected summary kinds {expected_kinds!r} but only found {found_kinds!r}  "
        f"game={slug}  url={url}"
    )


# ---------------------------------------------------------------------------
# 13. Similar games: at least one result, each links to a page returning 200,
#     and the similarity method badge is visible
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("slug", [
    "the-witcher-3-wild-hunt-remastered",
    "enshrouded",
])
def test_detail_similar_games(page, base_url, slug):
    """Test 13: Similar games section loads with results, links resolve, badge visible."""
    url = _open_detail(page, base_url, slug)

    # Similar section loads via HTMX — wait for it to replace the placeholder.
    similar_section = page.locator(".similar-section")
    assert similar_section.count(), (
        f"No .similar-section on page  game={slug}  url={url}"
    )
    # Wait until HTMX replaces the Loading… text (max 15 s)
    page.wait_for_function(
        "() => !document.querySelector('.similar-section')?.innerText.includes('Loading…')",
        timeout=15_000,
    )

    similar_list = page.locator(".similar-list")
    if similar_list.count() == 0:
        placeholder = page.locator(".similar-section .placeholder-text").inner_text()
        pytest.skip(f"Not enough games for similar results: {placeholder}")

    items = similar_list.locator(".similar-item").all()
    assert items, (
        f"Similar games list is empty  game={slug}  url={url}"
    )

    failures = []
    for item in items[:3]:   # check up to 3 to keep test fast
        link = item.locator(".similar-link")
        href = link.get_attribute("href") or ""
        if not href:
            failures.append(f"  similar item has no href  game={slug}")
            continue

        full_href = href if href.startswith("http") else base_url + href
        try:
            # HEAD returns 405 for HTML routes in this Starlette version — use GET.
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                r = client.get(full_href)
            if r.status_code != 200:
                title_el = item.locator(".similar-title")
                title = title_el.inner_text() if title_el.count() else "(unknown)"
                failures.append(
                    f"  similar game {title!r} links to {full_href} → HTTP {r.status_code}"
                )
        except Exception as exc:
            failures.append(f"  {full_href} raised {exc}")

        # Method badge must be visible
        badge = item.locator(".similar-method")
        assert badge.count(), (
            f"Similarity method badge missing  game={slug}  url={url}"
        )
        badge_text = badge.inner_text().strip()
        assert badge_text, (
            f"Similarity method badge is empty  game={slug}  url={url}"
        )

    assert not failures, (
        f"Similar game link failures for {slug}:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# Extra: cover loads for a game with no cover_url (proxy returns SVG)
# ---------------------------------------------------------------------------

def test_detail_no_cover_shows_placeholder_img(page, base_url):
    """
    bio-inc-classic has no cover_url in the DB.
    The proxy must serve an SVG placeholder — the page should still have an
    <img class='detail-cover'> that loads (naturalWidth > 0), not a broken img.
    """
    slug = "bio-inc-classic"
    url = _open_detail(page, base_url, slug)

    cover_img = page.locator("img.detail-cover")
    assert cover_img.count(), (
        f"No <img class='detail-cover'> for {slug}  url={url}\n"
        f"  Expected proxy to serve SVG placeholder as <img>, not a <div>."
    )

    page.wait_for_load_state("networkidle")
    nat_w = page.evaluate(
        "el => el.naturalWidth",
        cover_img.element_handle(),
    )
    assert nat_w > 0, (
        f"Cover img for {slug!r} has naturalWidth=0 (SVG placeholder not loading)\n"
        f"  src: {cover_img.get_attribute('src')}\n"
        f"  url: {url}"
    )
