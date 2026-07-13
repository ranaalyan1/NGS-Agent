"""Tests for the VCF parser tool."""
import asyncio
from pathlib import Path

from ngs_agent.runtime.events import EventBus
from ngs_agent.runtime.permission import PermissionPolicy
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.ngs.vcf_parse import VcfParseTool, classify, parse_vcf_file

DEMO_VCF = Path(__file__).resolve().parents[1] / "demo_data" / "sample.vcf"


def test_parse_vcf_file_returns_variants():
    variants = parse_vcf_file(DEMO_VCF)
    assert len(variants) == 4
    assert variants[0].gene == "BRCA2"
    assert variants[0].classification == "VUS"
    assert variants[1].classification == "Pathogenic"
    assert variants[3].classification == "Other"  # benign


def test_classify_paths():
    assert classify("Pathogenic") == "Pathogenic"
    assert classify("Likely pathogenic") == "Pathogenic"
    assert classify("Uncertain_significance") == "VUS"
    assert classify("VUS") == "VUS"
    assert classify("Benign") == "Other"
    assert classify(None) == "Other"
    # Conflicting must not be Pathogenic
    assert classify("Conflicting classifications of pathogenicity") == "Other"


def test_vcf_parse_tool_runs():
    tool = VcfParseTool()
    info = tool.info()
    assert info.name == "vcf_parse"
    assert "path" in info.parameters

    bus = EventBus("test")
    ctx = ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=bus,
    )

    result = asyncio.run(tool.run({"path": str(DEMO_VCF)}, ctx))
    assert not result.is_error
    assert "BRCA2" in result.content
    assert result.metadata["variant_count"] == 4


def test_vcf_parse_tool_filter_vus():
    tool = VcfParseTool()
    bus = EventBus("test")
    ctx = ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=bus,
    )
    result = asyncio.run(tool.run({"path": str(DEMO_VCF), "filter": "vus"}, ctx))
    assert result.metadata["variant_count"] == 1  # only the VUS one


def test_vcf_parse_tool_missing_file():
    tool = VcfParseTool()
    bus = EventBus("test")
    ctx = ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=bus,
    )
    result = asyncio.run(tool.run({"path": "/nonexistent.vcf"}, ctx))
    assert result.is_error
