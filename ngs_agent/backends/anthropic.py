"""Anthropic Claude backend."""

from __future__ import annotations

import os

from ngs_agent.backends.base import LLMBackend


class AnthropicBackend(LLMBackend):
    def __init__(self, model: str = "claude-sonnet-4-20250514", api_key: str | None = None) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    def complete(self, prompt: str, system: str = "") -> str:
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")

        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 2048,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        message = client.messages.create(**kwargs)
        return message.content[0].text
