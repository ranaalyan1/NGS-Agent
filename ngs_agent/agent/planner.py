from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from rich.console import Console

from ngs_agent.agent.models import ExperimentContext, Plan, PlanStep
from ngs_agent.bioinformatics.common import fastqc_stem
from ngs_agent.bioinformatics.samplesheet import SampleRecord, parse_samplesheet
from ngs_agent.bioinformatics.workflows import WorkflowInference, get_workflow, infer_workflow
from ngs_agent.config.settings import NGSSettings
from ngs_agent.environment import platform_warnings, verify_tool_environment
from ngs_agent.tools.permissions import SafetyLevel

logger = logging.getLogger(__name__)


class PlannerAgent:
    def __init__(self, settings: NGSSettings, console: Console) -> None:
        self.settings = settings
        self.console = console

    # ------------------------------------------------------------------
    # Context discovery
    # ------------------------------------------------------------------
    def discover_context(
        self, working_directory: Path, resume_token: str | None = None
    ) -> ExperimentContext:
        samplesheets = sorted(
            {
                *working_directory.glob("*samplesheet*.csv"),
                *working_directory.glob("*samplesheet*.tsv"),
                *working_directory.glob("samples*.csv"),
                *working_directory.glob("samples*.tsv"),
                # Also look one level down (data/samplesheet.csv is the
                # nf-core convention) but skip vendored/tool directories.
                *working_directory.glob("*/*samplesheet*.csv"),
                *working_directory.glob("*/*samplesheet*.tsv"),
                *working_directory.glob("*/samples*.csv"),
                *working_directory.glob("*/samples*.tsv"),
            }
        )
        references = sorted(
            [
                *working_directory.glob("**/*.fa"),
                *working_directory.glob("**/*.fasta"),
                *working_directory.glob("**/*.gtf"),
                *working_directory.glob("**/*.gff"),
                *working_directory.glob("**/*.fai"),
                *working_directory.glob("**/*.1.ht2"),
                *working_directory.glob("**/*.ht2l"),
            ]
        )
        prior_runs = sorted(
            [*working_directory.glob("runs/*"), *working_directory.glob("output/*")]
        )

        samples: list[dict[str, Any]] = []
        primary_samplesheet: Path | None = None
        if samplesheets:
            primary_samplesheet = samplesheets[0]
            try:
                records = parse_samplesheet(primary_samplesheet, validate_files=False)
                samples = [_record_to_dict(record) for record in records]
            except Exception as exc:  # noqa: BLE001 - planning must survive a bad samplesheet
                logger.warning("Failed to parse samplesheet %s: %s", primary_samplesheet, exc)

        return ExperimentContext(
            working_directory=working_directory,
            samplesheets=samplesheets[:10],
            references=references[:10],
            prior_runs=prior_runs[:10],
            resume_token=resume_token,
            samples=samples,
            metadata={
                "samplesheet_count": len(samplesheets),
                "reference_count": len(references),
                "prior_run_count": len(prior_runs),
                "sample_count": len(samples),
                "primary_samplesheet": str(primary_samplesheet) if primary_samplesheet else "",
            },
        )

    # ------------------------------------------------------------------
    # Environment checks surfaced as plan risks
    # ------------------------------------------------------------------
    def _environment_risks(self, workflow_key: str) -> tuple[list[str], list[str]]:
        """Return (risks, next_actions) from tool-env verification and platform checks."""
        risks: list[str] = []
        next_actions: list[str] = []

        try:
            spec = get_workflow(workflow_key)
            tools = spec.required_tools
        except ValueError:
            tools = ()

        report = verify_tool_environment(tools)
        for conflict in report.conflicts:
            risks.append(
                f"Tool '{conflict.name}' resolves to {conflict.path}, outside the expected "
                f"environment prefix {report.expected_prefix}; a different version may run  than"
                    f"the one pinned in environment.yml."
            )
            next_actions.append(
                f"Activate the project environment (or set NGS_TOOL_PREFIX) so "
                f"'{conflict.name}' resolves from the managed prefix."
            )
        missing = report.missing
        if missing:
            names = ", ".join(check.name for check in missing)
            risks.append(f"Missing tools for this workflow: {names}.")
            next_actions.append("Install tools with `mamba env create -f environment.yml`.")

        for warning in platform_warnings():
            risks.append(warning)
        return risks, next_actions

    # ------------------------------------------------------------------
    # Heuristic planning
    # ------------------------------------------------------------------
    def _heuristic_plan(
        self,
        objective: str,
        workflow_key: str,
        context: ExperimentContext,
        dry_run: bool,
    ) -> Plan:
        try:
            workflow = get_workflow(workflow_key)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        if workflow.key == "rnaseq":
            steps = self._rnaseq_steps(context)
        elif workflow.key == "qc":
            steps = self._qc_steps(context)
        else:
            steps = self._variant_steps(context)

        estimated_duration = sum(step.estimated_duration_minutes for step in steps)
        env_risks, env_actions = self._environment_risks(workflow.key)
        risks = [*env_risks]
        if workflow.key == "rnaseq" and self._discover_hisat2_index(context) is None:
            risks.append(
                "No prebuilt HISAT2 index found; the plan includes an index-build step (or will"
                    "stop before alignment if no FASTA is available)."
            )
        risks.append("Expensive steps require confirmation unless explicitly suppressed by policy.")
        if not context.samples:
            risks.append(
                "No samplesheet discovered; the plan uses generic single-sample steps.  Provide"
                    "a samplesheet (sample,fastq_1,fastq_2) for per-sample parallel execution."
            )

        next_actions = list(env_actions)
        if context.samplesheets and context.samples:
            next_actions.insert(
                0,
                f"Use discovered samplesheet: {context.samplesheets[0].name}"
                    f"({len(context.samples)} samples)",
            )
        elif not context.samples:
            next_actions.append("Provide a samplesheet or run in dry-run mode to inspect context.")

        return Plan(
            title=f"{workflow.label} pipeline plan",
            objective=objective,
            workflow=workflow.key,
            summary=f"{workflow.label} analysis plan prepared from the current experiment"
                f"context. {workflow.description}",
            estimated_duration_minutes=estimated_duration,
            estimated_cost_label="medium" if estimated_duration < 120 else "high",
            context=context,
            steps=steps,
            risks=risks,
            next_actions=next_actions,
            dry_run=dry_run,
            max_parallel_steps=self.settings.max_parallel_steps,
        )

    # ------------------------------------------------------------------
    # RNA-Seq: per-sample parallel DAG (qc -> trim -> align -> sort per
    # sample, quantify fans in all BAMs, then aggregate + report)
    # ------------------------------------------------------------------
    def _rnaseq_steps(self, context: ExperimentContext) -> list[PlanStep]:
        results_root = context.working_directory / "results"
        gtf = next((ref for ref in context.references if ref.suffix in {".gtf", ".gff"}), None)
        index_basename = self._discover_hisat2_index(context)
        strandness = self._majority_strandness(context)

        steps: list[PlanStep] = [
            PlanStep(
                name="discover-context",
                description="Inspect the working directory for samplesheets, references, and"
                    "existing checkpoints.",
                command_preview=f"scan {context.working_directory}",
                safety_level=SafetyLevel.READ,
                estimated_duration_minutes=1,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="discover-context",
            )
        ]

        if index_basename is None:
            fasta = next(
                (ref for ref in context.references if ref.suffix in {".fa", ".fasta"}), None
            )
            if fasta is not None:
                steps.append(
                    PlanStep(
                        name="build-index",
                        description=f"Build HISAT2 index from {fasta.name} (no prebuilt index"
                            f"found).",
                        command_preview=f"hisat2-build {fasta.name} genome_index",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=45,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key="build-index",
                        dependencies=["discover-context"],
                        expected_outputs=["genome_index.1.ht2"],
                        tool_name="hisat2-build",
                        tool_payload={
                            "reference_fasta": str(fasta),
                            "index_basename": str(fasta.with_suffix("")),
                        },
                    )
                )
                index_basename = str(fasta.with_suffix(""))
            else:
                index_basename = "<genome_index>"
        index_dependency = (
            "build-index"
            if any(step.name == "build-index" for step in steps)
            else "discover-context"
        )

        if context.samples:
            records = [SampleRecord(**sample) for sample in context.samples]
            for record in records:
                sample_root = results_root / record.sample
                qc_dir = sample_root / "qc"
                trim_dir = sample_root / "trimmed"
                reads = [str(record.fastq_r1)] + ([str(record.fastq_r2)] if record.fastq_r2 else [])

                steps.append(
                    PlanStep(
                        name=f"qc-{record.sample}",
                        description=f"FastQC quality control for sample {record.sample}.",
                        command_preview=f"fastqc --outdir {qc_dir} <reads>",
                        safety_level=SafetyLevel.READ,
                        estimated_duration_minutes=3,
                        estimated_cost_label="low",
                        requires_confirmation=False,
                        checkpoint_key=f"qc-{record.sample}",
                        dependencies=["discover-context"],
                        expected_outputs=[f"{fastqc_stem(record.fastq_r1)}_fastqc.zip"],
                        tool_name="fastqc",
                        tool_payload={
                            "reads": reads,
                            "output_dir": str(qc_dir),
                            "sample_name": record.sample,
                        },
                    )
                )

                summary_path = qc_dir / f"{fastqc_stem(record.fastq_r1)}_fastqc.summary.txt"
                steps.append(
                    PlanStep(
                        name=f"trim-{record.sample}",
                        description=f"Trim reads for sample {record.sample} using the"
                            f"QC-informed policy.",
                        command_preview=(
                            f"trimmomatic {'PE' if record.paired_end else 'SE'} -> {trim_dir}"
                        ),
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=10,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key=f"trim-{record.sample}",
                        dependencies=[f"qc-{record.sample}"],
                        expected_outputs=[f"{record.sample}_R1.trimmed.fastq.gz"],
                        tool_name="trimmomatic",
                        tool_payload={
                            "paired_end": record.paired_end,
                            "input_r1": str(record.fastq_r1),
                            "input_r2": str(record.fastq_r2) if record.fastq_r2 else None,
                            "input_single": None if record.fastq_r2 else str(record.fastq_r1),
                            "output_dir": str(trim_dir),
                            "sample_name": record.sample,
                            "fastqc_summary_path": str(summary_path),
                        },
                    )
                )

                bam = sample_root / "aligned.bam"
                sam = sample_root / "aligned.sam"
                trimmed_r1 = trim_dir / f"{record.sample}_R1.trimmed.fastq.gz"
                trimmed_r2 = (
                    trim_dir / f"{record.sample}_R2.trimmed.fastq.gz" if record.paired_end else None
                )
                align_reads = (
                    [str(trimmed_r1), str(trimmed_r2)]
                    if record.paired_end
                    else [str(trim_dir / f"{record.sample}.trimmed.fastq.gz")]
                )
                steps.append(
                    PlanStep(
                        name=f"align-{record.sample}",
                        description=(
                            f"Splice-aware HISAT2 alignment for sample {record.sample} "
                            + (
                                "with known splice sites from the GTF."
                                if gtf
                                else "(no GTF found: splice sites unavailable)."
                            )
                        ),
                        command_preview=f"hisat2 -x {Path(index_basename).name}"
                            f"--known-splicesites-infile splicesites.txt | samtools view",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=30,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key=f"align-{record.sample}",
                        dependencies=[f"trim-{record.sample}", index_dependency],
                        expected_outputs=[sam.name],
                        tool_name="hisat2",
                        tool_payload={
                            "index_basename": index_basename,
                            "reads": align_reads,
                            "output_bam": str(bam),
                            "gtf": str(gtf) if gtf else None,
                            "rna_strandness": record.strandedness or strandness,
                            "threads": self.settings.default_threads,
                        },
                    )
                )

                sorted_bam = sample_root / "aligned.sorted.bam"
                steps.append(
                    PlanStep(
                        name=f"sort-{record.sample}",
                        description=f"Sort and index the alignment for sample {record.sample}.",
                        command_preview=f"samtools sort -o {sorted_bam.name} && samtools index",
                        safety_level=SafetyLevel.WRITE,
                        estimated_duration_minutes=5,
                        estimated_cost_label="low",
                        requires_confirmation=False,
                        checkpoint_key=f"sort-{record.sample}",
                        dependencies=[f"align-{record.sample}"],
                        expected_outputs=[sorted_bam.name, sorted_bam.name + ".bai"],
                        tool_name="samtools",
                        tool_payload={
                            "action": "sort",
                            "input_path": str(bam),
                            "output_path": str(sorted_bam),
                            "threads": self.settings.default_threads,
                        },
                    )
                )
                steps.append(
                    PlanStep(
                        name=f"index-{record.sample}",
                        description=f"Index the sorted alignment for sample {record.sample}.",
                        command_preview=f"samtools index {sorted_bam.name}",
                        safety_level=SafetyLevel.WRITE,
                        estimated_duration_minutes=1,
                        estimated_cost_label="low",
                        requires_confirmation=False,
                        checkpoint_key=f"index-{record.sample}",
                        dependencies=[f"sort-{record.sample}"],
                        expected_outputs=[sorted_bam.name + ".bai"],
                        tool_name="samtools",
                        tool_payload={"action": "index", "input_path": str(sorted_bam)},
                    )
                )

            sorted_bams = sorted(results_root.glob("*/aligned.sorted.bam")) or [
                results_root / record.sample / "aligned.sorted.bam" for record in records
            ]
            count_dir = results_root / "counts"
            steps.append(
                PlanStep(
                    name="quantify",
                    description=(
                        "featureCounts gene-level quantification across all samples "
                        f"(strandness: {strandness}; MAPQ >= 10; multi-mappers excluded)."
                    ),
                    command_preview=(
                        f"featureCounts -s {strandness_code(strandness)} -q 10 "
                        f"-a {gtf.name if gtf else '<gtf>'}"
                    ),
                    safety_level=SafetyLevel.EXPENSIVE,
                    estimated_duration_minutes=15,
                    estimated_cost_label="medium",
                    requires_confirmation=True,
                    checkpoint_key="quantify",
                    dependencies=[f"sort-{record.sample}" for record in records],
                    expected_outputs=["counts.txt", "counts.txt.summary"],
                    tool_name="featureCounts",
                    tool_payload={
                        "bam_paths": [str(bam) for bam in sorted_bams],
                        "gtf": str(gtf) if gtf else "",
                        "output_dir": str(count_dir),
                        "strandness": strandness,
                        "min_mapq": 10,
                        "paired_end": all(record.paired_end for record in records),
                        "threads": self.settings.default_threads,
                    },
                )
            )
            qc_inputs = [str(qc_dir) for qc_dir in sorted(results_root.glob("*/qc"))]
            steps.append(
                PlanStep(
                    name="aggregate-qc",
                    description="Aggregate all QC and pipeline outputs with MultiQC.",
                    command_preview="multiqc results/ -o results/multiqc",
                    safety_level=SafetyLevel.WRITE,
                    estimated_duration_minutes=5,
                    estimated_cost_label="low",
                    requires_confirmation=False,
                    checkpoint_key="aggregate-qc",
                    dependencies=[f"qc-{record.sample}" for record in records] + ["quantify"],
                    expected_outputs=["multiqc_report.html"],
                    tool_name="multiqc",
                    tool_payload={
                        "input_paths": qc_inputs or [str(results_root)],
                        "output_dir": str(results_root / "multiqc"),
                    },
                )
            )
        else:
            # No samplesheet: emit the generic single-sample stages so the
            # plan is still inspectable in dry-run mode.
            steps.extend(
                [
                    PlanStep(
                        name="qc-decision",
                        description="Run QC and decide the trimming policy.",
                        command_preview="fastqc -> AI decision -> trimming policy",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=15,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key="qc-decision",
                        dependencies=["discover-context"],
                        expected_outputs=["qc_metrics.json", "trim_decision.json"],
                    ),
                    PlanStep(
                        name="align",
                        description="Align reads against the selected reference genome.",
                        command_preview="hisat2 | samtools sort",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=60,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key="align",
                        dependencies=["qc-decision"],
                        expected_outputs=["aligned.bam", "aligned.bam.bai"],
                    ),
                    PlanStep(
                        name="quantify",
                        description="Generate gene-level counts for downstream differential"
                            "expression.",
                        command_preview="featureCounts",
                        safety_level=SafetyLevel.EXPENSIVE,
                        estimated_duration_minutes=20,
                        estimated_cost_label="medium",
                        requires_confirmation=True,
                        checkpoint_key="quantify",
                        dependencies=["align"],
                        expected_outputs=["counts.tsv", "summary.txt"],
                    ),
                ]
            )

        steps.append(
            PlanStep(
                name="report",
                description="Render publication-ready HTML report, provenance manifest, and AI"
                    "summary.",
                command_preview="report.html + manifest.jsonl",
                safety_level=SafetyLevel.WRITE,
                estimated_duration_minutes=5,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="report",
                dependencies=(
                    [f"index-{r.sample}" for r in records] if context.samples else ["quantify"]
                ),
                expected_outputs=["manifest.jsonl"],
            )
        )
        return steps

    def _qc_steps(self, context: ExperimentContext) -> list[PlanStep]:
        results_root = context.working_directory / "results"
        steps: list[PlanStep] = [
            PlanStep(
                name="discover-context",
                description="Inspect the working directory for samplesheets and FASTQ inputs.",
                command_preview=f"scan {context.working_directory}",
                safety_level=SafetyLevel.READ,
                estimated_duration_minutes=1,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="discover-context",
                expected_outputs=["context.json"],
            )
        ]
        if context.samples:
            for record in (SampleRecord(**sample) for sample in context.samples):
                qc_dir = results_root / record.sample / "qc"
                reads = [str(record.fastq_r1)] + ([str(record.fastq_r2)] if record.fastq_r2 else [])
                steps.append(
                    PlanStep(
                        name=f"qc-{record.sample}",
                        description=f"FastQC quality control for sample {record.sample}.",
                        command_preview=f"fastqc --outdir {qc_dir} <reads>",
                        safety_level=SafetyLevel.READ,
                        estimated_duration_minutes=3,
                        estimated_cost_label="low",
                        requires_confirmation=False,
                        checkpoint_key=f"qc-{record.sample}",
                        dependencies=["discover-context"],
                        expected_outputs=[f"{fastqc_stem(record.fastq_r1)}_fastqc.zip"],
                        tool_name="fastqc",
                        tool_payload={
                            "reads": reads,
                            "output_dir": str(qc_dir),
                            "sample_name": record.sample,
                        },
                    )
                )
            steps.append(
                PlanStep(
                    name="aggregate-qc",
                    description="Aggregate QC outputs with MultiQC.",
                    command_preview="multiqc results/ -o results/multiqc",
                    safety_level=SafetyLevel.WRITE,
                    estimated_duration_minutes=3,
                    estimated_cost_label="low",
                    requires_confirmation=False,
                    checkpoint_key="aggregate-qc",
                    dependencies=[
                        f"qc-{record.sample}"
                        for record in (SampleRecord(**s) for s in context.samples)
                    ],
                    expected_outputs=["multiqc_report.html"],
                    tool_name="multiqc",
                    tool_payload={
                        "input_paths": [
                            str(results_root / r.sample / "qc")
                            for r in (SampleRecord(**s) for s in context.samples)
                        ],
                        "output_dir": str(results_root / "multiqc"),
                    },
                )
            )
        return steps

    def _variant_steps(self, context: ExperimentContext) -> list[PlanStep]:
        dependencies = ["discover-context"]
        steps: list[PlanStep] = [
            PlanStep(
                name="discover-context",
                description="Inspect the working directory for samplesheets, references, and"
                    "existing checkpoints.",
                command_preview=f"scan {context.working_directory}",
                safety_level=SafetyLevel.READ,
                estimated_duration_minutes=1,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="discover-context",
                expected_outputs=["context.json"],
            ),
            PlanStep(
                name="variant-call",
                description="Call variants and annotate findings (bwa-mem2 | gatk | snpEff).",
                command_preview="bwa-mem2 | gatk | snpEff",
                safety_level=SafetyLevel.EXPENSIVE,
                estimated_duration_minutes=120,
                estimated_cost_label="high",
                requires_confirmation=True,
                checkpoint_key="variant-call",
                dependencies=dependencies,
                expected_outputs=["variants.vcf", "annotated.vcf"],
            ),
            PlanStep(
                name="report",
                description="Generate final variant report and executive summary.",
                command_preview="report.html",
                safety_level=SafetyLevel.WRITE,
                estimated_duration_minutes=8,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="report",
                dependencies=["variant-call"],
                expected_outputs=["manifest.jsonl"],
            ),
        ]
        return steps

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _discover_hisat2_index(context: ExperimentContext) -> str | None:
        for reference in context.references:
            if reference.name.endswith(".1.ht2"):
                return str(reference.with_name(reference.name[: -len(".1.ht2")]))
        return None

    @staticmethod
    def _majority_strandness(context: ExperimentContext) -> str:
        values = [
            str(sample.get("strandedness") or "unstranded").lower() for sample in context.samples
        ]
        values = [value for value in values if value]
        if not values:
            return "unstranded"
        return max(set(values), key=values.count)

    # ------------------------------------------------------------------
    # AI-assisted planning (optional; falls back to heuristics)
    # ------------------------------------------------------------------
    def _anthropic_plan(
        self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool
    ) -> Plan | None:
        if not self.settings.anthropic_api_key:
            return None
        try:
            from anthropic import Anthropic
        except ImportError:
            logger.debug("anthropic SDK not installed; using heuristic planner")
            return None

        try:
            client = Anthropic(api_key=self.settings.anthropic_api_key)
            response = client.messages.create(
                model=self.settings.anthropic_model,
                max_tokens=800,
                temperature=0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "You are an expert NGS workflow planner. Produce a concise JSON plan"
                                "with steps, duration estimates, risks, next_actions, and safety "
                                "flags. "
                            "Use the current experiment context only. "
                            f"Objective: {objective}\n"
                            f"Workflow: {workflow}\n"
                            f"Context: {context.model_dump(mode='json')}\n"
                            f"Dry run: {dry_run}\n Return only valid JSON."
                        ),
                    }
                ],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            parsed: dict[str, Any] = json.loads(text)
            steps = [PlanStep.model_validate(step) for step in parsed.get("steps", [])]
            return Plan(
                title=parsed.get("title", f"{workflow} pipeline plan"),
                objective=objective,
                workflow=workflow,
                summary=parsed.get("summary", "AI-generated execution plan."),
                estimated_duration_minutes=int(parsed.get("estimated_duration_minutes", 0)),
                estimated_cost_label=str(parsed.get("estimated_cost_label", "medium")),
                context=context,
                steps=steps,
                risks=list(parsed.get("risks", [])),
                next_actions=list(parsed.get("next_actions", [])),
                dry_run=dry_run,
                max_parallel_steps=self.settings.max_parallel_steps,
            )
        except json.JSONDecodeError as exc:
            logger.warning("AI planner returned invalid JSON: %s", exc)
            return None
        except Exception as exc:
            logger.warning("AI planning failed with exception: %s", exc, exc_info=True)
            return None

    def create_plan(
        self,
        objective: str,
        workflow: str,
        context: ExperimentContext,
        dry_run: bool = False,
    ) -> Plan:
        # Validate the workflow against the catalogue before planning so a
        # typo fails loudly instead of silently becoming a variant run.
        get_workflow(workflow)
        plan = self._anthropic_plan(objective, workflow, context, dry_run)
        if plan is not None and plan.steps:
            # Heuristic environment risks still apply to AI plans.
            env_risks, env_actions = self._environment_risks(plan.workflow)
            plan.risks = list(dict.fromkeys([*plan.risks, *env_risks]))
            plan.next_actions = list(dict.fromkeys([*plan.next_actions, *env_actions]))
            return plan
        return self._heuristic_plan(objective, workflow, context, dry_run)


def strandness_code(strandness: str) -> int:
    """Map a strandness label to the featureCounts ``-s`` code."""
    return {"unstranded": 0, "forward": 1, "reverse": 2}.get(strandness.lower(), 0)


def _record_to_dict(record: SampleRecord) -> dict[str, Any]:
    return {
        "sample": record.sample,
        "fastq_r1": str(record.fastq_r1),
        "fastq_r2": str(record.fastq_r2) if record.fastq_r2 else None,
        "strandedness": record.strandedness,
        "condition": record.condition,
    }


__all__ = [
    "PlannerAgent",
    "WorkflowInference",
    "infer_workflow",
    "strandness_code",
]
