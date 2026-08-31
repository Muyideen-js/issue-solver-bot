"""Runtime configuration validation."""
import os


REQUIRED_SETTINGS = (
    "TELEGRAM_SOLVER_BOT_TOKEN",
    "TELEGRAM_OWNER_ID",
    "ENCRYPTION_KEY",
)

PROVIDER_KEYS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}


def validate_settings() -> None:
    missing = [name for name in REQUIRED_SETTINGS if not os.getenv(name)]
    provider = (os.getenv("AI_PROVIDER") or "deepseek").strip().lower()
    if provider not in PROVIDER_KEYS:
        raise RuntimeError(
            "AI_PROVIDER must be one of: deepseek, openai, gemini"
        )
    provider_key = PROVIDER_KEYS[provider]
    if not os.getenv(provider_key):
        missing.append(provider_key)
    if missing:
        raise RuntimeError(f"Missing required environment settings: {', '.join(missing)}")

    try:
        owner_id = int(os.environ["TELEGRAM_OWNER_ID"])
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_OWNER_ID must be a numeric Telegram user ID") from exc
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
