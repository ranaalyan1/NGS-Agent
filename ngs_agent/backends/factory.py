"""Backend factory supporting multi-provider LLMs."""

from __future__ import annotations

import os
from typing import Any

from ngs_agent.backends.anthropic import AnthropicBackend
from ngs_agent.backends.base import LLMBackend, NoBackend
from ngs_agent.backends.gemini import GeminiBackend
from ngs_agent.backends.ollama import OllamaBackend
from ngs_agent.backends.openai_compat import OpenAICompatBackend, PROVIDER_PRESETS


def get_backend(cfg: dict[str, Any]) -> LLMBackend:
    llm = cfg.get("llm", "none").lower()

    if not llm or llm == "none":
        # Auto-detect from environment variables if not set in config
        if os.environ.get("GEMINI_API_KEY"):
            return GeminiBackend(model=cfg.get("gemini_model", "gemini-2.0-flash"))
        if os.environ.get("ANTHROPIC_API_KEY"):
            return AnthropicBackend(model=cfg.get("anthropic_model", "claude-3-7-sonnet-20250219"))
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAICompatBackend(
                base_url="https://api.openai.com/v1",
                api_key=os.environ.get("OPENAI_API_KEY", ""),
                model=cfg.get("openai_model", "gpt-4o"),
            )
        return NoBackend()

    if llm == "gemini":
        return GeminiBackend(
            model=cfg.get("gemini_model", "gemini-2.0-flash"),
            api_key=cfg.get("gemini_api_key"),
        )

    if llm == "anthropic":
        return AnthropicBackend(
            model=cfg.get("anthropic_model", "claude-3-7-sonnet-20250219"),
            api_key=cfg.get("anthropic_api_key"),
        )

    if llm == "ollama":
        return OllamaBackend(
            model=cfg.get("ollama_model", "llama3.2"),
            host=cfg.get("ollama_host", "http://localhost:11434"),
        )

    if llm == "openai":
        return OpenAICompatBackend(
            base_url="https://api.openai.com/v1",
            api_key=cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", ""),
            model=cfg.get("openai_model", "gpt-4o"),
        )

    if llm == "openai_compat":
        return OpenAICompatBackend(
            base_url=cfg.get("openai_compat_base_url", "https://openrouter.ai/api/v1"),
            api_key=cfg.get("openai_compat_api_key"),
            model=cfg.get("openai_compat_model", "openrouter/auto"),
        )

    if llm in PROVIDER_PRESETS:
        base_url, default_model = PROVIDER_PRESETS[llm]
        env_key = f"{llm.upper()}_API_KEY"
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
