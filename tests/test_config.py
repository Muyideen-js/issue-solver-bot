import pytest

from app.config import validate_settings


def _required(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SOLVER_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")


def test_owner_id_must_be_numeric(monkeypatch):
    _required(monkeypatch)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "@username")
    with pytest.raises(RuntimeError, match="numeric Telegram user ID"):
        validate_settings()


def test_valid_owner_id_is_accepted(monkeypatch):
    _required(monkeypatch)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "123456789")
    validate_settings()
