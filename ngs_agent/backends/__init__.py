"""LLM backend registry."""

from ngs_agent.backends.base import LLMBackend, NoBackend
from ngs_agent.backends.factory import get_backend

__all__ = ["LLMBackend", "NoBackend", "get_backend"]
