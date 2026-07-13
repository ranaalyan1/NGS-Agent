"""Anthropic backend — streaming + tool-use + prompt caching + 1M-context beta.

Key features (ported from OpenCode's pattern):
  - cache_control: ephemeral on last 3 user messages + last tool definition
  - tool-use via Anthropic Messages API
  - 1M-context beta header for supported models
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from ..runtime.messages import Message, StreamEvent
from ..tools.base import BaseTool
from .base import LLMBackend, Request


class AnthropicBackend(LLMBackend):
    def __init__(self, api_key: str, model: str | None = None):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "anthropic package not installed. Run: pip install 'ngs-agent[llm]'"
            ) from e
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._default_model = model or "claude-sonnet-4-20250514"

    @property
    def name(self) -> str:
        return "anthropic"

    def _build_messages(self, messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                continue  # system is separate
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                content: list[dict] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                if content:
                    out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                results = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_call_id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                    for tr in m.tool_results
                ]
                out.append({"role": "user", "content": results})
        return out

    def _inject_cache_control(self, messages: list[dict]) -> list[dict]:
        """Add cache_control: ephemeral to the last 3 user messages."""
        user_indices = [
            i for i, m in enumerate(messages)
            if m.get("role") == "user"
        ]
        for i in user_indices[-3:]:
            content = messages[i]["content"]
            if isinstance(content, str):
                messages[i]["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }]
            elif isinstance(content, list) and content:
                if isinstance(content[-1], dict):
                    content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
        return messages

    def _build_tools(self, tools: list[BaseTool]) -> list[dict]:
        out: list[dict] = []
        for t in tools:
            info = t.info()
            tool_def = {
                "name": info.name,
                "description": info.description,
                "input_schema": {
                    "type": "object",
                    "properties": info.parameters,
                    "required": info.required,
                },
            }
            out.append(tool_def)
        # Cache control on the last tool definition (OpenCode pattern)
        if out:
            out[-1] = {**out[-1], "cache_control": {"type": "ephemeral"}}
        return out

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        model = request.model or self._default_model
        messages = self._build_messages(request.messages)
        messages = self._inject_cache_control(messages)
        tools = self._build_tools(request.tools) if request.tools else None

        extra_headers = {}
        if request.betas and "context-1m-2025-08-07" in request.betas:
            extra_headers["anthropic-beta"] = "context-1m-2025-08-07"

        try:
            async with self._client.messages.stream(
                model=model,
                system=request.system,
                messages=messages,
                tools=tools,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                extra_headers=extra_headers if extra_headers else None,
            ) as stream:
                current_tool: dict | None = None
                current_tool_args = ""

                async for event in stream:
                    et = event.type

                    if et == "message_start":
                        pass

                    elif et == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            current_tool = {
                                "id": block.id,
                                "name": block.name,
                            }
                            current_tool_args = ""
                            yield StreamEvent(
                                kind="tool_call_start",
                                tool_call_id=block.id,
                                tool_call_name=block.name,
                            )

                    elif et == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield StreamEvent(kind="text", text=delta.text)
                        elif delta.type == "input_json_delta":
                            current_tool_args += delta.partial_json
                            yield StreamEvent(
                                kind="tool_call_delta",
                                tool_call_id=current_tool["id"] if current_tool else None,
                                tool_call_arguments_delta=delta.partial_json,
                            )

                    elif et == "content_block_stop":
                        if current_tool:
                            yield StreamEvent(
                                kind="tool_call_end",
                                tool_call_id=current_tool["id"],
                                tool_call_name=current_tool["name"],
                            )
                            current_tool = None
                            current_tool_args = ""

                    elif et == "message_delta":
                        # usage info comes here
                        if hasattr(event, "usage") and event.usage:
                            yield StreamEvent(
                                kind="usage",
                                usage={
                                    "input_tokens": getattr(event.usage, "input_tokens", 0) or 0,
                                    "output_tokens": getattr(event.usage, "output_tokens", 0) or 0,
                                },
                            )

                    elif et == "message_stop":
                        pass

                final = await stream.get_final_message()
                yield StreamEvent(
                    kind="done",
                    finish_reason=final.stop_reason or "end_turn",
                )
                yield StreamEvent(
                    kind="usage",
                    usage={
                        "input_tokens": final.usage.input_tokens,
                        "output_tokens": final.usage.output_tokens,
                    },
                )

        except Exception as e:
            yield StreamEvent(kind="error", error=f"{type(e).__name__}: {e}")

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1_000) -> str:
        try:
            resp = await self._client.messages.create(
                model=self._default_model,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
            )
            return resp.content[0].text if resp.content else ""
        except Exception as e:
            return f"[compaction failed: {e}]"
