"""Proactive + reactive compaction — ported from Zero's compactor pattern.

Proactive: if estimated tokens > 92% of window, summarize oldest middle BEFORE
sending the next request.

Reactive: on ContextLimitError from the provider, compact once and retry the
same turn.

The compactor keeps the first message (system/user context) and the last 4
messages (recent work); summarizes everything in between.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .context import (
    measure_budget,
)
from .messages import Message

if TYPE_CHECKING:
    from ..backends.base import LLMBackend


KEEP_HEAD = 1       # first message
KEEP_TAIL = 4       # last 4 messages
MIN_MESSAGES_TO_COMPACT = KEEP_HEAD + KEEP_TAIL + 3  # don't compact tiny sessions


class Compactor:
    def __init__(self, backend: LLMBackend, model: str, betas: list[str] | None = None):
        self.backend = backend
        self.model = model
        self.betas = betas
        self._calibration = 1.0  # estimated / real

    def should_compact(self, messages: list[Message], tools: list) -> bool:
        if len(messages) < MIN_MESSAGES_TO_COMPACT:
            return False
        budget = measure_budget(messages, tools, self.model, self.betas)
        return budget.should_compact

    async def maybe_compact(self, messages: list[Message], tools: list) -> list[Message]:
        if not self.should_compact(messages, tools):
            return messages
        return await self._do_compact(messages, tools)

    async def compact_now(self, messages: list[Message], tools: list) -> list[Message]:
        """Reactive compaction — called from loop on ContextLimitError."""
        if len(messages) < MIN_MESSAGES_TO_COMPACT:
            # Too short to compact; drop the oldest non-system message instead
            return messages[1:] if len(messages) > 1 else messages
        return await self._do_compact(messages, tools)

    async def _do_compact(self, messages: list[Message], tools: list) -> list[Message]:
        """Compact the middle of the conversation.

        CRITICAL: tool_result messages are preserved VERBATIM — summarizing
        them would lose evidence citations (gnomAD AFs, ClinVar UIDs, PMIDs)
        that downstream verdicts must reference. Only assistant text and
        reasoning get summarized; tool results stay intact.
        """
        head = messages[:KEEP_HEAD]
        middle = messages[KEEP_HEAD:-KEEP_TAIL]
        tail = messages[-KEEP_TAIL:]

        if not middle:
            return messages

        # Partition middle into "to summarize" (assistant text/reasoning) and
        # "preserve verbatim" (tool results)
        to_summarize: list[Message] = []
        preserved: list[Message] = []
        for m in middle:
            if m.role == "tool" and m.tool_results:
                preserved.append(m)
            else:
                to_summarize.append(m)

        if not to_summarize:
            # Only tool results — nothing to summarize, just keep them
            return [*head, *preserved, *tail]

        summary = await self._summarize(to_summarize)
        compacted_msg = Message.user(
            f"[Compacted prior context — {len(to_summarize)} messages summarized]\n\n"
            f"Preserved tool results from this range: {len(preserved)} message(s).\n\n"
            f"Summary:\n{summary}"
        )
        # Insert compacted summary BEFORE the preserved tool results so the LLM
        # sees context first, then evidence
        return [*head, compacted_msg, *preserved, *tail]

    async def _summarize(self, messages: list[Message]) -> str:
        transcript = "\n\n".join(self._render(m) for m in messages)
        prompt = (
            "Summarize the following conversation history. Preserve:\n"
            "1. All variant IDs, gene symbols, and genomic coordinates mentioned.\n"
            "2. All evidence citations (gnomAD AF, ClinVar classifications, PubMed IDs).\n"
            "3. All ACMG criteria applied and the resulting classification.\n"
            "4. Any open questions or pending tool calls.\n\n"
            f"History:\n{transcript}\n\nSummary:"
        )
        try:
            resp = await self.backend.complete(
                prompt,
                system="You are a meticulous variant-interpretation scribe. Be terse but complete.",
                max_tokens=2_000,
            )
            return resp
        except Exception as e:
            # Degrade gracefully: keep a truncated transcript
            return f"[Compaction failed: {e}]\n\n{transcript[:4_000]}..."

    def _render(self, m: Message) -> str:
        out = f"[{m.role}]"
        if m.content:
            out += f" {m.content}"
        if m.tool_calls:
            for tc in m.tool_calls:
                out += f"\n  tool_call: {tc.name}({json.dumps(tc.arguments)})"
        if m.tool_results:
            for tr in m.tool_results:
                tag = "ERROR" if tr.is_error else "ok"
                out += f"\n  tool_result[{tag}] {tr.name}: {tr.content[:200]}"
        return out

    def calibrate(self, estimated: int, real_input_tokens: int) -> None:
        """Adjust estimator against the provider's real token count."""
        if real_input_tokens > 0 and estimated > 0:
            self._calibration = real_input_tokens / estimated
