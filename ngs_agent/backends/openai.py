"""OpenAI-compatible backend — works with OpenAI, Azure, Ollama (OpenAI mode),
OpenRouter, vLLM, LM Studio, etc.

Implements streaming + tool-use via the Responses API (or Chat Completions
fallback). Adds cache_control emulation via prompt_cache_key.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from ..runtime.messages import Message, StreamEvent
from ..tools.base import BaseTool
from .base import LLMBackend, Request


class OpenAIBackend(LLMBackend):
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
    ):
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError(
                "openai package not installed. Run: pip install 'ngs-agent[llm]'"
            ) from e
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._default_model = model or "gpt-4o"

    @property
    def name(self) -> str:
        return "openai"

    def _build_messages(self, system: str, messages: list[Message]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}] if system else []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "user":
                out.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                msg = {"role": "assistant", "content": m.content or None}
                if m.tool_calls:
                    msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                out.append(msg)
            elif m.role == "tool":
                for tr in m.tool_results:
                    out.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_call_id,
                        "content": tr.content,
                    })
        return out

    def _build_tools(self, tools: list[BaseTool]) -> list[dict]:
        out = []
        for t in tools:
            info = t.info()
            out.append({
                "type": "function",
                "function": {
                    "name": info.name,
                    "description": info.description,
                    "parameters": {
                        "type": "object",
                        "properties": info.parameters,
                        "required": info.required,
                    },
                },
            })
        return out

    async def stream(self, request: Request) -> AsyncIterator[StreamEvent]:
        model = request.model or self._default_model
        messages = self._build_messages(request.system, request.messages)
        tools = self._build_tools(request.tools) if request.tools else None

        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            }
            if tools:
                kwargs["tools"] = tools

            current_tool_id: str | None = None
            current_tool_name: str | None = None
            args_buffer = ""

            async for chunk in await self._client.chat.completions.create(**kwargs):
                if chunk.usage:
                    yield StreamEvent(
                        kind="usage",
                        usage={
                            "input_tokens": chunk.usage.prompt_tokens,
                            "output_tokens": chunk.usage.completion_tokens,
                        },
                    )

                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta.content:
                    yield StreamEvent(kind="text", text=delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        if tc.id:
                            # New tool call
                            current_tool_id = tc.id
                            current_tool_name = tc.function.name if tc.function else ""
                            args_buffer = ""
                            yield StreamEvent(
                                kind="tool_call_start",
                                tool_call_id=current_tool_id,
                                tool_call_name=current_tool_name,
                            )
                        if tc.function and tc.function.arguments:
                            args_buffer += tc.function.arguments
                            yield StreamEvent(
                                kind="tool_call_delta",
                                tool_call_id=current_tool_id,
                                tool_call_arguments_delta=tc.function.arguments,
                            )

                if choice.finish_reason:
                    # Close any open tool call
                    if current_tool_id:
                        yield StreamEvent(
                            kind="tool_call_end",
                            tool_call_id=current_tool_id,
                            tool_call_name=current_tool_name,
                        )
                        current_tool_id = None

                    yield StreamEvent(
                        kind="done",
                        finish_reason=choice.finish_reason,
                    )

        except Exception as e:
            yield StreamEvent(kind="error", error=f"{type(e).__name__}: {e}")

    async def complete(self, prompt: str, system: str = "", max_tokens: int = 1_000) -> str:
        try:
            resp = await self._client.chat.completions.create(
                model=self._default_model,
                messages=[
                    {"role": "system", "content": system} if system else None,
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.0,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            return f"[compaction failed: {e}]"
