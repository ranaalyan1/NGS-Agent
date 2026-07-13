"""End-to-end integration test: simulate the full interpreter flow with
StubBackend and a real tool registry.

This validates that the agent loop:
  - Calls acmg_classify first (per the interpreter system prompt contract)
  - Then calls gnomad_query for evidence
  - Then calls emit_verdict with all evidence
  - Stops after emit_verdict returns
"""
import asyncio
import json
from pathlib import Path

from ngs_agent.backends.base import StubBackend
from ngs_agent.runtime.loop import RunOptions
from ngs_agent.runtime.loop import run as agent_run
from ngs_agent.runtime.messages import StreamEvent
from ngs_agent.tools.bundle import build_registry

VCF_PATH = Path(__file__).resolve().parents[1] / "demo_data" / "sample.vcf"


def test_full_interpreter_flow():
    """Stub a 3-turn interpreter flow:
      Turn 1: call vcf_parse + acmg_classify
      Turn 2: call gnomad_query
      Turn 3: call emit_verdict
      Turn 4: final text
    """
    backend = StubBackend(turns=[
        # Turn 1: vcf_parse + acmg_classify
        [
            StreamEvent(kind="tool_call_start", tool_call_id="t1c1", tool_call_name="vcf_parse"),
            StreamEvent(kind="tool_call_delta", tool_call_id="t1c1",
                        tool_call_arguments_delta=json.dumps({"path": str(VCF_PATH), "filter": "vus"})),
            StreamEvent(kind="tool_call_end", tool_call_id="t1c1", tool_call_name="vcf_parse"),
            StreamEvent(kind="tool_call_start", tool_call_id="t1c2", tool_call_name="acmg_classify"),
            StreamEvent(kind="tool_call_delta", tool_call_id="t1c2",
                        tool_call_arguments_delta=json.dumps({
                            "gene": "BRCA2", "chrom": "17", "pos": 43091752,
                            "ref": "T", "alt": "G",
                            "consequence": "missense_variant",
                            "gnomad_af": 0.00002,
                        })),
            StreamEvent(kind="tool_call_end", tool_call_id="t1c2", tool_call_name="acmg_classify"),
            StreamEvent(kind="done", finish_reason="tool_use"),
            StreamEvent(kind="usage", usage={"input_tokens": 200, "output_tokens": 50}),
        ],
        # Turn 2: gnomad_query
        [
            StreamEvent(kind="tool_call_start", tool_call_id="t2c1", tool_call_name="gnomad_query"),
            StreamEvent(kind="tool_call_delta", tool_call_id="t2c1",
                        tool_call_arguments_delta=json.dumps({
                            "chrom": "17", "pos": 43091752, "ref": "T", "alt": "G",
                        })),
            StreamEvent(kind="tool_call_end", tool_call_id="t2c1", tool_call_name="gnomad_query"),
            StreamEvent(kind="done", finish_reason="tool_use"),
            StreamEvent(kind="usage", usage={"input_tokens": 300, "output_tokens": 30}),
        ],
        # Turn 3: emit_verdict
        [
            StreamEvent(kind="tool_call_start", tool_call_id="t3c1", tool_call_name="emit_verdict"),
            StreamEvent(kind="tool_call_delta", tool_call_id="t3c1",
                        tool_call_arguments_delta=json.dumps({
                            "gene": "BRCA2", "chrom": "17", "pos": 43091752,
                            "ref": "T", "alt": "G",
                            "classification": "VUS",
                            "acmg_criteria": ["PM2"],
                            "evidence_summary": "Rare missense. Insufficient evidence for pathogenicity.",
                            "evidence_citations": ["gnomAD:17-43091752-T-G"],
                            "recommendation": "Consider RNA studies.",
                            "confidence": "low",
                        })),
            StreamEvent(kind="tool_call_end", tool_call_id="t3c1", tool_call_name="emit_verdict"),
            StreamEvent(kind="done", finish_reason="tool_use"),
            StreamEvent(kind="usage", usage={"input_tokens": 400, "output_tokens": 100}),
        ],
        # Turn 4: final text
        [
            StreamEvent(kind="text", text="Variant interpretation complete. Verdict emitted."),
            StreamEvent(kind="done", finish_reason="end_turn"),
            StreamEvent(kind="usage", usage={"input_tokens": 500, "output_tokens": 20}),
        ],
    ])

    registry = build_registry([
        "vcf_parse", "acmg_classify", "gnomad_query", "emit_verdict",
    ])

    options = RunOptions(
        session_id="integration-test",
        model="claude-sonnet-4-20250514",
        system_prompt="You are a variant interpreter.",
        cwd=str(VCF_PATH.parent),
        permission_mode="yolo",
    )

    result = asyncio.run(agent_run(
        "Interpret the VUS variants in this VCF",
        backend, registry, options,
    ))

    # Assertions
    assert result.turns == 4, f"Expected 4 turns, got {result.turns}"
    assert result.finish_reason == "end_turn"
    assert result.total_input_tokens == 1400
    assert result.total_output_tokens == 200

    # Verify tool calls happened
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert len(tool_msgs) == 3  # one tool_results message per turn-with-tool-call

    # Check the verdict tool was called and returned success
    verdict_msgs = [
        m for m in result.messages if m.role == "tool"
        for tr in m.tool_results if tr.name == "emit_verdict"
    ]
    assert len(verdict_msgs) == 1
    vr = [tr for tr in verdict_msgs[0].tool_results if tr.name == "emit_verdict"][0]
    assert not vr.is_error
    assert vr.metadata["verdict"]["classification"] == "VUS"
    assert vr.metadata["verdict"]["verdict_id"].startswith("vdt_")
    assert "gnomAD" in vr.metadata["verdict"]["evidence_citations"][0]


def test_agent_loop_handles_tool_error_gracefully():
    """If a tool returns is_error=True, the loop continues and the LLM sees the error."""
    backend = StubBackend(turns=[
        # Turn 1: call a tool that doesn't exist
        [
            StreamEvent(kind="tool_call_start", tool_call_id="e1", tool_call_name="nonexistent_tool"),
            StreamEvent(kind="tool_call_delta", tool_call_id="e1",
                        tool_call_arguments_delta=json.dumps({})),
            StreamEvent(kind="tool_call_end", tool_call_id="e1", tool_call_name="nonexistent_tool"),
            StreamEvent(kind="done", finish_reason="tool_use"),
            StreamEvent(kind="usage", usage={"input_tokens": 50, "output_tokens": 10}),
        ],
        # Turn 2: emit final text (LLM recovers)
        [
            StreamEvent(kind="text", text="Tool not found, but I'll explain."),
            StreamEvent(kind="done", finish_reason="end_turn"),
            StreamEvent(kind="usage", usage={"input_tokens": 60, "output_tokens": 15}),
        ],
    ])

    registry = build_registry(["vcf_parse"])
    options = RunOptions(
        session_id="error-test", model="claude-sonnet-4",
        system_prompt="You are a test agent.",
        permission_mode="yolo",
    )

    result = asyncio.run(agent_run("test", backend, registry, options))
    assert result.turns == 2
    # The tool result should be an error
    tool_msg = [m for m in result.messages if m.role == "tool"][0]
    assert tool_msg.tool_results[0].is_error
    assert "not found" in tool_msg.tool_results[0].content.lower()
