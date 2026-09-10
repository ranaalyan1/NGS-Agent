"""Unit tests for the Temporal-free pipeline engine (swarm.engine).

These tests inject a fake ``run_agent`` coroutine so no Docker, Temporal,
Redis, or MinIO infrastructure is required. They pin down the stage ordering,
halt propagation, conditional trimming, alignment retry, and DNA vs RNA
branching that the engine preserves from workflows/pipeline_workflow.py.
"""

from __future__ import annotations

import pytest

from swarm.engine import RunInput, SampleRunInput, run_pipeline, run_sample


def make_fake_run_agent(responses=None, calls=None):
    """Build a fake agent runner that records calls and returns canned JSON."""
    responses = responses or {}
    calls = calls if calls is not None else []

    async def run_agent(agent_name, inputs, routing_ctx, timeout=None):
        calls.append(
            {"agent": agent_name, "inputs": inputs, "routing_ctx": routing_ctx, "timeout": timeout}
        )
        resp = responses.get(agent_name)
        if callable(resp):
            resp = resp(agent_name, inputs, routing_ctx)
        if resp is None:
            resp = {"status": "ok", "payload": {}, "reasoning": f"{agent_name} ok"}
        return dict(resp)

    return run_agent


def default_responses(overrides=None):
    responses = {
        "ingest": {"status": "ok", "payload": {"read_count": 1000, "is_paired": False}},
        "qc": {"status": "ok", "payload": {"verdict": "pass", "report_html": "s3://b/qc.html"}},
        "ai_decider": {"status": "ok", "payload": {"trim": False, "trim_params": {}}},
        "align": {
            "status": "ok",
            "payload": {"alignment_status": "ok", "mapping_rate": 0.95, "bam_path": "s3://b/x.bam"},
        },
        "count": {"status": "ok", "payload": {"count_matrix": "s3://b/counts.tsv", "n_genes": 20000}},
        "de_agent": {"status": "ok", "payload": {"artifacts": {}}},
        "insight_agent": {"status": "ok", "payload": {"ai_summary": "summary"}},
        "report_builder": {"status": "ok", "payload": {"report_html": "s3://b/index.html"}},
        "report_agent": {"status": "ok", "payload": {"narrative": "done"}},
        # DNA-branch agents
        "bwa_agent": {
            "status": "ok",
            "payload": {"artifacts": {"bam_path": "s3://b/x.bam", "coverage_depth_csv": "s3://b/cov.csv"}},
        },
        "gatk_agent": {"status": "ok", "payload": {"final_bam": "s3://b/f.bam", "variants_vcf": "s3://b/v.vcf"}},
        "annotation_agent": {
            "status": "ok",
            "payload": {"annotated_vcf": "s3://b/a.vcf", "variants_csv": "s3://b/v.csv", "coverage_depth_csv": "s3://b/cov.csv"},
        },
        "coverage_agent": {"status": "ok", "payload": {"mean_depth": 30}},
    }
    responses.update(overrides or {})
    return responses


def sample_input(sample_id="sample-01", experiment="RNA-Seq", initial=None, routing_extra=None):
    routing = {
        "experiment_type": experiment,
        "organism": "human",
        "paired_end": False,
        "reference_genome": "hg38",
        "run_id": f"run-x-{sample_id}",
        **(routing_extra or {}),
    }
    initial = initial or {"sample_id": sample_id, "fastq_path": "/data/s.fastq"}
    return SampleRunInput(
        run_id=f"run-x-{sample_id}",
        sample_id=sample_id,
        experiment_type=experiment,
        routing_context=routing,
        initial_inputs=initial,
    )


def pipeline_input(experiment="RNA-Seq", samples=None, run_id="run-x"):
    samples = samples or [
        {"sample_id": "sample-01", "condition": "unknown", "fastq_path": "/data/s.fastq"}
    ]
    return RunInput(
        run_id=run_id,
        experiment_type=experiment,
        routing_context={"experiment_type": experiment, "organism": "human", "run_id": run_id},
        initial_inputs={"samples": samples},
    )


# ---------------------------------------------------------------------------
# RNA-Seq flow
# ---------------------------------------------------------------------------

RNA_SAMPLE_SEQUENCE = [
    "ingest",
    "qc",
    "ai_decider",
    "align",
    "count",
    "de_agent",
    "insight_agent",
    "report_builder",
    "report_agent",
]

BATCH_TAIL = ["de_agent", "insight_agent", "report_builder", "report_agent"]


def test_rna_single_sample_full_flow():
    calls = []
    run_agent = make_fake_run_agent(default_responses(), calls)
    result = asyncio_run(run_pipeline(pipeline_input(), run_agent))

    agents = [c["agent"] for c in calls]
    assert agents == RNA_SAMPLE_SEQUENCE + BATCH_TAIL
    assert result["status"] == "complete"
    assert result["samples_processed"] == 1
    assert result["report_html"] == "s3://b/index.html"
    assert result["report_narrative"] == "done"
    assert result["sample_results"][0]["status"] == "complete"


def test_rna_stage_timeouts_passed_through():
    calls = []
    run_agent = make_fake_run_agent(default_responses(), calls)
    asyncio_run(run_pipeline(pipeline_input(), run_agent))

    by_agent = {c["agent"]: c["timeout"] for c in calls}
    assert by_agent["align"] == 2 * 3600
    assert by_agent["ingest"] == 5 * 60
    assert by_agent["qc"] == 15 * 60


def test_trim_requested_before_align():
    calls = []
    responses = default_responses(
        {"ai_decider": {"status": "ok", "payload": {"trim": True, "trim_params": {"LEADING": 5}}}}
    )
    run_agent = make_fake_run_agent(responses, calls)
    result = asyncio_run(run_sample(sample_input(), run_agent))

    agents = [c["agent"] for c in calls]
    assert agents.index("trim") == agents.index("ai_decider") + 1
    assert agents.index("align") == agents.index("trim") + 1
    assert result["trim_was_run"] is True


def test_halt_on_qc_stops_pipeline():
    calls = []
    responses = default_responses(
        {"qc": {"status": "fail", "halt": True, "halt_reason": "unusable reads", "payload": {}}}
    )
    run_agent = make_fake_run_agent(responses, calls)
    result = asyncio_run(run_sample(sample_input(), run_agent))

    assert result["status"] == "halted"
    assert result["agent"] == "qc"
    assert result["reason"] == "unusable reads"
    assert [c["agent"] for c in calls] == ["ingest", "qc"]


# ---------------------------------------------------------------------------
# Alignment retry
# ---------------------------------------------------------------------------

def test_align_retry_with_re_trim():
    calls = []
    align_calls = {"n": 0}

    def align_response(agent_name, inputs, routing_ctx):
        align_calls["n"] += 1
        if align_calls["n"] == 1:
            return {
                "status": "ok",
                "payload": {
                    "alignment_status": "fail",
                    "mapping_rate": 0.3,
                    "ai_evaluation": {"action": "re_trim", "new_trim_params": {"LEADING": 10}},
                },
            }
        return {"status": "ok", "payload": {"alignment_status": "ok", "mapping_rate": 0.92, "bam_path": "s3://b/x.bam"}}

    responses = default_responses({"align": align_response})
    run_agent = make_fake_run_agent(responses, calls)
    result = asyncio_run(run_sample(sample_input(), run_agent))

    agents = [c["agent"] for c in calls]
    assert agents.count("align") == 2
    assert agents.count("trim") == 1
    assert agents.index("trim") == agents.index("align") + 1  # re-trim after failed align
    assert result["status"] == "complete"
    assert result["trim_was_run"] is True


def test_align_retry_exhausted_marks_failure():
    calls = []
    responses = default_responses(
        {
            "align": {
                "status": "ok",
                "payload": {
                    "alignment_status": "fail",
                    "mapping_rate": 0.2,
                    "ai_evaluation": {"action": "re_trim", "new_trim_params": {}},
                },
            }
        }
    )
    run_agent = make_fake_run_agent(responses, calls)
    sample_result = asyncio_run(run_sample(sample_input(), run_agent))

    assert sample_result["status"] == "failed_at_alignment"
    assert [c["agent"] for c in calls].count("align") == 2

    # Batch level still runs report_builder/report_agent but skips de/insight.
    batch_calls = []
    batch_agent = make_fake_run_agent(default_responses(), batch_calls)
    batch_result = asyncio_run(run_pipeline(pipeline_input(), batch_agent))
    tail = [c["agent"] for c in batch_calls][-2:]
    assert tail == ["report_builder", "report_agent"]
    assert batch_result["status"] == "complete"


# ---------------------------------------------------------------------------
# DNA branch
# ---------------------------------------------------------------------------

DNA_SAMPLE_SEQUENCE = [
    "ingest",
    "qc",
    "ai_decider",
    "bwa_agent",
    "gatk_agent",
    "annotation_agent",
    "coverage_agent",
    "report_builder",
    "report_agent",
]


def test_dna_flow_skips_count_and_de():
    calls = []
    run_agent = make_fake_run_agent(default_responses(), calls)
    result = asyncio_run(run_sample(sample_input(experiment="WGS"), run_agent))

    agents = [c["agent"] for c in calls]
    assert agents == DNA_SAMPLE_SEQUENCE
    assert result["status"] == "complete"
    assert "count" not in agents
    assert "de_agent" not in agents
    assert result["outputs"]["variants_csv"] == "s3://b/v.csv"


def test_dna_batch_skips_aggregate_de_insight():
    calls = []
    run_agent = make_fake_run_agent(default_responses(), calls)
    result = asyncio_run(run_pipeline(pipeline_input(experiment="WGS"), run_agent))

    agents = [c["agent"] for c in calls]
    # sample DNA sequence + batch report only (no de/insight aggregation)
    assert agents == DNA_SAMPLE_SEQUENCE + ["report_builder", "report_agent"]
    assert result["samples_processed"] == 1


# ---------------------------------------------------------------------------
# Batch aggregation
# ---------------------------------------------------------------------------

def test_batch_two_samples_sequential_and_aggregated():
    calls = []
    run_agent = make_fake_run_agent(default_responses(), calls)
    result = asyncio_run(
        run_pipeline(
            pipeline_input(
                samples=[
                    {"sample_id": "sample-01", "condition": "control", "fastq_path": "/data/a.fastq"},
                    {"sample_id": "sample-02", "condition": "treated", "fastq_path": "/data/b.fastq"},
                ]
            ),
            run_agent,
        )
    )

    agents = [c["agent"] for c in calls]
    # Two full per-sample RNA sequences, then a single aggregate de/insight/report.
    assert agents == RNA_SAMPLE_SEQUENCE + RNA_SAMPLE_SEQUENCE + BATCH_TAIL
    assert result["samples_processed"] == 2
    assert [s["sample_id"] for s in result["sample_results"]] == ["sample-01", "sample-02"]
    assert agents.count("de_agent") == 3  # 2 per-sample + 1 aggregate
    assert agents.count("insight_agent") == 3


# ---------------------------------------------------------------------------
# Sample-list fallback
# ---------------------------------------------------------------------------

def test_pipeline_without_samples_falls_back_to_fastq_path():
    calls = []
    run_agent = make_fake_run_agent(default_responses(), calls)
    result = asyncio_run(
        run_pipeline(
            RunInput(
                run_id="run-x",
                experiment_type="RNA-Seq",
                routing_context={"experiment_type": "RNA-Seq", "run_id": "run-x"},
                initial_inputs={"fastq_path": "/data/fallback.fastq"},
            ),
            run_agent,
        )
    )
    assert result["samples_processed"] == 1
    first_ingest = calls[0]
    assert first_ingest["agent"] == "ingest"
    assert first_ingest["inputs"].get("fastq_path") == "/data/fallback.fastq"


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
