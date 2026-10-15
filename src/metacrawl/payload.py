"""
Reading Metacritic's page payload.

Metacritic is a server-rendered Nuxt 3 application. Every page embeds the data
its components rendered from in a `<script id="__NUXT_DATA__">` blob, so the
scraper reads structured objects — ids, scores, platforms, release dates —
instead of matching CSS classes.

That choice is the difference between a scraper that survives a redeploy and
one that silently returns empty lists the next morning. Class names like
`c-finderProductCard_title` change without notice; the payload is the data the
page itself runs on.

The blob is devalue-encoded, the format Nuxt uses for hydration: a flat array
where every value is either a literal or an integer index pointing at another
slot in the same array, which is how it deduplicates repeated strings and
handles cycles. Negative indices encode the JavaScript values JSON cannot —
undefined, holes, NaN, the infinities and negative zero.

`parse` turns that back into ordinary Python.
"""

from __future__ import annotations

import json
import re
from html import unescape as _html_unescape
from typing import Any

PAYLOAD = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(?P<body>.*?)</script>', re.S
)

# devalue's negative sentinels.
SENTINELS: dict[int, Any] = {
    -1: None,        # undefined
    -2: None,        # array hole
    -3: float("nan"),
    -4: float("inf"),
    -5: float("-inf"),
    -6: -0.0,
}

# Wrappers Nuxt adds around reactive state. The value we want is inside.
UNWRAP = {"Reactive", "ShallowReactive", "Ref", "ShallowRef", "EmptyRef", "Object"}

MAX_DEPTH = 80


class PayloadError(RuntimeError):
    """The page carried no payload, or one we could not read."""


def extract(html: str) -> list:
    """Pull the raw devalue array out of a page."""
    match = PAYLOAD.search(html)
    if match is None:
        raise PayloadError(
            "no __NUXT_DATA__ block in this page — Metacritic may have changed "
            "its rendering, or this is an error or consent page rather than content"
        )
    try:
        data = json.loads(match.group("body"))
    except json.JSONDecodeError as exc:
        raise PayloadError(f"__NUXT_DATA__ is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise PayloadError(f"expected a devalue array, got {type(data).__name__}")
    return data


def resolve(data: list, index: Any, depth: int = 0) -> Any:
    """Resolve one slot of a devalue array into a plain Python value."""
    if depth > MAX_DEPTH:
        # Nuxt payloads contain cycles (a page references its own route).
        # Depth-limiting is enough: nothing we read lives this deep.
        return None

    if isinstance(index, bool):
        return index
    if isinstance(index, int):
        if index in SENTINELS:
            return SENTINELS[index]
        if not 0 <= index < len(data):
            return None
        value = data[index]
    else:
        value = index

    if isinstance(value, list):
        if value and isinstance(value[0], str):
            tag = value[0]
            if tag in UNWRAP:
                return resolve(data, value[1], depth + 1)
            if tag == "Date":
                return resolve(data, value[1], depth + 1)
            if tag == "Set":
                return [resolve(data, item, depth + 1) for item in value[1:]]
            if tag == "Map":
                flat = [resolve(data, item, depth + 1) for item in value[1:]]
                return dict(zip(flat[::2], flat[1::2]))
            if tag == "BigInt":
                raw = resolve(data, value[1], depth + 1)
                return int(raw) if raw is not None else None
        return [resolve(data, item, depth + 1) for item in value]

    if isinstance(value, dict):
        return {key: resolve(data, slot, depth + 1) for key, slot in value.items()}

    return value


def parse(html: str) -> dict:
    """Page HTML to the resolved payload root."""
    root = resolve(extract(html), 0)
    if not isinstance(root, dict):
        raise PayloadError(f"payload root is {type(root).__name__}, expected an object")
    return root


_CDN_IMG = re.compile(
    r"(https://www\.metacritic\.com/a/img/resize/[a-f0-9]{40}/catalog(/[^\"'?<>\s,]+))"
)


def scrape_image_urls(html: str) -> dict:
    """
    Build a bucketPath → CDN URL map by scanning the whole page for Metacritic
    CDN URLs that carry the required content hash.

    imageUrl in the Nuxt payload is always null; the hash only appears in
    server-rendered markup and embedded JSON metadata.  The regex scans
    anywhere in the page (img src, og:image JSON, etc.).

    Returns e.g. {"/provider/6/12/6-1-824956-52.jpg": "https://…?auto=webp"}
    First occurrence per bucketPath wins.
    """
    result: dict = {}
    for m in _CDN_IMG.finditer(html):
        bucket = m.group(2)          # /provider/…/filename.jpg  (no query)
        if bucket not in result:
            result[bucket] = _html_unescape(m.group(1))
    return result


def find_items(root: dict, marker: str = "items") -> list[dict]:
    """
    Locate the list of content items in a resolved payload.

    The key that holds them encodes the request path — for example
    `browse-game-/browse/game/all/all/all-time/new/` — so it cannot be
    hard-coded across pages. Searching `data` for the first list of objects
    carrying both an id and a title survives Metacritic renaming its routes,
    which hard-coding would not.
    """
    data = root.get("data")
    if not isinstance(data, dict):
        return []

    def looks_like_content(value: Any) -> bool:
        return (
            isinstance(value, list)
            and value
            and isinstance(value[0], dict)
            and "title" in value[0]
            and "id" in value[0]
        )

    for section in data.values():
        if isinstance(section, dict):
            candidate = section.get(marker)
            if looks_like_content(candidate):
                return candidate
    # Fall back to a shallow sweep for payload shapes that nest one deeper.
    for section in data.values():
        if isinstance(section, dict):
            for value in section.values():
                if looks_like_content(value):
                    return value
    return []
