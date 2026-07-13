"""Backend interface — streaming + tool-use + cache control.

All backends implement this. The agent loop is provider-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..runtime.messages import Message, StreamEvent
from ..tools.base import BaseTool


@dataclass
class Request:
    model: str
    system: str
    messages: list[Message]
    tools: list[BaseTool] = field(default_factory=list)
    betas: list[str] | None = None
    max_tokens: int = 4_000
    temperature: float = 0.0
    prompt_cache_key: str | None = None


class LLMBackend(ABC):
    """Provider-agnostic LLM backend."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        """Stream a completion. Yields StreamEvent objects.

        Must eventually yield:
          - one or more 'text' events with content fragments
          - 'tool_call_start' / 'tool_call_delta' / 'tool_call_end' for each tool call
          - 'usage' with input_tokens + output_tokens
          - 'done' with finish_reason
          - 'error' on failure (with message)
        """
        ...
        yield  # pragma: no cover  -- makes mypy happy

    @abstractmethod
    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1_000) -> str:
        """Simple one-shot completion. Used by the compactor for summaries."""
        ...


class NoBackend(LLMBackend):
    """Sentinel backend when no LLM is configured."""

    @property
    def name(self) -> str:
        return "none"

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            kind="error",
            error=(
                "No LLM backend configured. The agent loop requires an LLM. "
                "Run `ngsagent config wizard` or set ANTHROPIC_API_KEY / OPENAI_API_KEY."
            ),
        )

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1_000) -> str:
        raise RuntimeError(
            "No LLM backend configured. Run `ngsagent config wizard`."
        )


class StubBackend(LLMBackend):
    """In-memory backend for tests. Returns a canned sequence of turns.

    Each call to stream() consumes the next "turn" in the script. A turn is
    a list of StreamEvents that ends with a 'done' event. Pass a list of turns.
    """

    @property
    def name(self) -> str:
        return "stub"

    def __init__(self, turns: list[list[StreamEvent]] | None = None):
        # Accept either a flat list (treated as one turn) or a list of turns
        if turns is None:
            self._turns: list[list[StreamEvent]] = []
        elif turns and isinstance(turns[0], StreamEvent):
            # flat list — single turn
            self._turns = [turns]
        else:
            self._turns = turns
        self._cursor = 0
        self.calls: list[Request] = []

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        self.calls.append(request)
        if self._cursor >= len(self._turns):
            # Default: just emit a stop
            yield StreamEvent(kind="done", finish_reason="end_turn")
            return
        turn = self._turns[self._cursor]
        self._cursor += 1
        for evt in turn:
            yield evt

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1_000) -> str:
        # Find the last text event across all remaining turns
        for turn in reversed(self._turns[self._cursor:]):
            for evt in reversed(turn):
                if evt.kind == "text" and evt.text:
                    return evt.text
        return "stub completion"
