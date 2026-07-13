"""Message types — shared across backends, runtime, and sessions."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    # optional pre-serialized form for backends that want raw JSON
    arguments_raw: str | None = None

    @classmethod
    def new(cls, name: str, arguments: dict[str, Any]) -> ToolCall:
        return cls(id=f"call_{uuid.uuid4().hex[:12]}", name=name, arguments=arguments)


@dataclass
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    reasoning: str | None = None
    timestamp: float = field(default_factory=time.time)

    # ---------- factories ----------
    @classmethod
    def system(cls, text: str) -> Message:
        return cls(role="system", content=text)

    @classmethod
    def user(cls, text: str) -> Message:
        return cls(role="user", content=text)

    @classmethod
    def assistant(
        cls,
        text: str = "",
        tool_calls: list[ToolCall] | None = None,
        reasoning: str | None = None,
    ) -> Message:
        return cls(
            role="assistant",
            content=text,
            tool_calls=tool_calls or [],
            reasoning=reasoning,
        )

    @classmethod
    def with_tool_results(cls, results: list[ToolResult]) -> Message:
        """Factory: build a tool-role message carrying tool results."""
        return cls(role="tool", tool_results=results)

    # ---------- introspection ----------
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    def has_tool_results(self) -> bool:
        return bool(self.tool_results)


@dataclass
class StreamEvent:
    """One event from a streaming LLM response."""

    kind: Literal["text", "tool_call_start", "tool_call_delta", "tool_call_end", "usage", "done", "error"]
    text: str | None = None
    tool_call_id: str | None = None
    tool_call_name: str | None = None
    tool_call_arguments_delta: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None


@dataclass
class CollectedStream:
    """Accumulated state from a single streamed turn."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    error: str | None = None

    def to_message(self) -> Message:
        return Message.assistant(
            text=self.text,
            tool_calls=self.tool_calls,
        )


class ContextLimitError(Exception):
    """Raised when the provider returned a context-limit error."""


class ImageRejectionError(Exception):
    """Raised when a provider 400'd with image/vision/multimodal keywords."""


def is_image_rejection(msg: str) -> bool:
    m = msg.lower()
    if "400" not in m:
        return False
    return any(k in m for k in ("image", "vision", "multimodal", "unsupported content type"))


def is_context_limit(msg: str) -> bool:
    m = msg.lower()
    return (
        "context length" in m
        or "context window" in m
        or "maximum context" in m
        or "too many tokens" in m
        or "context_length_exceeded" in m
    )


def is_stall_timeout(msg: str) -> bool:
    m = msg.lower()
    return "timeout" in m or "timed out" in m or "stream stalled" in m or "connection reset" in m
