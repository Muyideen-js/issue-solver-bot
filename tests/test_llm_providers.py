import pytest

from app.services.llm_providers import (
    build_chat_payload,
    default_model,
    normalize_provider,
    providers_for_api,
    resolve_env_credentials,
)


def test_providers_for_api_lists_all_connectors():
    providers = providers_for_api()
    assert [item["id"] for item in providers] == ["deepseek", "openai", "gemini"]
    assert "deepseek-v4-flash" in providers[0]["models"]
    assert "gpt-4o" in providers[1]["models"]
    assert "gemini-2.0-flash" in providers[2]["models"]


def test_normalize_provider_rejects_unknown_values():
    with pytest.raises(ValueError, match="Unsupported AI provider"):
        normalize_provider("anthropic")


def test_build_chat_payload_includes_deepseek_thinking_flag():
    payload = build_chat_payload("deepseek", [{"role": "user", "content": "hi"}], [], None)
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["model"] == default_model("deepseek")


def test_build_chat_payload_omits_deepseek_fields_for_openai():
    payload = build_chat_payload("openai", [{"role": "user", "content": "hi"}], [], "gpt-4o-mini")
    assert "thinking" not in payload
    assert payload["model"] == "gpt-4o-mini"


def test_resolve_env_credentials_uses_provider_specific_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-pro")
    key, model = resolve_env_credentials("gemini")
    assert key == "gemini-key"
    assert model == "gemini-2.0-pro"
