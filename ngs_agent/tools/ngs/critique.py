"""Critique agent — second-round moderator for variant interpretation.

The interpreter agent does a single forward pass: gather evidence → classify.
The critique agent does the inverse: given a draft verdict, look for evidence
that would CHANGE the classification. This is what makes the workflow truly
multi-agent.

Workflow:
  1. Interpreter emits a draft verdict via emit_verdict
  2. Critique agent receives the verdict + the transcript of tool calls
  3. Critique explicitly searches for:
     - Evidence the verdict OVER-classified (e.g. AF higher than stated,
       benign ClinVar submitters, functional rescue experiments)
     - Evidence the verdict UNDER-classified (e.g. segregation data not cited,
       functional studies supporting pathogenicity, in-trans phase confirmed)
  4. Critique outputs a structured critique with recommended reclassification
     (or "CONFIRM" if no change)
  5. If critique recommends change, interpreter re-runs emit_verdict

This is the multi-agent behavior v0.2's "debate" was reaching for but never
achieved — the personas were parallel, not adversarial.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class CritiqueResult:
    verdict_id: str
    decision: str            # "CONFIRM" | "RECLASSIFY_UP" | "RECLASSIFY_DOWN" | "REJECT"
    original_classification: str
    recommended_classification: str | None
    reasoning: str
    missed_evidence: list[str]
    overinterpreted_evidence: list[str]
    suggested_followups: list[str]


class CritiqueVerdictTool(BaseTool):
    """The critique agent's emit_verdict equivalent — produces a structured
    critique of a draft verdict."""

    def info(self) -> ToolInfo:
        return ToolInfo(
            name="critique_verdict",
            description=(
                "Critique a draft verdict produced by the interpreter agent. "
                "Search for evidence the verdict OVER-classified "
                "(benign literature, higher-than-stated AF, rescue experiments) "
                "and evidence it UNDER-classified (uncited segregation, "
                "additional functional studies, in-trans phase confirmation). "
                "Output a CONFIRM / RECLASSIFY_UP / RECLASSIFY_DOWN / REJECT "
                "decision with rationale and follow-up suggestions. The "
                "critique agent MUST call this exactly once per verdict reviewed."
            ),
            parameters={
                "verdict_id": {"type": "string"},
                "original_classification": {"type": "string"},
                "decision": {
                    "type": "string",
                    "enum": ["CONFIRM", "RECLASSIFY_UP", "RECLASSIFY_DOWN", "REJECT"],
                },
                "recommended_classification": {
                    "type": "string",
                    "enum": ["Pathogenic", "Likely Pathogenic", "VUS",
                             "Likely Benign", "Benign"],
                },
                "reasoning": {"type": "string"},
                "missed_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "overinterpreted_evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "suggested_followups": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            required=["verdict_id", "original_classification", "decision", "reasoning"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        result = CritiqueResult(
            verdict_id=params["verdict_id"],
            decision=params["decision"],
            original_classification=params["original_classification"],
            recommended_classification=params.get("recommended_classification"),
            reasoning=params["reasoning"],
            missed_evidence=params.get("missed_evidence", []),
            overinterpreted_evidence=params.get("overinterpreted_evidence", []),
            suggested_followups=params.get("suggested_followups", []),
        )

        decision_emoji = {
            "CONFIRM": "✓",
            "RECLASSIFY_UP": "↑",
            "RECLASSIFY_DOWN": "↓",
            "REJECT": "✗",
        }[result.decision]

        content = (
            f"# Critique — verdict {result.verdict_id}\n"
            f"  Decision: [{decision_emoji}] {result.decision}\n"
            f"  Original:     {result.original_classification}\n"
        )
        if result.recommended_classification:
            content += f"  Recommended:  {result.recommended_classification}\n"
        content += f"\n## Reasoning\n{result.reasoning}\n"

        if result.missed_evidence:
            content += "\n## Missed evidence (under-classified)\n"
            for e in result.missed_evidence:
                content += f"- {e}\n"

        if result.overinterpreted_evidence:
            content += "\n## Over-interpreted evidence (over-classified)\n"
            for e in result.overinterpreted_evidence:
                content += f"- {e}\n"

        if result.suggested_followups:
            content += "\n## Suggested follow-ups\n"
            for f in result.suggested_followups:
                content += f"- {f}\n"

        return ToolResponse(
            content=content,
            metadata={"critique": {
                "verdict_id": result.verdict_id,
                "decision": result.decision,
                "original_classification": result.original_classification,
                "recommended_classification": result.recommended_classification,
                "reasoning": result.reasoning,
                "missed_evidence": result.missed_evidence,
                "overinterpreted_evidence": result.overinterpreted_evidence,
                "suggested_followups": result.suggested_followups,
            }},
        )
