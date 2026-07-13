"""Per-model context window management — ported from OpenClaude's context.ts.

Precedence (highest first):
  1. NGSAGENT_MAX_CONTEXT_TOKENS env var (admin override)
  2. Session-scoped override (set via /set_context_window)
  3. 1M-context beta header
  4. Known model registry
  5. Safe fallback (128k)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Known model context windows (subset; extend as needed)
_KNOWN_WINDOWS: dict[str, int] = {
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-7-sonnet": 200_000,
    "gpt-4o": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o3": 200_000,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "llama3.2": 128_000,
    "llama3.3": 128_000,
    "qwen2.5": 128_000,
    "deepseek-r1": 64_000,
    "deepseek-chat": 64_000,
}

_1M_SUPPORTED_PREFIXES = (
    "claude-sonnet-4",
    "claude-opus-4",
    "gpt-4.1",
    "gemini-2",
    "gemini-1.5-pro",
)

# Session-scoped overrides — module-level state (OpenClaude pattern C)
_session_overrides: dict[str, int] = {}

FALLBACK_CONTEXT_WINDOW = 128_000
COMPACT_TRIGGER_RATIO = 0.92
SUMMARY_RESERVE = 4_000
OUTPUT_RESERVE = 8_000


@dataclass
class ContextBudget:
    """Per-turn context budget snapshot, surfaced to the TUI."""

    system_tokens: int
    message_tokens: int
    tool_def_tokens: int
    reserved_tokens: int
    context_window: int
    used_ratio: float

    @property
    def effective_available(self) -> int:
        return max(
            0,
            self.context_window - self.reserved_tokens - self.system_tokens,
        )

    @property
    def should_compact(self) -> bool:
        return self.used_ratio >= COMPACT_TRIGGER_RATIO


def _canonicalize(model: str) -> str:
    """Strip provider prefix (openai/anthropic/) and lowercase."""
    lowered = model.lower()
    stripped = re.sub(r"^[a-z][\w-]*/", "", lowered)
    return stripped


def set_session_override(model: str, tokens: int) -> None:
    if tokens < 33_000:
        raise ValueError(
            f"Context window must be at least 33,000 tokens (got {tokens})"
        )
    _session_overrides[_canonicalize(model)] = tokens


def clear_session_override(model: str | None = None) -> None:
    if model is None:
        _session_overrides.clear()
    else:
        _session_overrides.pop(_canonicalize(model), None)


def supports_1m(model: str) -> bool:
    canonical = _canonicalize(model)
    return any(canonical.startswith(p) for p in _1M_SUPPORTED_PREFIXES)


def context_window_for(model: str, betas: list[str] | None = None) -> int:
    """Resolve the effective context window for a model."""
    env = os.environ.get("NGSAGENT_MAX_CONTEXT_TOKENS")
    if env and env.isdigit() and int(env) > 0:
        return int(env)

    canonical = _canonicalize(model)
    if canonical in _session_overrides:
        return _session_overrides[canonical]

    if betas and "context-1m-2025-08-07" in betas and supports_1m(canonical):
        return 1_000_000

    for prefix, size in _KNOWN_WINDOWS.items():
        if canonical.startswith(prefix):
            return size

    return FALLBACK_CONTEXT_WINDOW


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 chars/token. Calibrated by Compactor against
    real provider token counts at runtime."""
    return max(1, len(text) // 4)


def measure_budget(
    messages: list,
    tools: list,
    model: str,
    betas: list[str] | None = None,
) -> ContextBudget:
    """Estimate current context budget for a turn."""
    window = context_window_for(model, betas)
    msg_tokens = sum(estimate_tokens(_msg_to_text(m)) for m in messages)
    tool_tokens = sum(
        estimate_tokens(str(t.info().parameters)) + estimate_tokens(t.info().description)
        for t in tools
    )
    system_tokens = estimate_tokens(_system_prompt_stub())
    reserved = SUMMARY_RESERVE + OUTPUT_RESERVE
    used = system_tokens + msg_tokens + tool_tokens
    return ContextBudget(
        system_tokens=system_tokens,
        message_tokens=msg_tokens,
        tool_def_tokens=tool_tokens,
        reserved_tokens=reserved,
        context_window=window,
        used_ratio=used / window if window else 1.0,
    )


def _msg_to_text(m) -> str:
    if hasattr(m, "content") and isinstance(m.content, str):
        return m.content
    if hasattr(m, "text"):
        return m.text
    return str(m)


def _system_prompt_stub() -> str:
    # Approximate system prompt size; the real one is built per-agent
    return " " * 2_000
