"""Ollama local LLM backend."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ngs_agent.backends.base import LLMBackend


class OllamaBackend(LLMBackend):
    def __init__(self, model: str = "llama3.2", host: str = "http://localhost:11434") -> None:
        self.model = model
        self.host = host.rstrip("/")

    def complete(self, prompt: str, system: str = "") -> str:
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system

        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama unreachable at {self.host}: {exc}") from exc

        return data.get("response", "")
