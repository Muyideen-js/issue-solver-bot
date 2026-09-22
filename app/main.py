"""FastAPI health service, Telegram polling, discovery poller, and solver worker."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from hmac import compare_digest

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from telegram import Update

from app import dashboard
from app.config import validate_settings
from app.models.database import bootstrap_admin, init_db
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


WEBHOOK_PATH = "/telegram/webhook"


def _telegram_mode() -> str:
    """polling keeps a process alive; webhook lets an idle host spin down.

    A free host that sleeps cannot long-poll Telegram: once it spins down the
    updater stops and commands go unanswered until something else wakes it.
    In webhook mode Telegram's own POST is the inbound request that wakes it.
    """
    mode = os.getenv("TELEGRAM_MODE", "polling").strip().lower()
    if mode not in {"polling", "webhook", "off"}:
        raise RuntimeError(f"TELEGRAM_MODE must be polling, webhook or off; got {mode!r}")
    return mode


def _run_background_workers() -> bool:
    """False on hosts billed for uptime, where a scheduled run does this work."""
    return os.getenv("RUN_BACKGROUND_WORKERS", "true").strip().lower() != "false"


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_settings()
    await init_db()
    await bootstrap_admin()
    stop_event = asyncio.Event()
    mode = _telegram_mode()
    telegram = None
    if mode != "off":
        telegram = build_application(os.environ["TELEGRAM_SOLVER_BOT_TOKEN"])
        await telegram.initialize()
        await telegram.start()
        if mode == "polling":
            await telegram.updater.start_polling(drop_pending_updates=False)
        else:
            base = os.environ["TELEGRAM_WEBHOOK_BASE"].rstrip("/")
            await telegram.bot.set_webhook(
                url=f"{base}{WEBHOOK_PATH}",
                secret_token=os.environ["TELEGRAM_WEBHOOK_SECRET"],
                drop_pending_updates=False,
            )
    tasks = []
    if _run_background_workers():
        tasks.append(asyncio.create_task(solver_worker(stop_event), name="solver-worker"))
        tasks.append(
            asyncio.create_task(assignment_poller(stop_event), name="assignment-poller")
        )
    app.state.telegram = telegram
    app.state.stop_event = stop_event
    logger.info(
        "Issue solver bot started (telegram=%s, background_workers=%s)",
        mode,
        bool(tasks),
    )
    try:
        yield
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if telegram is not None:
            if mode == "polling":
                await telegram.updater.stop()
            await telegram.stop()
            await telegram.shutdown()


app = FastAPI(title="Issue Solver Bot", lifespan=lifespan)
# Every dashboard action is a same-origin fetch() JSON call, never an HTML form
# POST, so SameSite=Lax already blocks cross-site requests from carrying this
# cookie — a separate CSRF token isn't needed on top of that.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["ENCRYPTION_KEY"],
    same_site="lax",
    https_only=os.getenv("DASHBOARD_COOKIE_SECURE", "true").lower() != "false",
)
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


@app.post(WEBHOOK_PATH, status_code=204)
async def telegram_webhook(request: Request):
    """Receive Telegram updates in webhook mode.

    Telegram echoes the secret in this header; rejecting a mismatch keeps
    anyone who guesses the path from injecting updates.
    """
    telegram = getattr(request.app.state, "telegram", None)
    if telegram is None or _telegram_mode() != "webhook":
        raise HTTPException(status_code=404, detail="Webhook mode is not enabled")
    expected = os.environ["TELEGRAM_WEBHOOK_SECRET"]
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Bad webhook secret")
    update = Update.de_json(await request.json(), telegram.bot)
    await telegram.process_update(update)
    return Response(status_code=204)
