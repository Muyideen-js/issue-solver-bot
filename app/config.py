"""Runtime configuration validation."""
import os


# Needed only while Telegram is enabled; see telegram_enabled().
TELEGRAM_SETTINGS = (
    "TELEGRAM_SOLVER_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
)

PROVIDER_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def telegram_enabled() -> bool:
    """False when TELEGRAM_MODE=off, i.e. the dashboard is the only interface."""
    return (os.getenv("TELEGRAM_MODE") or "polling").strip().lower() != "off"


def validate_settings() -> None:
    with_telegram = telegram_enabled()
    missing = [] if os.getenv("ENCRYPTION_KEY") else ["ENCRYPTION_KEY"]
    if with_telegram:
        missing += [name for name in TELEGRAM_SETTINGS if not os.getenv(name)]
    elif not os.getenv("DASHBOARD_PASSWORD"):
        # With Telegram off the dashboard is the only way in, and it stays
        # disabled until a password exists, which would leave no interface.
        missing.append("DASHBOARD_PASSWORD")

    provider = (os.getenv("AI_PROVIDER") or "deepseek").strip().lower()
    if provider not in PROVIDER_KEYS:
        raise RuntimeError(
            "AI_PROVIDER must be one of: deepseek, openai, gemini"
        )
    # The environment key is only the fallback for legacy Telegram-only
    # accounts. Dashboard accounts each supply their own key in AI settings.
    if with_telegram and not os.getenv(PROVIDER_KEYS[provider]):
        missing.append(PROVIDER_KEYS[provider])
    if missing:
        raise RuntimeError(f"Missing required environment settings: {', '.join(missing)}")

    owner_raw = os.getenv("TELEGRAM_OWNER_ID")
    if owner_raw:
        try:
            owner_id = int(owner_raw)
        except ValueError as exc:
            raise RuntimeError(
                "TELEGRAM_OWNER_ID must be a numeric Telegram user ID"
            ) from exc
        if owner_id <= 0:
            raise RuntimeError("TELEGRAM_OWNER_ID must be a positive Telegram user ID")

    if not os.getenv("DATABASE_URL"):
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./solver.db"
    if int(os.getenv("ASSIGNMENT_POLL_SECONDS", "300")) < 60:
        raise RuntimeError("ASSIGNMENT_POLL_SECONDS must be at least 60")
    if int(os.getenv("SOLVER_MAX_TURNS", "30")) < 1:
        raise RuntimeError("SOLVER_MAX_TURNS must be positive")
    if int(os.getenv("SOLVER_REPAIR_MAX_TURNS", "16")) < 4:
        raise RuntimeError("SOLVER_REPAIR_MAX_TURNS must be at least 4")
