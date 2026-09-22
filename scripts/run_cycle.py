"""One-shot discovery + solve cycle for scheduled runs (e.g. GitHub Actions cron).

The web app's assignment_poller and solver_worker loop forever, which is what
keeps a host awake and burns metered uptime. This script does a single pass and
exits, so the runner and the database can go back to sleep between runs.

Usage: python -m scripts.run_cycle
"""
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from app.config import validate_settings
from app.models.database import init_db
from app.services.solver_queue import drain_queue, run_discovery_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Matches app.main: python-telegram-bot puts the token in request URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("run_cycle")


async def _stop_after(stop_event: asyncio.Event, seconds: int) -> None:
    """Ask the lanes to stop claiming new work once the budget is spent.

    The event is only checked between jobs, so an in-flight solve finishes
    rather than being cancelled half-way through a push.
    """
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return
    logger.warning("Cycle budget of %ss reached; finishing current jobs then exiting", seconds)
    stop_event.set()


def _require_durable_database() -> None:
    """A scheduled run gets a fresh filesystem, so SQLite would start empty.

    Losing job state that way makes the bot re-solve issues it already has open
    PRs for, so fail fast instead of quietly doing damage.
    """
    url = os.getenv("DATABASE_URL", "")
    if url and not url.startswith("sqlite"):
        return
    if os.getenv("ALLOW_EPHEMERAL_DB", "").strip().lower() == "true":
        logger.warning("Running against ephemeral SQLite because ALLOW_EPHEMERAL_DB=true")
        return
    raise RuntimeError(
        "DATABASE_URL must point at a durable database (e.g. Postgres) for a "
        "scheduled run; set ALLOW_EPHEMERAL_DB=true only for local testing."
    )


async def main() -> int:
    _require_durable_database()
    validate_settings()
    await init_db()

    budget = max(60, int(os.getenv("CYCLE_MAX_SECONDS", "3000")))
    stop_event = asyncio.Event()
    timer = asyncio.create_task(_stop_after(stop_event, budget))

    try:
        queued = await run_discovery_once()
        logger.info("Discovery queued %s new job(s)", queued)
        processed = await drain_queue(stop_event)
        logger.info("Processed %s job(s) this cycle", processed)
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
