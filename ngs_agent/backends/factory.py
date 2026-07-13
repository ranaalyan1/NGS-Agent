"""Backend factory — picks the right backend from config + env."""
from __future__ import annotations

import os
from typing import Any

from .base import LLMBackend, NoBackend


def get_backend(cfg: dict[str, Any] | None = None) -> LLMBackend:
    """Resolve backend based on config + env vars.

    Priority:
      1. Explicit config["llm"] = "anthropic" | "openai" | "ollama" | "none"
      2. ANTHROPIC_API_KEY env var
      3. OPENAI_API_KEY env var
      4. OLLAMA_HOST env var
      5. NoBackend
    """
    cfg = cfg or {}

    provider = cfg.get("llm") or os.environ.get("NGSAGENT_LLM")

    if provider == "anthropic" or (not provider and os.environ.get("ANTHROPIC_API_KEY")):
        try:
            from .anthropic import AnthropicBackend
            return AnthropicBackend(
                api_key=os.environ["ANTHROPIC_API_KEY"],
                model=cfg.get("anthropic_model") or "claude-sonnet-4-20250514",
            )
        except ImportError:
            pass

    if provider == "openai" or (not provider and os.environ.get("OPENAI_API_KEY")):
        try:
            from .openai import OpenAIBackend
            return OpenAIBackend(
                api_key=os.environ.get("OPENAI_API_KEY", ""),
                base_url=cfg.get("openai_base_url") or os.environ.get("OPENAI_BASE_URL"),
                model=cfg.get("openai_model") or "gpt-4o",
            )
        except ImportError:
            pass

    if provider == "ollama" or (not provider and os.environ.get("OLLAMA_HOST")):
        try:
            from .openai import OpenAIBackend
            return OpenAIBackend(
                api_key="ollama",  # required by SDK but ignored by server
                base_url=cfg.get("ollama_host", "http://localhost:11434/v1"),
                model=cfg.get("ollama_model") or "llama3.2",
            )
        except ImportError:
            pass

    return NoBackend()
