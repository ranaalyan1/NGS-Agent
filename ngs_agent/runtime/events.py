"""Pubsub event bus — ported from OpenCode's pubsub.Broker pattern.

Surfaces agent lifecycle events to the TUI / headless stream-json consumer.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal[
    "session_start",
    "session_end",
    "text",
    "tool_call_start",
    "tool_call_end",
    "tool_result",
    "context",
    "error",
    "permission_request",
    "permission_response",
    "compaction",
    "usage",
]


@dataclass
class AgentEvent:
    type: EventType
    session_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:12]}")


class EventBus:
    """Async pubsub for agent events. Subscribers receive events in order."""

    def __init__(self, session_id: str, sink: Callable[[AgentEvent], None] | None = None):
        self._session_id = session_id
        self._sink = sink
        self._subscribers: list[asyncio.Queue[AgentEvent]] = []

    def subscribe(self) -> asyncio.Queue[AgentEvent]:
        q: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def publish(self, type_: EventType, **payload: Any) -> AgentEvent:
        evt = AgentEvent(type=type_, session_id=self._session_id, payload=payload)
        if self._sink:
            try:
                self._sink(evt)
            except Exception:
                pass
        for q in self._subscribers:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                pass
        return evt

    def text(self, text: str) -> None:
        self.publish("text", text=text)

    def tool_call_start(self, tool_call_id: str, name: str, args: dict) -> None:
        self.publish(
            "tool_call_start",
            tool_call_id=tool_call_id,
            name=name,
            arguments=args,
        )

    def tool_result(self, tool_call_id: str, content: str, is_error: bool) -> None:
        self.publish(
            "tool_result",
            tool_call_id=tool_call_id,
            content=content,
            is_error=is_error,
        )

    def error(self, message: str) -> None:
        self.publish("error", message=message)

    def usage(self, input_tokens: int, output_tokens: int) -> None:
        self.publish("usage", input_tokens=input_tokens, output_tokens=output_tokens)

    def context(self, budget) -> None:
        self.publish("context", budget=budget)
