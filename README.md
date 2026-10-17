# Metacrawl

A Metacritic games service that crawls game listings, fetches critic and user
reviews, generates AI summaries, and serves a browsable web UI with search,
platform filtering, and text-similarity recommendations.

---

## Live demo

**https://metacrawl.onrender.com**

The service shows a paginated grid of games with Metascores, cover art, per-platform
score tables, AI-written critic and user summaries, and "similar games" recommendations
based on text embeddings or genre/platform overlap. If the free Render instance has
spun down, the first load may take around 50 seconds.

---

## Quick start

### Docker

```bash
docker build -t metacrawl .
docker run -p 8000:8000 \
  -e MC_LLM_BASE_URL=https://api.groq.com/openai/v1 \
  -e MC_LLM_API_KEY=gsk_... \
  -e MC_LLM_MODEL=llama-3.3-70b-versatile \
  -e MC_CRAWL_SECRET=change-me \
  metacrawl
```

The app seeds itself from `seeds/seed.json` on first boot and is ready at
`http://localhost:8000`.

### Local development

```bash
pip install -r requirements.txt

# Start Ollama for local LLM and embeddings
ollama serve &
ollama pull qwen2.5:7b
ollama pull nomic-embed-text

# Run the web server (seeds automatically on first start)
PYTHONPATH=src python -m metacrawl.web        # http://localhost:8000

# Run a crawl
PYTHONPATH=src python -m metacrawl.crawl --source scored --pages 5

# Backfill covers and AI summaries for newly crawled games
PYTHONPATH=src python -m metacrawl.backfill

# Tests
make test          # 188 unit tests
make e2e           # 28 Playwright tests (requires a running server on localhost:8000)
```

Required environment variables:

| Variable | Purpose | Default |
|----------|---------|---------|
| `MC_LLM_BASE_URL` | OpenAI-compatible endpoint | `http://localhost:11434/v1` |
| `MC_LLM_API_KEY` | API key for the LLM provider | `ollama` |
| `MC_LLM_MODEL` | Chat model name | `qwen2.5:7b` |
| `MC_CRAWL_SECRET` | Shared secret protecting `POST /api/crawl/run` | *(unset = endpoint refuses)* |
| `MC_EMBED_PROVIDER` | Enable embeddings (`ollama` or `openai-compatible`) | *(unset = disabled)* |

---

## Architecture

```
Crawler (background thread)
  │
  ├── Finder API        → game metadata, platform scores (upsert)
  ├── Detail API        → description, genres, video, developer/publisher
  ├── Reviews API       → critic reviews (score-sorted) + user reviews (thumbs_up-sorted)
  ├── LLM               → two summaries per game: critic kind + user kind
  └── Embedder          → text embedding of title + description + genres

Storage
  └── SQLite (games, platform_scores, game_genres, reviews, summaries,
              embeddings, crawl_runs)

Web (FastAPI + Jinja2 + HTMX)
  ├── GET /                    paginated game list, search, platform filter, sort
  ├── GET /games/:slug         detail: scores, summaries, reviews, similar games
  ├── GET /games/:slug/similar HTMX fragment — similar games via embedding or overlap
  ├── GET /img/:id             cover image proxy (LRU disk cache, SVG placeholder on failure)
  ├── GET /health              liveness — always 200, exposes db/llm/crawl stats
  ├── GET /health/ready        readiness — 503 when LLM unreachable
  └── POST /api/crawl/run      trigger crawl (HMAC token from UI, shared secret for cron)
```

---

## Data sources

Metacritic's frontend is a server-rendered Nuxt 3 application. Rather than
parsing HTML, this service calls the backend JSON API directly — the same
endpoints the Nuxt server uses to hydrate its pages. Because the scraper does
not depend on CSS class names or DOM structure, cosmetic redesigns do not break
it.

**Devalue encoding.** The page payload is serialised with the
[devalue](https://github.com/Rich-Harris/devalue) format: a flat array where
objects are represented by integer indices into the array, and special values
use negative-integer sentinels (`-1` for `undefined`, `-2` for `Infinity`,
etc.). The crawler decodes this inline rather than importing the npm package,
walking the index graph to reconstruct the original object tree.

**Cover images.** Cover art is not included in the JSON payload — it appears
only in the server-rendered HTML as a `<img srcset>` attribute. The CDN path
carries a 40-character content hash that encodes the exact resize dimensions;
the same hash with different query parameters is rejected by the CDN with a 403.
Even with a correctly formed URL, direct image requests from a different origin
are refused: the CDN requires `Referer: https://www.metacritic.com/` and a
browser-like `Accept: image/*` header. To avoid exposing broken images,
every cover is proxied through `GET /img/{game_id}`: the service fetches
upstream with the required headers, caches the result on disk (LRU, bounded
by file count and total size), and returns an SVG placeholder — the game's
initials on a colour derived from its title — on any failure or missing URL.
Templates never reference `metacritic.com` directly; a test asserts this.

---

## Robots.txt position

`metacritic.com/robots.txt` disallows `/search`, `/signup`, `/login`, `/user`,
`/jl/`, `/8264/`, and `/7336/`. None of those paths are touched by this
service. Several AI-crawler user-agent strings are banned by name in the same
file.

The stance taken here: honest `User-Agent` string in every request, 1 request
per second (configurable via `MC_RATE_LIMIT_RPS`), all responses disk-cached
with a 1-hour TTL, nothing behind a login wall. The service reads publicly
available scores and reviews, the same information visible to any browser.

---

## What was measured

The seed was built locally with Ollama and ships with the repo because the free
Render host has no persistent disk — every restart reseeds from
`seeds/seed.json`.

| Metric | Value |
|--------|-------|
| Games in shipped seed | 172 |
| Games with both critic and user summary | 100 |
| Games with one summary (critic or user) | 67 |
| Reviews stored | 2,712 |
| Embeddings in seed | 171 |
| Unit tests passing | 188 |
| Playwright E2E tests passing | 28 |

Summaries generated locally with **qwen2.5:7b** via Ollama.
Embeddings generated locally with **nomic-embed-text** via Ollama.
Production LLM: **llama-3.3-70b-versatile** on Groq's free tier.

---

## Design decisions

**Ephemeral database with a committed seed.** Render's free tier provides no
persistent disk. Rather than requiring a database add-on, the app ships a JSON
seed of 172 processed games that is loaded automatically when the `games` table
is empty. The seed is rebuilt locally and recommitted to update production data.
The tradeoff is that crawl results added after deploy do not survive a restart;
the committed seed is the ground truth.

**External scheduler instead of in-process cron.** A Render free instance spins
down after 15 minutes of inactivity, so an APScheduler job embedded in the app
cannot fire on schedule. `MC_SCHEDULER_ENABLED=false` disables the in-process
scheduler. A GitHub Actions workflow (`.github/workflows/crawl.yml`) sends a
signed `POST /api/crawl/run` each hour, which also wakes the instance. A
separate 10-minute ping job hits `/health` to prevent spin-down between crawls.

**Summaries are skipped, never invented.** A game with no reviews gets no
summary. A game with uniformly negative reviews stores `null` for the `likes`
field rather than a fabricated positive; the card shows "No positives mentioned
across N reviews" rather than a blank heading. Either field may be null
independently.

**Errors are counted, not swallowed.** Each crawl run records `llm_errors` and
`embed_errors` alongside `games_created`. These are exposed at `/health` so
failures are visible without reading logs.

**Two-path crawl authentication.** `POST /api/crawl/run` accepts either an HMAC
token (minted server-side, embedded in the UI button's `hx-headers`, valid for
5 minutes) or a shared secret header (`X-Crawl-Secret`) for programmatic
callers such as GitHub Actions. The raw secret never reaches the browser.

---

## Testing

**188 unit tests** cover the crawler, payload parser, image proxy, seed
export/import, and web routes. They run offline — no network calls, no running
server required.

**28 Playwright E2E tests** (`make e2e`) drive a real Chromium browser against
a live server. The suite reads `BASE_URL` from the environment and runs
identically against `localhost:8000` and `https://metacrawl.onrender.com`.
Notable assertions:

- Images are tested for `naturalWidth > 0`, not just the presence of an `<img>`
  element — this catches proxy failures and broken CDN URLs that an HTML check
  would miss.
- The game count shown on the listing page must match `/health`'s `db.games`.
- Title sort is asserted case-insensitively (SQLite's default binary collation
  puts all-caps titles before mixed-case ones; the fix uses `COLLATE NOCASE`).
- No template may contain a `metacritic.com` URL (asserted by a unit test that
  scans every `.html` file in the templates directory).

---

## What does not work yet

**Embeddings disabled in production.** Groq serves chat completions only;
`MC_EMBED_PROVIDER` is left empty in `render.yaml`. Games added by the hourly
crawl fall back to metadata-overlap similarity (weighted Jaccard: genres 60%,
platforms 40%). The UI labels each result with the method used ("embedding" or
"metadata overlap") so the degradation is visible.

**Ephemeral database.** Every Render restart restores the committed seed. Crawl
results accumulated between deploys are lost on the next restart. This is a
known consequence of the no-persistent-disk design described above.

**`mafia-the-old-country-man-of-honor` user summary.** Local Ollama returned
HTTP 500 when generating the user summary for this title. The failure is not
reproducible by sending the same prompt to Ollama directly; the root cause is
unidentified. The critic summary is present; the user summary is absent and
counted as an `llm_error` in the crawl run. No prediction is made about other
providers.

**GitHub Actions scheduled workflows.** Scheduled workflows can run several
minutes late under GitHub's queuing. They are also disabled automatically after
60 days of repository inactivity; the repo must receive a commit or the workflow
must be manually re-enabled.

**Optional task 1 (YouTube Let's Play transcription) not implemented.**

---

## AI collaboration

`ai-logs/` contains the raw Claude Code session transcripts for this project —
every user message, assistant response, and tool call (file reads, edits, shell
commands) in newline-delimited JSON. One personal email address has been
replaced with `[redacted-email]`; no other content was modified.
