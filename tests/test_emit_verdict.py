"""Tests for emit_verdict tool — the structured-output sink for the interpreter."""
import asyncio

from ngs_agent.runtime.events import EventBus
from ngs_agent.runtime.permission import PermissionPolicy
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.ngs.emit_verdict import EmitVerdictTool


def _ctx():
    return ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=EventBus("test"),
    )


def test_emit_verdict_basic():
    tool = EmitVerdictTool()
    info = tool.info()
    assert info.name == "emit_verdict"
    result = asyncio.run(tool.run({
        "gene": "BRCA2",
        "chrom": "17", "pos": 43091752, "ref": "T", "alt": "G",
        "classification": "VUS",
        "acmg_criteria": ["PM2", "PP3"],
        "evidence_summary": "Rare missense in BRCA2. Functional studies inconclusive.",
        "evidence_citations": ["PMID:12345678", "ClinVar:VCV000123"],
        "recommendation": "Consider RNA studies + segregation.",
        "confidence": "low",
    }, _ctx()))
    assert not result.is_error
    assert "VUS" in result.content
    assert result.metadata["verdict"]["classification"] == "VUS"
    assert result.metadata["verdict"]["verdict_id"].startswith("vdt_")
    assert result.metadata["verdict"]["evidence_citations"] == ["PMID:12345678", "ClinVar:VCV000123"]


def test_emit_verdict_missing_required():
    tool = EmitVerdictTool()
    result = asyncio.run(tool.run({
        "gene": "BRCA2",
        # missing required fields
    }, _ctx()))
    # Should either error or return error response
    assert result.is_error or "BRCA2" not in result.content
