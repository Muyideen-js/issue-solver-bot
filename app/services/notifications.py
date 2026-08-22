"""Telegram notifications independent of command-handler lifecycle."""
import logging
import os

import httpx

logger = logging.getLogger(__name__)


async def notify(telegram_id: str, message: str) -> None:
    token = os.getenv("TELEGRAM_SOLVER_BOT_TOKEN", "")
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": telegram_id, "text": message[:4000]},
            )
        response.raise_for_status()
    except Exception as exc:
        logger.error("Telegram notification failed: %s", exc)
