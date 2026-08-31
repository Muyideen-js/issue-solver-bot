"""LLM provider configuration for the coding agent."""
import os

PROVIDERS: dict[str, dict] = {
    "deepseek": {
        "name": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "default_model": "deepseek-v4-flash",
        "models": ["deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
        "env_key": "DEEPSEEK_API_KEY",
        "env_model": "DEEPSEEK_MODEL",
        "extra_payload": {"thinking": {"type": "disabled"}},
    },
    "openai": {
        "name": "OpenAI",
        "url": "https://api.openai.com/v1/chat/completions",
        "default_model": "gpt-4o",
        "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
        "env_key": "OPENAI_API_KEY",
        "env_model": "OPENAI_MODEL",
    },
    "gemini": {
        "name": "Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "default_model": "gemini-2.0-flash",
        "models": ["gemini-2.0-flash", "gemini-2.0-pro", "gemini-1.5-pro"],
        "env_key": "GEMINI_API_KEY",
        "env_model": "GEMINI_MODEL",
    },
}

DEFAULT_PROVIDER = "deepseek"


def normalize_provider(provider: str | None) -> str:
    value = (provider or DEFAULT_PROVIDER).strip().lower()
    if value not in PROVIDERS:
        raise ValueError(f"Unsupported AI provider: {value}")
    return value


def provider_config(provider: str | None) -> dict:
    return PROVIDERS[normalize_provider(provider)]


def default_model(provider: str | None) -> str:
    return provider_config(provider)["default_model"]


def resolve_model(provider: str | None, model: str | None) -> str:
    config = provider_config(provider)
    chosen = (model or os.getenv(config["env_model"]) or config["default_model"]).strip()
    return chosen or config["default_model"]


def resolve_env_credentials(provider: str | None) -> tuple[str | None, str | None]:
    config = provider_config(provider)
    api_key = (os.getenv(config["env_key"]) or "").strip() or None
    model = (os.getenv(config["env_model"]) or "").strip() or None
    return api_key, model


def build_chat_payload(
    provider: str | None,
    messages: list[dict],
    tools: list[dict],
    model: str | None,
) -> dict:
    config = provider_config(provider)
    payload = {
        "model": resolve_model(provider, model),
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.1,
        "max_tokens": 8192,
    }
    payload.update(config.get("extra_payload", {}))
    return payload


def providers_for_api() -> list[dict]:
    return [
        {
            "id": provider_id,
            "name": config["name"],
            "default_model": config["default_model"],
            "models": list(config["models"]),
        }
        for provider_id, config in PROVIDERS.items()
    ]
