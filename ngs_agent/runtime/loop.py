"""The agent loop — ported from Zero's internal/agent/loop.go.

This is the heart of v0.3. It replaces the serial 3-call debate.py with a
true tool-use loop:

  while not done:
    1. Partition tools (only expose schemas the model needs this turn)
    2. Proactive compaction if history approaches context window
    3. Stream LLM response with tool calls
    4. Stall recovery: re-issue only when no visible prose was forwarded
    5. Reactive compaction: on ContextLimitError, compact and retry
    6. Execute tool calls in parallel
    7. Append tool results
    8. Continue until LLM emits no tool calls or max_turns hit
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..backends.base import LLMBackend, Request
from ..tools.registry import Registry
from .compactor import Compactor
from .context import measure_budget
from .events import EventBus
from .evidence_graph import EvidenceGraph
from .file_tracker import FileTracker
from .messages import (
    CollectedStream,
    ContextLimitError,
    ImageRejectionError,
    Message,
    ToolCall,
    ToolResult,
    is_context_limit,
    is_image_rejection,
    is_stall_timeout,
)
from .permission import PermissionPolicy

MAX_TURNS_DEFAULT = 25
MAX_STREAM_STALL_RETRIES = 1
STALL_BACKOFF_BASE = 1.5
CONTEXT_LIMIT_MAX_RETRIES = 1


@dataclass
class RunOptions:
    session_id: str
    model: str
    system_prompt: str
    cwd: str = "."
    max_turns: int = MAX_TURNS_DEFAULT
    permission_mode: str = "auto"
    betas: list[str] | None = None
    file_tracker: FileTracker | None = None
    on_text: Callable[[str], None] | None = None
    on_tool_call_start: Callable[[str, str, dict], None] | None = None
    on_tool_result: Callable[[str, str, bool], None] | None = None
    on_context: Callable[[Any], None] | None = None
    on_event: Callable[[Any], None] | None = None
    permission_callback: Callable[[str, str, dict], bool] | None = None


@dataclass
class RunResult:
    messages: list[Message]
    turns: int
    finish_reason: str
    error: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0


async def run(
    prompt: str,
    backend: LLMBackend,
    registry: Registry,
    options: RunOptions,
    prior_messages: list[Message] | None = None,
) -> RunResult:
    """Execute an agentic loop. Returns the final message list and metrics."""

    # Compose initial message list
    if prior_messages:
        messages = list(prior_messages)
        messages.append(Message.user(prompt))
    else:
        messages = [Message.user(prompt)]

    compactor = Compactor(backend, options.model, options.betas)
    bus = EventBus(options.session_id, options.on_event)
    policy = PermissionPolicy(options.permission_mode)
    file_tracker = options.file_tracker or FileTracker()
    evidence_graph = EvidenceGraph()

    bus.publish("session_start", model=options.model, prompt=prompt[:500])

    total_in = 0
    total_out = 0

    for turn in range(options.max_turns):
        # 1. Partition tools
        exposed_tools = registry.partition_for_run(permission_mode=options.permission_mode)

        # 2. Proactive compaction
        try:
            messages = await compactor.maybe_compact(messages, exposed_tools)
        except Exception as e:
            bus.error(f"Proactive compaction failed (non-fatal): {e}")

        # 3. Build request
        request = Request(
            model=options.model,
            system=options.system_prompt,
            messages=messages,
            tools=exposed_tools,
            betas=options.betas,
            prompt_cache_key=options.session_id,
        )

        # 4. Context budget notification
        if options.on_context:
            budget = measure_budget(messages, exposed_tools, options.model, options.betas)
            options.on_context(budget)
            bus.context(budget)

        # 5. Stream with stall recovery
        try:
            collected = await _stream_with_recovery(
                backend, request, options, bus
            )
        except ContextLimitError as e:
            # Reactive compaction
            bus.publish("compaction", kind="reactive", reason=str(e))
            try:
                messages = await compactor.compact_now(messages, exposed_tools)
                # Retry this turn
                continue
            except Exception as comp_err:
                return RunResult(
                    messages, turn + 1, "error",
                    error=f"Reactive compaction failed: {comp_err}",
                )
        except ImageRejectionError as e:
            return RunResult(messages, turn + 1, "image_rejected", error=str(e))

        if collected.error:
            return RunResult(messages, turn + 1, "error", error=collected.error)

        # 6. Calibrate compactor
        if collected.usage:
            total_in += collected.usage.get("input_tokens", 0)
            total_out += collected.usage.get("output_tokens", 0)
            bus.usage(
                collected.usage.get("input_tokens", 0),
                collected.usage.get("output_tokens", 0),
            )

        # 7. Append assistant message
        messages.append(collected.to_message())

        # 8. No tool calls? Done.
        if not collected.tool_calls:
            bus.publish("session_end", turns=turn + 1, finish_reason=collected.finish_reason or "stop")
            return RunResult(
                messages, turn + 1, collected.finish_reason or "stop",
                total_input_tokens=total_in, total_output_tokens=total_out,
            )

        # 9. Execute tool calls in parallel
        tool_results = await _execute_tool_calls(
            collected.tool_calls,
            registry,
            policy,
            file_tracker,
            options,
            bus,
            evidence_graph,
        )
        messages.append(Message.with_tool_results(tool_results))

    # Max turns reached
    return RunResult(
        messages, options.max_turns, "max_turns",
        total_input_tokens=total_in, total_output_tokens=total_out,
    )


async def _stream_with_recovery(
    backend: LLMBackend,
    request: Request,
    options: RunOptions,
    bus: EventBus,
) -> CollectedStream:
    """Stream with bounded stall retry. Re-issue only when no visible prose."""
    forwarded_visible = False

    for attempt in range(MAX_STREAM_STALL_RETRIES + 1):
        collected = CollectedStream()

        async for evt in backend.stream(request):
            if evt.kind == "text":
                forwarded_visible = True
                collected.text += evt.text or ""
                if options.on_text:
                    options.on_text(evt.text or "")
                bus.text(evt.text or "")
            elif evt.kind == "tool_call_start":
                tc = ToolCall(
                    id=evt.tool_call_id or "",
                    name=evt.tool_call_name or "",
                    arguments={},
                )
                collected.tool_calls.append(tc)
                if options.on_tool_call_start:
                    options.on_tool_call_start(tc.id, tc.name, {})
                bus.tool_call_start(tc.id, tc.name, {})
            elif evt.kind == "tool_call_delta":
                # accumulate raw args; parsed later
                if collected.tool_calls:
                    last = collected.tool_calls[-1]
                    if last.arguments_raw is None:
                        last.arguments_raw = ""
                    last.arguments_raw += evt.tool_call_arguments_delta or ""
            elif evt.kind == "tool_call_end":
                # parse accumulated args
                if collected.tool_calls:
                    last = collected.tool_calls[-1]
                    raw = last.arguments_raw or "{}"
                    try:
                        last.arguments = json.loads(raw)
                    except json.JSONDecodeError:
                        last.arguments = {"_parse_error": raw[:500]}
            elif evt.kind == "usage":
                collected.usage = evt.usage
            elif evt.kind == "done":
                collected.finish_reason = evt.finish_reason
            elif evt.kind == "error":
                collected.error = evt.error

        # Check for image rejection
        if collected.error and is_image_rejection(collected.error):
            raise ImageRejectionError(
                f"Model rejected image: {collected.error}. "
                "Try a vision-capable model (claude-sonnet-4, gpt-4o, gemini-2.0-flash)."
            )

        # Check for context limit
        if collected.error and is_context_limit(collected.error):
            raise ContextLimitError(collected.error)

        # Stall retry: only if no visible prose forwarded
        if (
            collected.error
            and is_stall_timeout(collected.error)
            and not forwarded_visible
            and not collected.text
            and attempt < MAX_STREAM_STALL_RETRIES
        ):
            await asyncio.sleep(STALL_BACKOFF_BASE ** (attempt + 1))
            continue

        return collected

    return collected


async def _execute_tool_calls(
    tool_calls: list[ToolCall],
    registry: Registry,
    policy: PermissionPolicy,
    file_tracker: FileTracker,
    options: RunOptions,
    bus: EventBus,
    evidence_graph: EvidenceGraph | None = None,
) -> list[ToolResult]:
    """Execute all tool calls in parallel; each gates through permission policy."""

    async def _one(tc: ToolCall) -> ToolResult:
        tool = registry.get(tc.name)
        if tool is None:
            content = f"Tool '{tc.name}' not found. Available: {registry.list_names()[:20]}"
            bus.tool_result(tc.id, content, True)
            return ToolResult(
                tool_call_id=tc.id, name=tc.name, content=content, is_error=True
            )

        decision = policy.decide(tc.name, tc.arguments)
        if not decision.allow and decision.ask_prompt:
            if options.permission_callback:
                # Support both sync and async permission callbacks
                import inspect
                result = options.permission_callback(options.session_id, tc.name, tc.arguments)
                if inspect.isawaitable(result):
                    approved = await result
                else:
                    approved = result
                if not approved:
                    content = f"User denied tool call: {tc.name}"
                    bus.tool_result(tc.id, content, True)
                    return ToolResult(
                        tool_call_id=tc.id, name=tc.name, content=content, is_error=True
                    )
            else:
                # No callback in headless mode — auto-deny
                content = f"Permission required for {tc.name} (mode={policy.mode}). Run with --permission yolo to auto-approve, or set up an interactive TUI."
                bus.tool_result(tc.id, content, True)
                return ToolResult(
                    tool_call_id=tc.id, name=tc.name, content=content, is_error=True
                )

        if not decision.allow and decision.deny_reason:
            content = f"Denied: {decision.deny_reason}"
            bus.tool_result(tc.id, content, True)
            return ToolResult(
                tool_call_id=tc.id, name=tc.name, content=content, is_error=True
            )

        try:
            from ..tools.base import ToolContext
            ctx = ToolContext(
                session_id=options.session_id,
                cwd=options.cwd,
                permission=policy,
                file_tracker=file_tracker,
                bus=bus,
                evidence_graph=evidence_graph,
            )
            resp = await asyncio.wait_for(
                tool.run(tc.arguments, ctx), timeout=120.0
            )
            if options.on_tool_result:
                options.on_tool_result(tc.id, resp.content[:200], resp.is_error)
            bus.tool_result(tc.id, resp.content, resp.is_error)
            return ToolResult(
                tool_call_id=tc.id,
                name=tc.name,
                content=resp.content,
                is_error=resp.is_error,
                metadata=resp.metadata,
            )
        except TimeoutError:
            content = f"Tool {tc.name} timed out after 120s"
            bus.tool_result(tc.id, content, True)
            return ToolResult(
                tool_call_id=tc.id, name=tc.name, content=content, is_error=True
            )
        except Exception as e:
            content = f"Tool {tc.name} raised: {type(e).__name__}: {e}"
            bus.tool_result(tc.id, content, True)
            return ToolResult(
                tool_call_id=tc.id, name=tc.name, content=content, is_error=True
            )

    return await asyncio.gather(*[_one(tc) for tc in tool_calls])
