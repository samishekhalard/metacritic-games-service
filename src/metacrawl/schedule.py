"""
Day logic and APScheduler setup.

DAY BOUNDARY — midnight UTC.
=============================================================================
All timestamps in the DB are naive UTC (datetime.utcnow()).  "Today" means
the current UTC calendar date.  This is intentional and documented:

  * UTC is unambiguous across deploys (server in US, team in Russia, CI in EU).
  * midnight UTC is not midnight Moscow (MSK = UTC+3) — if the target audience
    cares about "new releases for today" in Moscow time, set MC_DAY_TZ and
    update _today_start() to use that zone.  The constant is in one place.
  * The BRIEF does not specify a timezone; UTC is the safe default.

BROWSE CURSOR — persisted in crawl_runs.next_browse_url.
=============================================================================
After every successful browse run, do_crawl() stores links.next.href in
CrawlRun.next_browse_url.  On the next browse run, decide_crawl_source()
reads the most-recent successful browse run's next_browse_url.  A server
restart loses nothing because the cursor is in SQLite.

If next_browse_url is None (no prior browse run, or reached end of list),
fetch_browse_page() starts from offset 0.
"""
from __future__ import annotations

from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .db.models import CrawlRun


def _today_start() -> datetime:
    """Return midnight UTC today as a naive datetime (matches utcnow() in DB)."""
    d = datetime.now(timezone.utc).date()
    return datetime(d.year, d.month, d.day)


def decide_crawl_source(session_factory) -> tuple[str, str | None, int]:
    """
    Return (source, browse_next_url, page) for the next run.

    source = 'new-releases' if no successful new-releases run exists for today
             (UTC); otherwise 'browse'.
    browse_next_url = the links.next.href stored on the last successful browse
                      run, or None (→ start from offset 0).
    page = 1-based page number for CrawlRun.page.
    """
    today = _today_start()
    with session_factory() as session:
        nr_today = (
            session.query(CrawlRun)
            .filter(
                CrawlRun.source == "new-releases",
                CrawlRun.status == "done",
                CrawlRun.started_at >= today,
            )
            .first()
        )
        if nr_today is None:
            return "new-releases", None, 1

        last_browse = (
            session.query(CrawlRun)
            .filter(
                CrawlRun.source == "browse",
                CrawlRun.status == "done",
            )
            .order_by(CrawlRun.id.desc())
            .first()
        )
        if last_browse is None:
            return "browse", None, 1

        next_url = last_browse.next_browse_url
        page = (last_browse.page or 0) + 1
        return "browse", next_url, page


def make_scheduler(worker) -> BackgroundScheduler:
    """
    Return a BackgroundScheduler that fires do_crawl at the top of every hour.

    The caller owns start() / shutdown() around the application lifecycle.
    APScheduler runs its own thread pool; the job just calls worker.submit(),
    which is non-blocking (the actual crawl runs in CrawlWorker's daemon thread).
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: worker.submit(trigger="schedule"),
        CronTrigger(minute=0, timezone="UTC"),
        id="hourly-crawl",
        replace_existing=True,
        misfire_grace_time=300,   # tolerate up to 5-minute startup delay
    )
    return scheduler
