"""FastAPI health service, Telegram polling, discovery poller, and solver worker."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles

from app import dashboard
from app.config import validate_settings
from app.models.database import init_db
from app.services.solver_queue import assignment_poller, solver_worker
from app.telegram_bot import build_application

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# python-telegram-bot uses token-bearing request URLs. Never emit them to logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()
    await init_db()
    stop_event = asyncio.Event()
    telegram = build_application(os.environ["TELEGRAM_SOLVER_BOT_TOKEN"])
    await telegram.initialize()
    await telegram.start()
    await telegram.updater.start_polling(drop_pending_updates=False)
    worker_task = asyncio.create_task(solver_worker(stop_event), name="solver-worker")
    poller_task = asyncio.create_task(assignment_poller(stop_event), name="assignment-poller")
    app.state.telegram = telegram
    app.state.stop_event = stop_event
    logger.info("Issue solver bot started")
    try:
        yield
    finally:
        stop_event.set()
        worker_task.cancel()
        poller_task.cancel()
        await asyncio.gather(worker_task, poller_task, return_exceptions=True)
        await telegram.updater.stop()
        await telegram.stop()
        await telegram.shutdown()


app = FastAPI(title="Issue Solver Bot", lifespan=lifespan)
app.include_router(dashboard.router)
app.mount(
    "/dashboard/assets",
    StaticFiles(directory=dashboard.STATIC_DIR / "assets"),
    name="dashboard-assets",
)


@app.get("/")
async def root():
    return {"service": "issue-solver-bot", "status": "running"}


@app.head("/", status_code=204)
async def root_head():
    """Support free uptime monitors that are restricted to lightweight HEAD checks."""
    return Response(status_code=204)


@app.get("/health")
async def health():
    return {"status": "ok"}
