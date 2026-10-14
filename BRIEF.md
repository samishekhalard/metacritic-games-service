# Metacritic games service — technical brief

Everything in this document was verified against live responses on 2026-10-14.
Field names, endpoints and shapes are observed, not guessed. Where something
was not checked, it says so.

---

## 1. Data sources

Metacritic's web frontend is a server-rendered Nuxt 3 app that talks to a
public JSON backend. **Use the backend directly.** It needs no key, returns
clean objects, and paginates itself. HTML parsing is the fallback, not the plan.

`robots.txt` for `User-agent: *` disallows only `/search`, `/signup`, `/login`,
`/user`, `/jl/`, `/8264/`, `/7336/`. Nothing we touch is disallowed. Several
AI crawlers (`GPTBot`, `CCBot`, `OAI-SearchBot`) are banned **by name**, which
is worth a paragraph in the README: we identify honestly, rate-limit, and cache.

### 1.1 Listings — the finder API

```
https://backend.metacritic.com/finder/metacritic/web
    ?componentType=ProductList
    &mcoTypeId=13              # 13 = games
    &sortBy=-releaseDate       # newest first
    &offset=0&limit=24
    [&metaScoreMin=1]          # only titles that have a Metascore
```

Two queries matter, matching the two sections the assignment names:

| Assignment wording | Query |
|---|---|
| "первые 20 игр … раздел New Releases" | add `componentName=new-releases-carousel`, `metaScoreMin=1`, `limit=20` |
| "SEE ALL (сортировка Новые)" | omit `componentName` and `metaScoreMin`, page with `offset` |

Observed: 18,620 results for the scored carousel, 181,114 for the full list.

Response shape:

```jsonc
{"data": {"totalResults": 18620,
          "items": [ /* game objects, see 1.2 */ ]},
 "links": {"next": {"href": "...offset=20..."},      // follow this to paginate
           "last": {"href": "...offset=18600..."}}}
```

`links.next.href` is a complete URL — follow it rather than computing offsets.

### 1.2 Listing item fields

```
id            int, stable — use as the external key
slug          str — the /game/{slug}/ path segment
title         str
description   str | null        (often null for unreleased titles)
releaseDate   "YYYY-MM-DD" | null
premiereYear  int | null
rating        "M" | "E10+" | ... | null
image         {bucketPath, width, height, imageUrl}   image may be null
genres        [{name}]
platforms     [{id, name}]      listing gives names only, no per-platform score
criticScoreSummary {score, reviewCount, positiveCount, neutralCount,
                    negativeCount, sentiment, url}    score null until reviewed
userScore     {score}           0-10 float, often null
```

Cover URL: `imageUrl` when present, otherwise compose
`https://www.metacritic.com/a/img/resize/{width}/{height}/catalog{bucketPath}`.

**Unreleased titles are the normal case** in a "new releases" feed: null score,
null description, null image, zero reviews. Every field must be optional.

### 1.3 Game detail — the page payload

No detail endpoint was found on the backend; the game page carries everything.
Fetch `https://www.metacritic.com/game/{slug}/` and read the
`<script id="__NUXT_DATA__">` blob (decoder in `src/metacrawl/payload.py`,
already written and tested — see section 5).

`data["loadPage:games:{slug}:"]["components"]` is a list of 9, identified by
`meta.componentName`:

| componentName | Contains |
|---|---|
| `product` | `item`: title, description, releaseDate, rating, genres, platforms, images, **production.companies**, **video** |
| `critic-score-summary` | `item`: score, reviewCount, positive/neutral/negativeCount, sentiment |
| `user-score-summary` | same shape, score is 0-10 |
| `critic-reviews` | `item.default`: first 7 reviews, `totalResults` |
| `user-reviews` | `item.default`: first 7 reviews, `totalResults` |
| `related-carousel` | `items`: 24 related games — Metacritic's own similarity |
| `where-to-buy`, `related-news`, `game-stats` | not needed |

Index order was stable across fetches but **match on `meta.componentName`,
never on index.**

Developer: `product.item.production.companies[]`, each with
`typeName: "Developer" | "Publisher"` and `name`.

Video: `product.item.video` → `{videoId, slug, title, videoTitle, ...}`.
The full object was not dumped — inspect it and find the playable URL field.

### 1.4 Reviews — the reviews API

```
https://backend.metacritic.com/reviews/metacritic/critic/games/{slug}/web
    ?offset=0&limit=10&filterBySentiment=all&sort=score
https://backend.metacritic.com/reviews/metacritic/user/games/{slug}/web
    ?offset=0&limit=50&filterBySentiment=all&sort=date
```

Critic review: `{quote, score, author, publicationName, publicationSlug, date,
platform, url}`. User review: `{quote, score, author, date, platform, spoiler,
thumbsUp, thumbsDown, id}`.

`quote` is the full review text for users and an extract for critics. That is
what the summaries run on.

---

## 2. What to build

### 2.1 Required

- **Hourly job.** Each run: 20 games the service has not processed *today*.
  First run of a day takes the New Releases carousel; later runs page through
  the full browse list, one page per run. The day counter resets at midnight.
  Re-processing an existing game updates it rather than duplicating it.
- **Per game store:** title, cover, platforms with Metascore and Userscore,
  developer, description, video link.
- **Two LLM summaries per game** — what players like and dislike — one from
  critic reviews, one from user reviews, kept separate, refreshed on re-crawl.
- **Web UI:** list view, detail card, platform filter, title search, sort by
  rating.
- **Similar games from our own database**, shown on the card, clickable.
  Metacritic's `related-carousel` is a cross-check, not the answer: the task
  says "из тех что уже есть в базе сервиса".

### 2.2 Optional part 1

Find Let's Plays on YouTube, take the most popular, transcribe it, summarise
the transcript, attach the summary and the video link to the game.

### 2.3 Optional part 2

Live worker status and processed counts in the UI, plus a force-run button.

---

## 3. Architecture

```
FastAPI + Jinja2 + HTMX          server-rendered, no build step
SQLite via SQLAlchemy            one file; swap to Postgres by URL
APScheduler                      hourly job and the manual trigger
httpx                            rate-limited client, one connection pool
Ollama (default) or any          LLM behind one interface; nomic-embed-text
  OpenAI-compatible API            for similarity, qwen2.5:7b for summaries
Server-Sent Events               live monitoring without websockets
Docker + render.yaml             deploy target: Render free tier
```

### Schema sketch

```
games            id, external_id UNIQUE, slug UNIQUE, title, description,
                 developer, publisher, cover_url, video_url, release_date,
                 rating, metascore, userscore, first_seen_at, last_crawled_at
platform_scores  game_id FK, platform, metascore, userscore, review_counts
genres           many-to-many with games
reviews          game_id FK, kind('critic'|'user'), author, publication,
                 score, quote, review_date
summaries        game_id FK, kind('critic'|'user'), likes, dislikes,
                 model, source_review_count, generated_at
embeddings       game_id FK, vector BLOB, model        -- for similarity
crawl_runs       started_at, finished_at, trigger('schedule'|'manual'),
                 source('new-releases'|'browse'), page, games_seen,
                 games_created, games_updated, errors, status
```

`crawl_runs` is what Optional 2's monitor reads. Write it from the start —
retrofitting observability is always worse.

### Rules that keep it honest

- **One HTTP client with a rate limit**, 1 request/second, a real
  `User-Agent` naming the project, and `If-Modified-Since` where possible.
  No parallel hammering: this is someone else's server.
- **Cache every raw response** to disk during development so iterating on
  parsing costs no requests.
- **Every external field is optional.** Unreleased games have nulls everywhere.
- **Summaries are skipped when a game has no reviews** — do not invent a
  summary from a description. That is the one failure that would be obvious
  to a reviewer who knows the data.
- **Record which model produced each summary** and how many reviews it read.

---

## 4. Suggested order

1. Config, HTTP client with rate limiting and disk cache.
2. Finder API client + models + upsert. Prove it: run once, 20 games in SQLite.
3. Detail fetch: developer, video, platform scores, reviews.
4. Scheduler with the day-reset logic, writing `crawl_runs`.
5. LLM summaries, critic and user kept apart.
6. Web UI: list, card, filter, search, sort.
7. Similarity over embeddings of description + genres.
8. Dockerfile + render.yaml, deploy, confirm the URL works.
9. Optional 2 (SSE monitor + force run) — cheap, and visibly impressive.
10. Optional 1 (YouTube) — heaviest, do last.

---

## 5. Already written

`src/metacrawl/payload.py` and `src/metacrawl/scrape/listing.py` exist and are
tested against saved fixtures:

- `payload.py` extracts and decodes the Nuxt devalue blob — a flat array where
  integers are pointers into the same array and negatives encode `undefined`,
  holes, `NaN` and the infinities. Handles cycles by depth limit.
- `listing.py` turns payload items into `GameSummary` objects. Parses the saved
  browse fixture to 24 complete games.

The hub page (`/game/`) is ten carousels; only `new-releases-carousel` is the
New Releases block. With the finder API in 1.1 you may not need the hub page
at all.

Fixtures saved under `tests/fixtures/`: `browse.html`, `new-releases.html`,
`game.html`, `critic-reviews.html`, `user-reviews.html`, `api-new-releases.json`,
`api-page2.json`. Build parsers against these so tests need no network.

---

## 6. Deliverables

- Repository link
- Live service URL
- The full AI conversation as raw JSONL — Claude Code writes these to
  `~/.claude/projects/<project>/*.jsonl`; copy them into the repo under
  `ai-logs/` before sending
- README covering: architecture, the robots.txt position, what was measured,
  and what does not work yet

Send to `cherkasov@skytecgames.com`.
