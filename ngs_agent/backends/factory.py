"""Backend factory."""

from __future__ import annotations

from typing import Any

from ngs_agent.backends.anthropic import AnthropicBackend
from ngs_agent.backends.base import LLMBackend, NoBackend
from ngs_agent.backends.ollama import OllamaBackend


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

    return NoBackend()
