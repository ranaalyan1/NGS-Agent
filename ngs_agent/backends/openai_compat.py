"""OpenAI-compatible backend.

Covers any provider that exposes an OpenAI Messages API endpoint:
OpenRouter, Groq, DeepSeek, Gemini (via OpenAI shim), LM Studio,
llama.cpp server, NVIDIA NIM, and others.

Usage in ~/.ngsagent/config.yaml:
    llm: openai_compat
    openai_compat_base_url: https://openrouter.ai/api/v1
    openai_compat_api_key: sk-or-...
    openai_compat_model: openrouter/auto
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from ngs_agent.backends.base import LLMBackend

# Well-known provider presets (base_url, default_model)
PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "openrouter":  ("https://openrouter.ai/api/v1",          "openrouter/auto"),
    "groq":        ("https://api.groq.com/openai/v1",         "llama-3.3-70b-versatile"),
    "deepseek":    ("https://api.deepseek.com/v1",            "deepseek-chat"),
    "gemini":      ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    "lmstudio":    ("http://localhost:1234/v1",               "local-model"),
    "llamacpp":    ("http://localhost:8080/v1",               "local-model"),
}


class OpenAICompatBackend(LLMBackend):
    """Call any OpenAI-compatible /chat/completions endpoint.

    Parameters
    ----------
    base_url:
        Base URL of the provider (no trailing slash).
        Defaults to OpenRouter.
    api_key:
        Bearer token. Read from env var if not provided directly.
    model:
        Model identifier as the provider expects it.
    extra_headers:
        Optional dict of extra HTTP headers (e.g. HTTP-Referer for OpenRouter).
    """

    def __init__(
        self,
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: str | None = None,
        model: str = "openrouter/auto",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_COMPAT_API_KEY", "")
        self.model = model
        self.extra_headers = extra_headers or {}

    def complete(self, prompt: str, system: str = "") -> str:
        if not self.api_key:
            raise RuntimeError(
                "No API key configured for OpenAI-compatible backend. "
                "Set openai_compat_api_key in ~/.ngsagent/config.yaml "
                "or set OPENAI_COMPAT_API_KEY environment variable."
            )

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 2048,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP {exc.code} from {self.base_url}: {body[:400]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Cannot reach {self.base_url}: {exc}"
            ) from exc

        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(
                f"Unexpected response shape from {self.base_url}: "
                f"{json.dumps(data)[:300]}"
            ) from exc
