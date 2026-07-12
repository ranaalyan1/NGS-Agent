"""Backend factory."""

from __future__ import annotations

from typing import Any

from ngs_agent.backends.anthropic import AnthropicBackend
from ngs_agent.backends.base import LLMBackend, NoBackend
from ngs_agent.backends.ollama import OllamaBackend
from ngs_agent.backends.openai_compat import OpenAICompatBackend, PROVIDER_PRESETS


def get_backend(cfg: dict[str, Any]) -> LLMBackend:
    llm = cfg.get("llm", "none")
    if not llm or llm == "none":
        return NoBackend()

    if llm == "anthropic":
        return AnthropicBackend(
            model=cfg.get("anthropic_model", "claude-sonnet-4-20250514"),
            api_key=cfg.get("anthropic_api_key"),
        )

    if llm == "ollama":
        return OllamaBackend(
            model=cfg.get("ollama_model", "llama3.2"),
            host=cfg.get("ollama_host", "http://localhost:11434"),
        )

    # OpenAI-compatible: generic config
    if llm == "openai_compat":
        return OpenAICompatBackend(
            base_url=cfg.get("openai_compat_base_url", "https://openrouter.ai/api/v1"),
            api_key=cfg.get("openai_compat_api_key"),
            model=cfg.get("openai_compat_model", "openrouter/auto"),
        )

    # Named provider shortcuts — each maps to OpenAICompatBackend with preset URLs
    if llm in PROVIDER_PRESETS:
        base_url, default_model = PROVIDER_PRESETS[llm]
        # API key env var convention: OPENROUTER_API_KEY, GROQ_API_KEY, etc.
        env_key = f"{llm.upper()}_API_KEY"
        import os
        api_key = cfg.get(f"{llm}_api_key") or os.environ.get(env_key, "")
        model = cfg.get(f"{llm}_model", default_model)
        extra: dict[str, str] = {}
        if llm == "openrouter":
            extra["HTTP-Referer"] = "https://github.com/ranaalyan1/NGS-Agent"
            extra["X-Title"] = "NGS-Agent"
        return OpenAICompatBackend(
            base_url=base_url,
            api_key=api_key,
            model=model,
            extra_headers=extra,
        )

    return NoBackend()
