"""Tests for log_diagnose and multiqc_parse tools."""
import asyncio
from pathlib import Path

from ngs_agent.runtime.events import EventBus
from ngs_agent.runtime.permission import PermissionPolicy
from ngs_agent.tools.base import ToolContext
from ngs_agent.tools.ngs.log_diagnose import LogDiagnoseTool, scan_log
from ngs_agent.tools.ngs.multiqc_parse import MultiQcParseTool, parse_multiqc

DEMO_LOG = Path(__file__).resolve().parents[1] / "demo_data" / "sample.log"
DEMO_MQC = Path(__file__).resolve().parents[1] / "demo_data" / "multiqc.txt"


def _ctx():
    return ToolContext(
        session_id="test", cwd=".", permission=PermissionPolicy(),
        file_tracker=None, bus=EventBus("test"),
    )


def test_log_diagnose_finds_signatures():
    text = DEMO_LOG.read_text()
    matches = scan_log(text)
    # Should find: low mapping rate, low coverage, high dup, adapter, poor insert, GATK error
    names = {m.signature.name for m in matches}
    assert "Low Alignment Rate" in names
    assert "Low Mean Coverage" in names
    assert "High PCR Duplication" in names
    assert "GATK Error" in names


def test_log_diagnose_tool_runs():
    tool = LogDiagnoseTool()
    info = tool.info()
    assert info.name == "log_diagnose"
    result = asyncio.run(tool.run({"path": str(DEMO_LOG)}, _ctx()))
    assert not result.is_error
    assert "Low Alignment Rate" in result.content


def test_log_diagnose_clean_log():
    tool = LogDiagnoseTool()
    result = asyncio.run(tool.run({"path": "/dev/null"}, _ctx()))
    assert "No failure signatures" in result.content


def test_multiqc_parse_extracts_metrics():
    text = DEMO_MQC.read_text()
    metrics = parse_multiqc(text)
    by_name = {m["name"]: m for m in metrics}
    assert "mapping_rate" in by_name
    assert by_name["mapping_rate"]["value"] == 65.3
    assert by_name["mapping_rate"]["grade"] == "fail"
    assert "mean_coverage" in by_name
    assert by_name["mean_coverage"]["grade"] == "fail"
    assert by_name["duplication_rate"]["grade"] == "warn"  # 42.8% is between 30 and 50%


def test_multiqc_parse_tool_runs():
    tool = MultiQcParseTool()
    info = tool.info()
    assert info.name == "multiqc_parse"
    result = asyncio.run(tool.run({"path": str(DEMO_MQC)}, _ctx()))
    assert not result.is_error
    assert "FAIL" in result.content or "FAILED" in result.content
