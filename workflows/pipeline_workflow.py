import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict

from temporalio import workflow

from shared.models import AgentResult
from workflows.activities import (
    ai_decider_activity,
    annotate_activity,
    align_activity,
    bwa_activity,
    count_activity,
    coverage_activity,
    de_activity,
    gatk_activity,
    ingest_activity,
    insight_activity,
    qc_activity,
    report_activity,
    report_builder_activity,
    trim_activity,
)


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


def _halted_response(run_id: str, agent_name: str, raw_result: Dict[str, Any]) -> Dict[str, Any] | None:
    result = AgentResult.from_dict(raw_result)
    if result.halt:
        return {
            "run_id": run_id,
            "status": "halted",
            "reason": result.halt_reason or result.reasoning or f"{agent_name} requested halt",
            "agent": agent_name,
        }
    return None


@workflow.defn
class NGSSampleWorkflow:
    @workflow.run
    async def run(self, input_data: SampleRunInput) -> Dict[str, Any]:
        is_dna = input_data.experiment_type in {"WGS", "WES"}

        ingest = await workflow.execute_activity(
            ingest_activity,
            args=(input_data.initial_inputs, input_data.routing_context),
            start_to_close_timeout=timedelta(minutes=5),
        )
        if halted := _halted_response(input_data.run_id, "ingest", ingest):
            return halted

        qc = await workflow.execute_activity(
            qc_activity,
            args=(ingest, input_data.routing_context),
            start_to_close_timeout=timedelta(minutes=15),
        )
        if halted := _halted_response(input_data.run_id, "qc", qc):
            return halted

        ai_decision = await workflow.execute_activity(
            ai_decider_activity,
            args=(qc, input_data.routing_context),
            start_to_close_timeout=timedelta(minutes=5),
        )
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
            align_input = await workflow.execute_activity(
                trim_activity,
                args=(trim_request, input_data.routing_context),
                start_to_close_timeout=timedelta(minutes=30),
            )
            if halted := _halted_response(input_data.run_id, "trim", align_input):
                return halted
            trim_was_run = True
        else:
            align_input = ingest

        if is_dna:
            bwa = await workflow.execute_activity(
                bwa_activity,
                args=(align_input, input_data.routing_context),
                start_to_close_timeout=timedelta(hours=4),
            )
            if halted := _halted_response(input_data.run_id, "bwa_agent", bwa):
                return halted

            gatk = await workflow.execute_activity(
                gatk_activity,
                args=(bwa, input_data.routing_context),
                start_to_close_timeout=timedelta(hours=6),
            )
            if halted := _halted_response(input_data.run_id, "gatk_agent", gatk):
                return halted

            annotation = await workflow.execute_activity(
                annotate_activity,
                args=(
                    {
                        **gatk,
                        "panel_bed": input_data.routing_context.get("panel_bed"),
                    },
                    input_data.routing_context,
                ),
                start_to_close_timeout=timedelta(minutes=45),
            )
            if halted := _halted_response(input_data.run_id, "annotate_agent", annotation):
                return halted

            coverage = await workflow.execute_activity(
                coverage_activity,
                args=(
                    {
                        "payload": {
                            "coverage_depth_csv": annotation.get("payload", {}).get("coverage_depth_csv")
                            or bwa.get("payload", {}).get("artifacts", {}).get("coverage_depth_csv")
                        }
                    },
                    input_data.routing_context,
                ),
                start_to_close_timeout=timedelta(minutes=10),
            )
            if halted := _halted_response(input_data.run_id, "coverage_agent", coverage):
                return halted

            report = await workflow.execute_activity(
                report_builder_activity,
                args=(
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
                ),
                start_to_close_timeout=timedelta(minutes=10),
            )
            if halted := _halted_response(input_data.run_id, "report_builder", report):
                return halted

            report_agent = await workflow.execute_activity(
                report_activity,
                args=(
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
                ),
                start_to_close_timeout=timedelta(minutes=8),
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

        max_align_attempts = 2
        current_align_input = align_input
        align = None
        alignment_failed = False

        for attempt in range(max_align_attempts):
            align = await workflow.execute_activity(
                align_activity,
                args=(current_align_input, input_data.routing_context),
                start_to_close_timeout=timedelta(hours=2),
            )

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
                current_align_input = await workflow.execute_activity(
                    trim_activity,
                    args=(trim_request, input_data.routing_context),
                    start_to_close_timeout=timedelta(minutes=30),
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

        count = await workflow.execute_activity(
            count_activity,
            args=(align, input_data.routing_context),
            start_to_close_timeout=timedelta(minutes=30),
        )
        if halted := _halted_response(input_data.run_id, "count", count):
            return halted

        de = await workflow.execute_activity(
            de_activity,
            args=(
                {
                    **count,
                    "sample_sheet": input_data.routing_context.get("sample_sheet"),
                },
                input_data.routing_context,
            ),
            start_to_close_timeout=timedelta(minutes=25),
        )
        if halted := _halted_response(input_data.run_id, "de_agent", de):
            return halted

        insight = await workflow.execute_activity(
            insight_activity,
            args=(
                {
                    **de,
                    "go_input": input_data.routing_context.get("go_input"),
                },
                input_data.routing_context,
            ),
            start_to_close_timeout=timedelta(minutes=15),
        )
        if halted := _halted_response(input_data.run_id, "insight_agent", insight):
            return halted

        report = await workflow.execute_activity(
            report_builder_activity,
            args=(
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
            ),
            start_to_close_timeout=timedelta(minutes=10),
        )
        if halted := _halted_response(input_data.run_id, "report_builder", report):
            return halted

        report_agent = await workflow.execute_activity(
            report_activity,
            args=(
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
            ),
            start_to_close_timeout=timedelta(minutes=8),
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


@workflow.defn
class NGSPipelineWorkflow:
    @workflow.run
    async def run(self, input_data: RunInput) -> Dict[str, Any]:
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

        sample_futures = []
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

            future = workflow.execute_child_workflow(
                NGSSampleWorkflow.run,
                sample_input,
                id=f"ngs-sample-{sample_run_id}",
            )
            sample_futures.append(future)

        sample_results = await asyncio.gather(*sample_futures)

        de = {}
        insight = {}

        successful_rna_samples = [r for r in sample_results if r.get("status") == "complete" and not is_dna]

        if not is_dna and successful_rna_samples:
            counts = [r["outputs"].get("count") for r in successful_rna_samples if r.get("outputs", {}).get("count")]
            de_input = {
                "counts": counts,
                "samples": samples,
            }
            de = await workflow.execute_activity(
                de_activity,
                args=(de_input, input_data.routing_context),
                start_to_close_timeout=timedelta(minutes=25),
            )

            insight_input = {
                **de,
                "go_input": input_data.routing_context.get("go_input"),
            }
            insight = await workflow.execute_activity(
                insight_activity,
                args=(insight_input, input_data.routing_context),
                start_to_close_timeout=timedelta(minutes=15),
            )

        report_payload = {
            "samples": sample_results,
            "de": de,
            "insight": insight,
        }

        report = await workflow.execute_activity(
            report_builder_activity,
            args=(
                {
                    "payload": report_payload,
                    "artifacts_dir": input_data.routing_context.get("artifacts_dir"),
                },
                input_data.routing_context,
            ),
            start_to_close_timeout=timedelta(minutes=10),
        )

        report_agent = await workflow.execute_activity(
            report_activity,
            args=(
                {
                    "payload": {
                        "samples": sample_results,
                        "de": de,
                        "insight": insight,
                        "report_builder": report,
                    }
                },
                input_data.routing_context,
            ),
            start_to_close_timeout=timedelta(minutes=8),
        )

        return {
            "run_id": input_data.run_id,
            "status": "complete",
            "samples_processed": len(samples),
            "report_html": report.get("payload", {}).get("report_html"),
            "report_narrative": report_agent.get("payload", {}).get("narrative"),
        }
