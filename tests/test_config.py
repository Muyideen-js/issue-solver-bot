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


def test_gemini_can_be_the_environment_provider(monkeypatch):
    monkeypatch.setenv("TELEGRAM_SOLVER_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "123456789")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-key")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    validate_settings()


def _dashboard_only(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MODE", "off")
    monkeypatch.setenv("ENCRYPTION_KEY", "encryption-key")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "a-strong-password")
    for name in ("TELEGRAM_SOLVER_BOT_TOKEN", "TELEGRAM_OWNER_ID", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_dashboard_only_needs_no_telegram_or_provider_key(monkeypatch):
    """Dashboard accounts bring their own AI key, so the env key is optional."""
    _dashboard_only(monkeypatch)

    validate_settings()


def test_dashboard_only_requires_a_dashboard_password(monkeypatch):
    """Telegram off and no password would leave no way into the app at all."""
    _dashboard_only(monkeypatch)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        validate_settings()


def test_dashboard_only_still_requires_the_encryption_key(monkeypatch):
    _dashboard_only(monkeypatch)
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)

    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        validate_settings()


def test_dashboard_only_still_rejects_an_unknown_provider(monkeypatch):
    _dashboard_only(monkeypatch)
    monkeypatch.setenv("AI_PROVIDER", "llama")

    with pytest.raises(RuntimeError, match="AI_PROVIDER"):
        validate_settings()


def test_a_bad_owner_id_is_still_rejected_when_supplied(monkeypatch):
    """Off mode ignores Telegram, but a typo in a supplied value is still a bug."""
    _dashboard_only(monkeypatch)
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "@username")

    with pytest.raises(RuntimeError, match="numeric Telegram user ID"):
        validate_settings()
