"""emit_verdict — the structured-output tool the interpreter agent calls
to commit a final classification. Produces a machine-readable verdict
with evidence trail, suitable for clinical audit.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


class EmitVerdictTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="emit_verdict",
            description=(
                "Emit a final structured variant-interpretation verdict. The "
                "interpreter agent MUST call this exactly once per variant after "
                "gathering evidence. Produces a machine-readable JSON verdict with "
                "evidence trail, suitable for clinical audit and CAP/CLIA review."
            ),
            parameters={
                "gene": {"type": "string"},
                "chrom": {"type": "string"},
                "pos": {"type": "integer"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
                "classification": {
                    "type": "string",
                    "enum": ["Pathogenic", "Likely Pathogenic", "VUS", "Likely Benign", "Benign"],
                },
                "acmg_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ACMG codes applied (e.g. ['PVS1', 'PM2', 'PP3'])",
                },
                "evidence_summary": {"type": "string"},
                "evidence_citations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "PMIDs, ClinVar UIDs, gnomAD variant IDs",
                },
                "recommendation": {"type": "string"},
                "confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                },
                "limitations": {"type": "string"},
            },
            required=["gene", "chrom", "pos", "ref", "alt", "classification",
                      "acmg_criteria", "evidence_summary"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        # Validate required fields
        required = ["gene", "chrom", "pos", "ref", "alt", "classification",
                    "acmg_criteria", "evidence_summary"]
        missing = [f for f in required if f not in params or params[f] is None or params[f] == ""]
        if missing:
            return ToolResponse(
                content=f"emit_verdict missing required fields: {', '.join(missing)}",
                is_error=True,
            )

        verdict = {
            "verdict_id": f"vdt_{uuid.uuid4().hex[:12]}",
            "session_id": ctx.session_id,
            "timestamp": time.time(),
            "gene": params["gene"],
            "variant": {
                "chrom": params["chrom"],
                "pos": params["pos"],
                "ref": params["ref"],
                "alt": params["alt"],
            },
            "classification": params["classification"],
            "acmg_criteria": params["acmg_criteria"],
            "evidence_summary": params["evidence_summary"],
            "evidence_citations": params.get("evidence_citations", []),
            "recommendation": params.get("recommendation", ""),
            "confidence": params.get("confidence", "medium"),
            "limitations": params.get("limitations", ""),
        }

        return ToolResponse(
            content=(
                f"# Verdict emitted: {verdict['classification']}\n\n"
                f"**{verdict['gene']}** {verdict['variant']['chrom']}:"
                f"{verdict['variant']['pos']}{verdict['variant']['ref']}>"
                f"{verdict['variant']['alt']}\n\n"
                f"ACMG: {', '.join(verdict['acmg_criteria']) or 'none'}\n"
                f"Confidence: {verdict['confidence']}\n"
                f"Verdict ID: {verdict['verdict_id']}\n\n"
                f"## Evidence\n{verdict['evidence_summary']}\n\n"
                f"## Citations\n" +
                "\n".join(f"- {c}" for c in verdict["evidence_citations"])
                + f"\n\n## Recommendation\n{verdict['recommendation']}"
                + (f"\n\n## Limitations\n{verdict['limitations']}" if verdict["limitations"] else "")
            ),
            metadata={"verdict": verdict},
        )
