from app.main import app


def test_root_supports_head_for_uptime_monitoring():
    root_methods = {
        method
        for route in app.routes
        if getattr(route, "path", None) == "/"
        for method in getattr(route, "methods", set())
    }
    assert {"GET", "HEAD"}.issubset(root_methods)


import types

import pytest
from fastapi import HTTPException

from app.main import (
    WEBHOOK_PATH,
    _run_background_workers,
    _telegram_mode,
    telegram_webhook,
)


def _request(headers: dict, telegram) -> types.SimpleNamespace:
    async def _json():
        return {"update_id": 1}

    return types.SimpleNamespace(
        headers=headers,
        app=types.SimpleNamespace(state=types.SimpleNamespace(telegram=telegram)),
        json=_json,
    )


def test_background_workers_default_on_and_opt_out(monkeypatch):
    monkeypatch.delenv("RUN_BACKGROUND_WORKERS", raising=False)
    assert _run_background_workers() is True
    monkeypatch.setenv("RUN_BACKGROUND_WORKERS", "false")
    assert _run_background_workers() is False


def test_telegram_mode_defaults_to_polling_and_rejects_typos(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MODE", raising=False)
    assert _telegram_mode() == "polling"
    monkeypatch.setenv("TELEGRAM_MODE", "WEBHOOK")
    assert _telegram_mode() == "webhook"
    monkeypatch.setenv("TELEGRAM_MODE", "webook")
    with pytest.raises(RuntimeError):
        _telegram_mode()


def test_webhook_route_is_registered_for_post():
    methods = {
        method
        for route in app.routes
        if getattr(route, "path", None) == WEBHOOK_PATH
        for method in getattr(route, "methods", set())
    }
    assert "POST" in methods


@pytest.mark.asyncio
async def test_webhook_rejects_a_wrong_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "right")
    processed = []
    telegram = types.SimpleNamespace(
        bot=object(), process_update=lambda update: processed.append(update)
    )

    with pytest.raises(HTTPException) as exc:
        await telegram_webhook(
            _request({"X-Telegram-Bot-Api-Secret-Token": "wrong"}, telegram)
        )

    assert exc.value.status_code == 403
    assert processed == []


@pytest.mark.asyncio
async def test_webhook_rejects_a_missing_secret_header(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MODE", "webhook")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "right")

    with pytest.raises(HTTPException) as exc:
        await telegram_webhook(_request({}, types.SimpleNamespace(bot=object())))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_webhook_is_closed_while_polling(monkeypatch):
    """Polling deployments must not expose a second update path."""
    monkeypatch.setenv("TELEGRAM_MODE", "polling")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "right")

    with pytest.raises(HTTPException) as exc:
        await telegram_webhook(
            _request(
                {"X-Telegram-Bot-Api-Secret-Token": "right"},
                types.SimpleNamespace(bot=object()),
            )
        )

    assert exc.value.status_code == 404
