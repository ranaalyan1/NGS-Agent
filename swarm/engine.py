"""Temporal-free execution engine for the NGS swarm pipeline.

This module mirrors the orchestration logic of
:mod:`workflows.pipeline_workflow` (``NGSSampleWorkflow`` and
``NGSPipelineWorkflow``) as plain ``asyncio`` coroutines. Instead of
``workflow.execute_activity`` it calls an injectable ``run_agent`` coroutine,
which by default is :meth:`swarm.runner.AgentRunner.run_agent` and executes the
per-tool Docker images directly.

No Temporal server, Postgres, Redis, or external worker is required. The
pipeline decisions — halt handling, conditional trimming, alignment retry with
AI-guided re-trim, DNA vs RNA branching, and batch-level DE/insight
aggregation — are preserved verbatim from the Temporal workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from shared.models import AgentResult

RunAgent = Callable[..., Awaitable[Dict[str, Any]]]
ProgressFn = Callable[[str], None]


@dataclass
class RunInput:
    run_id: str
    experiment_type: str
    routing_context: Dict[str, Any]
    initial_inputs: Dict[str, Any]


@dataclass
class SampleRunInput:
    run_id: str
    sample_id: str
    experiment_type: str
    routing_context: Dict[str, Any]
    initial_inputs: Dict[str, Any]


# Per-stage wall-clock timeouts (seconds), matching the start_to_close
# timeouts declared in workflows/pipeline_workflow.py.
STAGE_TIMEOUTS: Dict[str, float] = {
    "ingest": 5 * 60,
    "qc": 15 * 60,
    "ai_decider": 5 * 60,
    "trim": 30 * 60,
    "align": 2 * 3600,
    "bwa_agent": 4 * 3600,
    "gatk_agent": 6 * 3600,
    "annotation_agent": 45 * 60,
    "coverage_agent": 10 * 60,
    "count": 30 * 60,
    "de_agent": 25 * 60,
    "insight_agent": 15 * 60,
    "report_builder": 10 * 60,
    "report_agent": 8 * 60,
}


def _halted_response(run_id: str, agent_name: str, raw_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    result = AgentResult.from_dict(raw_result)
    if result.halt:
        return {
            "run_id": run_id,
            "status": "halted",
            "reason": result.halt_reason or result.reasoning or f"{agent_name} requested halt",
            "agent": agent_name,
        }
    return None


async def _run_stage(
    run_agent: RunAgent,
    agent_name: str,
    inputs: Dict[str, Any],
    routing_ctx: Dict[str, Any],
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    if progress:
        sample = routing_ctx.get("sample_id", "")
        progress(f"→ {agent_name}" + (f" (sample {sample})" if sample else ""))
    return await run_agent(
        agent_name,
        inputs,
        routing_ctx,
        timeout=STAGE_TIMEOUTS.get(agent_name),
    )


async def run_sample(
    input_data: SampleRunInput, run_agent: RunAgent, progress: Optional[ProgressFn] = None
) -> Dict[str, Any]:
    """Run the per-sample pipeline stages. Mirrors ``NGSSampleWorkflow.run``."""
    is_dna = input_data.experiment_type in {"WGS", "WES"}

    ingest = await _run_stage(run_agent, "ingest", input_data.initial_inputs, input_data.routing_context, progress)
    if halted := _halted_response(input_data.run_id, "ingest", ingest):
        return halted

    qc = await _run_stage(run_agent, "qc", ingest, input_data.routing_context, progress)
    if halted := _halted_response(input_data.run_id, "qc", qc):
        return halted

    ai_decision = await _run_stage(run_agent, "ai_decider", qc, input_data.routing_context, progress)
    if halted := _halted_response(input_data.run_id, "ai_decider", ai_decision):
        return halted

    trim_was_run = False
    if ai_decision.get("payload", {}).get("trim", False):
        trim_request = {
            "payload": {
                **qc.get("payload", {}),
                "trim_params": ai_decision.get("payload", {}).get("trim_params", {}),
            }
        }
        align_input = await _run_stage(run_agent, "trim", trim_request, input_data.routing_context, progress)
        if halted := _halted_response(input_data.run_id, "trim", align_input):
            return halted
        trim_was_run = True
    else:
        align_input = ingest

    if is_dna:
        bwa = await _run_stage(run_agent, "bwa_agent", align_input, input_data.routing_context, progress)
        if halted := _halted_response(input_data.run_id, "bwa_agent", bwa):
            return halted

        gatk = await _run_stage(run_agent, "gatk_agent", bwa, input_data.routing_context, progress)
        if halted := _halted_response(input_data.run_id, "gatk_agent", gatk):
            return halted

        annotation = await _run_stage(
            run_agent,
            "annotation_agent",
            {**gatk, "panel_bed": input_data.routing_context.get("panel_bed")},
            input_data.routing_context,
            progress,
        )
        if halted := _halted_response(input_data.run_id, "annotate_agent", annotation):
            return halted

        coverage = await _run_stage(
            run_agent,
            "coverage_agent",
            {
                "payload": {
                    "coverage_depth_csv": annotation.get("payload", {}).get("coverage_depth_csv")
                    or bwa.get("payload", {}).get("artifacts", {}).get("coverage_depth_csv")
                }
            },
            input_data.routing_context,
            progress,
        )
        if halted := _halted_response(input_data.run_id, "coverage_agent", coverage):
            return halted

        report = await _run_stage(
            run_agent,
            "report_builder",
            {
                "payload": {
                    "qc": qc,
                    "align": bwa,
                    "count": {},
                    "de": {},
                    "insight": {},
                    "annotation": annotation,
                    "variants_csv": annotation.get("payload", {}).get("variants_csv"),
                    "coverage_depth_png": annotation.get("payload", {}).get("coverage_depth_png"),
                    "coverage_depth_csv": annotation.get("payload", {}).get("coverage_depth_csv"),
                    "coverage": coverage,
                },
                "artifacts_dir": input_data.routing_context.get("artifacts_dir"),
            },
            input_data.routing_context,
            progress,
        )
        if halted := _halted_response(input_data.run_id, "report_builder", report):
            return halted

        report_agent = await _run_stage(
            run_agent,
            "report_agent",
            {
                "payload": {
                    "qc": qc,
                    "bwa": bwa,
                    "gatk": gatk,
                    "annotation": annotation,
                    "coverage": coverage,
                    "report_builder": report,
                }
            },
            input_data.routing_context,
            progress,
        )
        if halted := _halted_response(input_data.run_id, "report_agent", report_agent):
            return halted

        outputs = {
            "qc_report_html": qc.get("payload", {}).get("report_html"),
            "bam_path": bwa.get("payload", {}).get("artifacts", {}).get("bam_path"),
            "bam_index": bwa.get("payload", {}).get("artifacts", {}).get("bam_index"),
            "flagstat": bwa.get("payload", {}).get("artifacts", {}).get("flagstat"),
            "coverage_depth_csv": bwa.get("payload", {}).get("artifacts", {}).get("coverage_depth_csv"),
            "coverage_depth_png": bwa.get("payload", {}).get("artifacts", {}).get("coverage_depth_png"),
            "final_bam": gatk.get("payload", {}).get("final_bam"),
            "variants_vcf": gatk.get("payload", {}).get("variants_vcf"),
            "annotated_vcf": annotation.get("payload", {}).get("annotated_vcf"),
            "variants_csv": annotation.get("payload", {}).get("variants_csv"),
            "report_html": report.get("payload", {}).get("report_html"),
            "coverage_check": coverage.get("payload", {}),
            "report_narrative": report_agent.get("payload", {}).get("narrative"),
        }

        return {
            "sample_id": input_data.sample_id,
            "status": "complete",
            "trim_was_run": trim_was_run,
            "agents": [
                "ingest",
                "qc",
                "ai_decider",
                "trim",
                "bwa_agent",
                "gatk_agent",
                "annotate_agent",
                "coverage_agent",
                "report_agent",
                "report_builder",
            ],
            "outputs": outputs,
            "ai_decision": ai_decision.get("payload", {}),
        }

    # --- RNA-Seq branch -------------------------------------------------
    max_align_attempts = 2
    current_align_input = align_input
    align: Dict[str, Any] | None = None
    alignment_failed = False

    for attempt in range(max_align_attempts):
        align = await _run_stage(run_agent, "align", current_align_input, input_data.routing_context, progress)

        if align.get("payload", {}).get("alignment_status") != "fail":
            break

        ai_eval = align.get("payload", {}).get("ai_evaluation")
        if ai_eval and ai_eval.get("action") == "re_trim" and attempt < max_align_attempts - 1:
            new_trim_params = ai_eval.get("new_trim_params", {})
            trim_request = {
                "payload": {
                    **qc.get("payload", {}),
                    "trim_params": new_trim_params,
                }
            }
            current_align_input = await _run_stage(
                run_agent, "trim", trim_request, input_data.routing_context, progress
            )
            trim_was_run = True
        else:
            alignment_failed = True
            break

    if alignment_failed:
        ai_eval = align.get("payload", {}).get("ai_evaluation", {}) if align else {}
        return {
            "sample_id": input_data.sample_id,
            "status": "failed_at_alignment",
            "trim_was_run": trim_was_run,
            "ai_eval": ai_eval,
            "outputs": {
                "qc_report_html": qc.get("payload", {}).get("report_html"),
                "mapping_rate": align.get("payload", {}).get("mapping_rate") if align else None,
            },
            "ai_decision": ai_decision.get("payload", {}),
        }

    count = await _run_stage(run_agent, "count", align, input_data.routing_context, progress)
    if halted := _halted_response(input_data.run_id, "count", count):
        return halted

    de = await _run_stage(
        run_agent,
        "de_agent",
        {**count, "sample_sheet": input_data.routing_context.get("sample_sheet")},
        input_data.routing_context,
        progress,
    )
    if halted := _halted_response(input_data.run_id, "de_agent", de):
        return halted

    insight = await _run_stage(
        run_agent,
        "insight_agent",
        {**de, "go_input": input_data.routing_context.get("go_input")},
        input_data.routing_context,
        progress,
    )
    if halted := _halted_response(input_data.run_id, "insight_agent", insight):
        return halted

    report = await _run_stage(
        run_agent,
        "report_builder",
        {
            "payload": {
                "qc": qc,
                "align": align,
                "count": count,
                "de": de,
                "insight": insight,
            },
            "artifacts_dir": input_data.routing_context.get("artifacts_dir"),
        },
        input_data.routing_context,
        progress,
    )
    if halted := _halted_response(input_data.run_id, "report_builder", report):
        return halted

    report_agent = await _run_stage(
        run_agent,
        "report_agent",
        {
            "payload": {
                "qc": qc,
                "align": align,
                "count": count,
                "de": de,
                "insight": insight,
                "report_builder": report,
            }
        },
        input_data.routing_context,
        progress,
    )
    if halted := _halted_response(input_data.run_id, "report_agent", report_agent):
        return halted

    outputs = {
        "qc_report_html": qc.get("payload", {}).get("report_html"),
        "mapping_rate": align.get("payload", {}).get("mapping_rate"),
        "bam_path": align.get("payload", {}).get("bam_path"),
        "bam_index": align.get("payload", {}).get("bam_index"),
        "count": count,
        "count_matrix": count.get("payload", {}).get("count_matrix"),
        "count_summary": count.get("payload", {}).get("count_summary"),
        "de_artifacts": de.get("payload", {}).get("artifacts", {}),
        "insight_summary": insight.get("payload", {}).get("ai_summary"),
        "report_html": report.get("payload", {}).get("report_html"),
        "report_narrative": report_agent.get("payload", {}).get("narrative"),
    }

    return {
        "sample_id": input_data.sample_id,
        "status": "complete",
        "trim_was_run": trim_was_run,
        "agents": [
            "ingest",
            "qc",
            "ai_decider",
            "trim",
            "align",
            "count",
            "de_agent",
            "insight_agent",
            "report_agent",
            "report_builder",
        ],
        "outputs": outputs,
        "ai_decision": ai_decision.get("payload", {}),
    }


async def run_pipeline(
    input_data: RunInput,
    run_agent: RunAgent,
    progress: Optional[ProgressFn] = None,
) -> Dict[str, Any]:
    """Run the whole pipeline. Mirrors ``NGSPipelineWorkflow.run``.

    Samples are processed sequentially on the local machine to avoid
    CPU/memory contention between concurrently running Docker agents; the
    Temporal path retains its parallel child-workflow fan-out.
    """
    is_dna = input_data.experiment_type in {"WGS", "WES"}
    samples = input_data.initial_inputs.get("samples")

    if not samples:
        samples = [
            {
                "sample_id": "sample-01",
                "condition": "unknown",
                "fastq_path": input_data.initial_inputs.get("fastq_path"),
                "fastq_r1": input_data.initial_inputs.get("fastq_r1"),
                "fastq_r2": input_data.initial_inputs.get("fastq_r2"),
            }
        ]

    if progress:
        progress(f"Pipeline {input_data.run_id}: {len(samples)} sample(s) — {input_data.experiment_type}")

    sample_results = []
    for sample in samples:
        sample_run_id = f"{input_data.run_id}-{sample['sample_id']}"
        sample_routing_ctx = {
            **input_data.routing_context,
            "sample_id": sample["sample_id"],
            "condition": sample.get("condition", "unknown"),
            "replicate_group": sample.get("replicate_group", ""),
            "species": sample.get("species", input_data.routing_context.get("organism")),
        }
        sample_input = SampleRunInput(
            run_id=sample_run_id,
            sample_id=sample["sample_id"],
            experiment_type=input_data.experiment_type,
            routing_context=sample_routing_ctx,
            initial_inputs=sample,
        )
        if progress:
            progress(f"▶ sample {sample['sample_id']}")
        sample_results.append(await run_sample(sample_input, run_agent, progress))

    de: Dict[str, Any] = {}
    insight: Dict[str, Any] = {}

    successful_rna_samples = [
        r for r in sample_results if r.get("status") == "complete" and not is_dna
    ]

    if not is_dna and successful_rna_samples:
        counts = [
            r["outputs"].get("count")
            for r in successful_rna_samples
            if r.get("outputs", {}).get("count")
        ]
        de_input = {"counts": counts, "samples": samples}
        de = await _run_stage(run_agent, "de_agent", de_input, input_data.routing_context, progress)

        insight_input = {**de, "go_input": input_data.routing_context.get("go_input")}
        insight = await _run_stage(run_agent, "insight_agent", insight_input, input_data.routing_context, progress)

    report_payload = {
        "samples": sample_results,
        "de": de,
        "insight": insight,
    }

    report = await _run_stage(
        run_agent,
        "report_builder",
        {
            "payload": report_payload,
            "artifacts_dir": input_data.routing_context.get("artifacts_dir"),
        },
        input_data.routing_context,
        progress,
    )

    report_agent = await _run_stage(
        run_agent,
        "report_agent",
        {
            "payload": {
                "samples": sample_results,
                "de": de,
                "insight": insight,
                "report_builder": report,
            }
        },
        input_data.routing_context,
        progress,
    )

    return {
        "run_id": input_data.run_id,
        "status": "complete",
        "samples_processed": len(samples),
        "sample_results": sample_results,
        "report_html": report.get("payload", {}).get("report_html"),
        "report_narrative": report_agent.get("payload", {}).get("narrative"),
    }
