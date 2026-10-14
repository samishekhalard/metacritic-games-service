"""
Background-thread wrapper for crawl jobs.

ARCHITECTURE DECISION
─────────────────────
The HTTP client uses time.sleep for rate-limiting (1 req/s, ~20 s per run).
If do_crawl were called inside a FastAPI request handler it would hold a
uvicorn worker thread for the full duration, starving every other request.

Rule: do_crawl is ONLY ever called from a CrawlWorker thread.
      The force-run API endpoint calls worker.submit() and returns immediately.
      The SSE monitor streams progress from the crawl_runs table.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from .db.models import CrawlRun

log = logging.getLogger(__name__)


class CrawlWorker:
    """Single-slot background worker.

    Guarantees at most one crawl runs at a time; a second submit() while
    the first is alive is a no-op that returns False.
    """

    def __init__(
        self,
        session_factory,
        on_done: Callable[[CrawlRun], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._on_done = on_done
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def submit(self, trigger: str = "manual") -> bool:
        """
        Start a crawl in a daemon thread.

        Returns True if the thread was started, False if one was already running.
        """
        if self.is_running:
            return False
        with self._lock:
            if self.is_running:
                return False
            t = threading.Thread(
                target=self._run,
                kwargs={"trigger": trigger},
                daemon=True,
                name="crawl-worker",
            )
            self._thread = t
            t.start()
            log.info("crawl worker thread started (trigger=%s)", trigger)
            return True

    def _run(self, trigger: str) -> None:
        from .crawl import do_crawl

        try:
            run = do_crawl(self._session_factory, trigger=trigger)
            log.info(
                "crawl done: created=%s updated=%s errors=%s",
                run.games_created, run.games_updated, run.errors,
            )
            if self._on_done is not None:
                self._on_done(run)
        except Exception:
            log.exception("crawl worker thread raised")
