"""Abstract LLM backend interface."""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMBackend(ABC):
    """Base class for LLM providers."""

    @abstractmethod
    def complete(self, prompt: str, system: str = "") -> str:
        """Return model text for a prompt."""


class NoBackend(LLMBackend):
    """Placeholder when no LLM is configured."""

    def complete(self, prompt: str, system: str = "") -> str:
        raise RuntimeError(
            "No LLM backend configured. Run `ngsagent config wizard` or set llm in "
            "~/.ngsagent/config.yaml. Only `debate` requires an LLM; `watch` and "
            "`analyze` work without one."
        )
