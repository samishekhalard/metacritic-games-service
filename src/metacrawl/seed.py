"""
Seed data — export the local database to a JSON file committed to the repo,
and restore it on first boot when the target database is empty.

WHY A SEED FILE
───────────────
Render free-tier instances start with a fresh disk on each deploy.  Rather
than bootstrapping from zero every time, we commit a snapshot of the local
database (40–60 games with summaries) so the service is immediately useful
after deploy without running any crawls.

The seed is ONLY loaded when the `games` table is empty (never overwrites
existing data), so re-deploys and restarts after the first crawl are safe.

FORMAT
──────
JSON with one key per table, rows serialised as list-of-dicts.  Datetimes
are stored as ISO-8601 strings and accepted by SQLite on insert.  The
`game_genres` association table is included as a plain list of dicts.

NULL values are preserved.  The embedding `vector` column stays as a
JSON-serialised float list (already TEXT in the schema).

COMMAND LINE
────────────
  # Export:
  PYTHONPATH=src python3 -m metacrawl.seed export [--output seeds/seed.json]

  # Manual restore (normally called automatically on boot):
  PYTHONPATH=src python3 -m metacrawl.seed import [--input seeds/seed.json]
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

log = logging.getLogger(__name__)

# Tables exported/imported in dependency order (parents before children).
_TABLES = [
    "games",
    "platform_scores",
    "game_genres",
    "reviews",
    "summaries",
    "embeddings",
]

# Default seed path relative to the repository root (also copied into the
# Docker image via `COPY seeds/ ./seeds/`).
DEFAULT_SEED_PATH = Path(__file__).parent.parent.parent / "seeds" / "seed.json"


# ── Export ────────────────────────────────────────────────────────────────────

class _DatetimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def export_seed(engine, path: Path, processed_only: bool = False) -> dict:
    """
    Dump tracked tables to JSON and write to `path`.

    processed_only — when True, include only games that have at least one
    summary row, plus up to 5 null-score unreleased games as deliberate
    evidence that no-review games are handled correctly.  All child rows
    (platform_scores, reviews, embeddings, etc.) are filtered to match the
    selected game IDs.  Reduces the seed file size significantly.

    Returns a stats dict suitable for printing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {"version": 1, "generated_at": datetime.utcnow().isoformat()}
    stats: dict = {}

    with engine.connect() as conn:
        if processed_only:
            game_ids = _select_processed_ids(conn)
        else:
            game_ids = None  # export all

        for table in _TABLES:
            rows = _fetch_table(conn, table, game_ids)
            data[table] = rows
            stats[table] = len(rows)

    path.write_text(json.dumps(data, indent=2, cls=_DatetimeEncoder), encoding="utf-8")
    log.info("seed exported to %s (%s)", path, stats)
    return stats


def _select_processed_ids(conn) -> list:
    """Return game IDs for processed_only export: games with summaries + 5 unreleased."""
    result = conn.execute(text("SELECT DISTINCT game_id FROM summaries"))
    processed = [row[0] for row in result]
    processed_set = set(processed)

    result = conn.execute(text(
        "SELECT id FROM games WHERE metascore IS NULL LIMIT 10"
    ))
    unreleased = [row[0] for row in result if row[0] not in processed_set][:5]

    return processed + unreleased


def _fetch_table(conn, table: str, game_ids) -> list:
    """Fetch all rows from a table, optionally filtered to the given game_ids."""
    if game_ids is None:
        result = conn.execute(text(f"SELECT * FROM {table}"))
        return [dict(r._mapping) for r in result]

    if table == "games":
        id_list = ",".join(str(i) for i in game_ids)
        result = conn.execute(text(f"SELECT * FROM games WHERE id IN ({id_list})"))
    elif table in ("platform_scores", "game_genres", "reviews", "summaries", "embeddings"):
        id_list = ",".join(str(i) for i in game_ids)
        result = conn.execute(text(f"SELECT * FROM {table} WHERE game_id IN ({id_list})"))
    else:
        result = conn.execute(text(f"SELECT * FROM {table}"))

    return [dict(r._mapping) for r in result]


# ── Import / restore ──────────────────────────────────────────────────────────

def restore_if_empty(engine, path: Path = DEFAULT_SEED_PATH) -> bool:
    """
    Load the seed file into the database only when the `games` table is empty.

    Returns True if the seed was loaded, False if the DB already had rows or
    no seed file was found.  Never overwrites existing data.
    """
    if not path.exists():
        log.info("no seed file at %s — starting with empty database", path)
        return False

    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM games")).scalar()
    if count > 0:
        log.info("database already has %d games — skipping seed restore", count)
        return False

    log.info("restoring seed from %s", path)
    _import_seed(engine, path)
    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM games")).scalar()
    log.info("seed restore complete: %d games now in database", n)
    return True


def _import_seed(engine, path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))

    with engine.begin() as conn:
        for table in _TABLES:
            rows = data.get(table, [])
            if not rows:
                continue
            cols = ", ".join(rows[0].keys())
            placeholders = ", ".join(f":{k}" for k in rows[0].keys())
            stmt = text(
                f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})"
            )
            for row in rows:
                conn.execute(stmt, row)
    log.debug("seed import finished")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli():
    import argparse

    from .db.session import init_db, make_engine, make_session_factory  # noqa: F401

    parser = argparse.ArgumentParser(description="Metacrawl seed management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    exp = sub.add_parser("export", help="Dump local DB to seed file")
    exp.add_argument("--output", type=Path, default=DEFAULT_SEED_PATH)
    exp.add_argument(
        "--processed-only",
        action="store_true",
        help=(
            "Only export games that have at least one summary, plus up to 5 "
            "unreleased (null-score) games as evidence.  Produces a much smaller "
            "seed file."
        ),
    )

    imp = sub.add_parser("import", help="Restore seed into (empty) DB")
    imp.add_argument("--input", type=Path, default=DEFAULT_SEED_PATH)
    imp.add_argument("--force", action="store_true",
                     help="Import even if database is not empty (dangerous)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    engine = make_engine()
    init_db(engine)

    if args.cmd == "export":
        stats = export_seed(engine, args.output, processed_only=args.processed_only)
        for table, n in stats.items():
            print(f"  {table}: {n} rows")
        print(f"Seed written to {args.output}")

    elif args.cmd == "import":
        if args.force:
            _import_seed(engine, args.input)
            print(f"Seed imported from {args.input} (--force)")
        else:
            restored = restore_if_empty(engine, args.input)
            if restored:
                print(f"Seed restored from {args.input}")
            else:
                print("Database is not empty — skipping (use --force to override)")


if __name__ == "__main__":
    _cli()
