from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from rich.console import Console

from ngs_agent.agentic.agent.models import ExperimentContext, Plan, PlanStep
from ngs_agent.agentic.config.settings import NGSSettings
from ngs_agent.agentic.tools.permissions import SafetyLevel

logger = logging.getLogger(__name__)

_FASTQ_PATTERNS = ("*.fastq", "*.fastq.gz", "*.fq", "*.fq.gz")
_READ1_RE = re.compile(r"(_R?1)(?=[._])")
_READ2_RE = re.compile(r"(_R?2)(?=[._])")
_HT2_INDEX_RE = re.compile(r"\.\d+\.ht2l?$")


def _fastq_stem(path: Path) -> str:
    """Strip fastq extensions the way FastQC does when naming its reports."""
    name = path.name
    for ext in (".fastq.gz", ".fq.gz", ".fastq", ".fq", ".gz"):
        if name.endswith(ext):
            return name[: -len(ext)]
    return path.stem


def _pair_reads(fastqs: list[Path]) -> tuple[Path | None, Path | None, Path | None]:
    """Return (r1, r2, single). Pairs by _R1/_R2 or _1/_2 naming conventions."""
    r1_candidates = [f for f in fastqs if _READ1_RE.search(f.name)]
    for r1 in r1_candidates:
        expected_r2 = _READ1_RE.sub(lambda m: m.group(1).replace("1", "2"), r1.name)
        for candidate in fastqs:
            if candidate.name == expected_r2:
                return r1, candidate, None
    if fastqs:
        return None, None, fastqs[0]
    return None, None, None


def _discover_hisat2_index(references: list[Path], working_directory: Path) -> str | None:
    ht2_files = sorted(working_directory.glob("**/*.ht2")) + sorted(working_directory.glob("**/*.ht2l"))
    for ht2 in ht2_files:
        basename = _HT2_INDEX_RE.sub("", str(ht2))
        if basename != str(ht2):
            return basename
    return None


class PlannerAgent:
    def __init__(self, settings: NGSSettings, console: Console) -> None:
        self.settings = settings
        self.console = console

    def discover_context(self, working_directory: Path, resume_token: str | None = None) -> ExperimentContext:
        samplesheets = sorted(
            {
                *working_directory.glob("*samplesheet*.csv"),
                *working_directory.glob("*samplesheet*.tsv"),
                *working_directory.glob("samples*.csv"),
                *working_directory.glob("samples*.tsv"),
            }
        )
        references = sorted(
            {
                *working_directory.glob("**/*.fa"),
                *working_directory.glob("**/*.fasta"),
                *working_directory.glob("**/*.gtf"),
                *working_directory.glob("**/*.gff"),
                *working_directory.glob("**/*.fai"),
            }
        )
        fastq_files: list[Path] = []
        for pattern in _FASTQ_PATTERNS:
            fastq_files.extend(working_directory.glob(pattern))
            fastq_files.extend(working_directory.glob(f"*/{pattern}"))
        fastq_files = sorted(set(fastq_files))
        prior_runs = sorted([*working_directory.glob("runs/*"), *working_directory.glob("output/*")])
        return ExperimentContext(
            working_directory=working_directory,
            samplesheets=samplesheets[:10],
            references=references[:10],
            fastq_files=fastq_files[:50],
            prior_runs=prior_runs[:10],
            resume_token=resume_token,
            metadata={
                "samplesheet_count": len(samplesheets),
                "reference_count": len(references),
                "fastq_count": len(fastq_files),
                "prior_run_count": len(prior_runs),
            },
        )

    def _heuristic_plan(self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool) -> Plan:
        is_rnaseq = workflow == "rnaseq" or "rna" in objective.lower()
        workflow_name = "RNA-Seq" if is_rnaseq else "variant"
        artifacts_root = Path(self.settings.artifacts_dir)
        run_id = context.resume_token or "run"

        fastqs = context.fastq_files
        r1, r2, single = _pair_reads(fastqs)
        paired_end = r1 is not None and r2 is not None
        sample_name = _fastq_stem(r1 if paired_end else single) if (paired_end or single) else "sample"
        sample_name = re.sub(r"_R?[12]$", "", sample_name)
        gtf = next((ref for ref in context.references if ref.suffix in {".gtf", ".gff"}), None)
        hisat2_index = _discover_hisat2_index(context.references, context.working_directory)

        steps: list[PlanStep] = [
            PlanStep(
                name="discover-context",
                description="Inspect the working directory for samplesheets, references, and existing checkpoints.",
                command_preview=f"scan {context.working_directory}",
                safety_level=SafetyLevel.READ,
                estimated_duration_minutes=1,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="discover-context",
                expected_outputs=[],
            )
        ]

        # --- QC step: bound to the FastQC tool when reads are available ---
        qc_dir = artifacts_root / "qc"
        if fastqs:
            qc_reads = [r1, r2] if paired_end else [single]
            steps.append(
                PlanStep(
                    name="qc",
                    description="Run FastQC quality control on the discovered reads.",
                    command_preview=f"fastqc --outdir {qc_dir} " + " ".join(str(r) for r in qc_reads),
                    safety_level=SafetyLevel.READ,
                    estimated_duration_minutes=10,
                    estimated_cost_label="low",
                    requires_confirmation=False,
                    checkpoint_key="qc",
                    dependencies=["discover-context"],
                    expected_outputs=[f"{_fastq_stem(read)}_fastqc.html" for read in qc_reads],
                    tool_name="fastqc",
                    tool_payload={
                        "run_id": run_id,
                        "reads": [str(read) for read in qc_reads],
                        "output_dir": str(qc_dir),
                        "sample_name": sample_name,
                        "paired_end": paired_end,
                    },
                )
            )
        else:
            steps.append(
                PlanStep(
                    name="qc",
                    description="Run FastQC quality control (skipped: no FASTQ files discovered).",
                    command_preview="fastqc <reads>",
                    safety_level=SafetyLevel.READ,
                    checkpoint_key="qc",
                    dependencies=["discover-context"],
                    skip_reason="No FASTQ files found in the working directory.",
                )
            )

        if is_rnaseq:
            steps.extend(
                self._rnaseq_steps(
                    context=context,
                    artifacts_root=artifacts_root,
                    run_id=run_id,
                    sample_name=sample_name,
                    r1=r1,
                    r2=r2,
                    single=single,
                    paired_end=paired_end,
                    gtf=gtf,
                    hisat2_index=hisat2_index,
                )
            )
        else:
            steps.append(
                PlanStep(
                    name="variant-call",
                    description="Call variants and annotate findings.",
                    command_preview="bwa-mem2 | gatk | snpEff",
                    safety_level=SafetyLevel.EXPENSIVE,
                    estimated_duration_minutes=120,
                    estimated_cost_label="high",
                    requires_confirmation=True,
                    checkpoint_key="variant-call",
                    dependencies=["qc"],
                    skip_reason=(
                        "Variant calling tools (bwa-mem2/gatk/snpEff) are not registered in the tool "
                        "registry yet. Use the RNA-Seq workflow or the container agents for variant runs."
                    ),
                )
            )

        steps.append(
            PlanStep(
                name="report",
                description="Render publication-ready HTML report and AI summary (generated by the reporter agent).",
                command_preview="report.html",
                safety_level=SafetyLevel.WRITE,
                estimated_duration_minutes=2,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="report",
                expected_outputs=[],
            )
        )

        estimated_duration = sum(step.estimated_duration_minutes for step in steps)
        risks = [
            "Expensive steps require confirmation unless explicitly suppressed by policy.",
        ]
        if not fastqs:
            risks.append("No FASTQ files were discovered; QC and downstream steps will be skipped.")
        if is_rnaseq and hisat2_index is None:
            risks.append("No HISAT2 index (*.ht2) found; alignment will be skipped until an index is provided.")
        if is_rnaseq and gtf is None:
            risks.append("No GTF/GFF annotation found; quantification will be skipped.")

        if context.samplesheets:
            next_actions = [f"Use discovered samplesheet: {context.samplesheets[0].name}"]
        else:
            next_actions = ["Provide a samplesheet or run in dry-run mode to inspect context."]

        return Plan(
            title=f"{workflow_name} pipeline plan",
            objective=objective,
            workflow=workflow,
            summary=f"{workflow_name} analysis plan prepared from the current experiment context.",
            estimated_duration_minutes=estimated_duration,
            estimated_cost_label="medium" if estimated_duration < 120 else "high",
            context=context,
            steps=steps,
            risks=risks,
            next_actions=next_actions,
            dry_run=dry_run,
        )

    def _rnaseq_steps(
        self,
        context: ExperimentContext,
        artifacts_root: Path,
        run_id: str,
        sample_name: str,
        r1: Path | None,
        r2: Path | None,
        single: Path | None,
        paired_end: bool,
        gtf: Path | None,
        hisat2_index: str | None,
    ) -> list[PlanStep]:
        steps: list[PlanStep] = []
        trim_dir = artifacts_root / "trim"
        align_dir = artifacts_root / "align"
        counts_dir = artifacts_root / "counts"

        # --- Trimming: bound to Trimmomatic with AI-assisted parameter selection ---
        if paired_end or single is not None:
            payload: dict[str, Any] = {
                "run_id": run_id,
                "paired_end": paired_end,
                "output_dir": str(trim_dir),
                "sample_name": sample_name,
            }
            if paired_end:
                payload["input_r1"] = str(r1)
                payload["input_r2"] = str(r2)
                trimmed_reads = [
                    trim_dir / f"{sample_name}_R1.trimmed.fastq.gz",
                    trim_dir / f"{sample_name}_R2.trimmed.fastq.gz",
                ]
            else:
                payload["input_single"] = str(single)
                trimmed_reads = [trim_dir / f"{sample_name}.trimmed.fastq.gz"]
            steps.append(
                PlanStep(
                    name="trim",
                    description="Trim adapters and low-quality bases with Trimmomatic (AI-assisted parameters).",
                    command_preview=f"trimmomatic {'PE' if paired_end else 'SE'} -> {trim_dir}",
                    safety_level=SafetyLevel.EXPENSIVE,
                    estimated_duration_minutes=15,
                    estimated_cost_label="medium",
                    requires_confirmation=True,
                    checkpoint_key="trim",
                    dependencies=["qc"],
                    expected_outputs=[read.name for read in trimmed_reads],
                    tool_name="trimmomatic",
                    tool_payload=payload,
                )
            )
            align_reads = trimmed_reads
        else:
            steps.append(
                PlanStep(
                    name="trim",
                    description="Trim reads with Trimmomatic (skipped: no FASTQ files discovered).",
                    command_preview="trimmomatic",
                    safety_level=SafetyLevel.EXPENSIVE,
                    checkpoint_key="trim",
                    dependencies=["qc"],
                    skip_reason="No FASTQ files found in the working directory.",
                )
            )
            align_reads = []

        # --- Alignment: bound to HISAT2 + samtools sort/index ---
        output_bam = align_dir / f"{sample_name}.bam"
        if hisat2_index and align_reads:
            steps.append(
                PlanStep(
                    name="align",
                    description="Align reads against the reference genome with HISAT2, then sort and index.",
                    command_preview=f"hisat2 -x {hisat2_index} | samtools sort -o {output_bam}",
                    safety_level=SafetyLevel.EXPENSIVE,
                    estimated_duration_minutes=60,
                    estimated_cost_label="medium",
                    requires_confirmation=True,
                    checkpoint_key="align",
                    dependencies=["trim"],
                    expected_outputs=[output_bam.name, output_bam.name + ".bai"],
                    tool_name="hisat2",
                    tool_payload={
                        "run_id": run_id,
                        "index_basename": hisat2_index,
                        "reads": [str(read) for read in align_reads],
                        "output_bam": str(output_bam),
                        "sample_name": sample_name,
                    },
                )
            )
        else:
            reason = (
                "No HISAT2 index (*.ht2) found in the working directory."
                if not hisat2_index
                else "No reads available for alignment."
            )
            steps.append(
                PlanStep(
                    name="align",
                    description=f"Align reads with HISAT2 (skipped: {reason.lower()})",
                    command_preview="hisat2 | samtools sort",
                    safety_level=SafetyLevel.EXPENSIVE,
                    checkpoint_key="align",
                    dependencies=["trim"],
                    skip_reason=reason,
                )
            )

        # --- Quantification: bound to featureCounts ---
        count_matrix = counts_dir / f"{sample_name}.counts.txt"
        if gtf is not None:
            steps.append(
                PlanStep(
                    name="quantify",
                    description="Generate gene-level counts for downstream differential expression.",
                    command_preview=f"featureCounts -a {gtf} -o {count_matrix}",
                    safety_level=SafetyLevel.EXPENSIVE,
                    estimated_duration_minutes=20,
                    estimated_cost_label="medium",
                    requires_confirmation=True,
                    checkpoint_key="quantify",
                    dependencies=["align"],
                    expected_outputs=[count_matrix.name],
                    tool_name="featureCounts",
                    tool_payload={
                        "run_id": run_id,
                        "bam_paths": [str(output_bam)],
                        "gtf": str(gtf),
                        "output_dir": str(counts_dir),
                        "sample_name": sample_name,
                    },
                )
            )
        else:
            steps.append(
                PlanStep(
                    name="quantify",
                    description="Quantify reads with featureCounts (skipped: no GTF/GFF annotation found).",
                    command_preview="featureCounts",
                    safety_level=SafetyLevel.EXPENSIVE,
                    checkpoint_key="quantify",
                    dependencies=["align"],
                    skip_reason="No GTF/GFF annotation found in the working directory.",
                )
            )

        # --- Aggregate QC: bound to MultiQC ---
        steps.append(
            PlanStep(
                name="multiqc",
                description="Aggregate QC and pipeline outputs into a MultiQC report.",
                command_preview=f"multiqc -o {artifacts_root / 'multiqc'} {artifacts_root}",
                safety_level=SafetyLevel.WRITE,
                estimated_duration_minutes=3,
                estimated_cost_label="low",
                requires_confirmation=False,
                checkpoint_key="multiqc",
                dependencies=["qc"],
                expected_outputs=["multiqc_report.html"],
                tool_name="multiqc",
                tool_payload={
                    "run_id": run_id,
                    "input_paths": [str(artifacts_root)],
                    "output_dir": str(artifacts_root / "multiqc"),
                },
            )
        )

        # --- Differential expression: bound to DESeq2 when a samplesheet exists ---
        de_dir = artifacts_root / "deseq2"
        if context.samplesheets and gtf is not None:
            steps.append(
                PlanStep(
                    name="differential-expression",
                    description="Run DESeq2 differential expression analysis and generate plots.",
                    command_preview=f"Rscript deseq2_analysis.R {count_matrix} {context.samplesheets[0]} {de_dir}",
                    safety_level=SafetyLevel.EXPENSIVE,
                    estimated_duration_minutes=10,
                    estimated_cost_label="medium",
                    requires_confirmation=True,
                    checkpoint_key="differential-expression",
                    dependencies=["quantify"],
                    expected_outputs=["deseq2_results.csv"],
                    tool_name="deseq2",
                    tool_payload={
                        "run_id": run_id,
                        "count_matrix": str(count_matrix),
                        "sample_sheet": str(context.samplesheets[0]),
                        "output_dir": str(de_dir),
                    },
                )
            )
            steps.append(
                PlanStep(
                    name="go-enrichment",
                    description="Run GO enrichment analysis on DESeq2 results.",
                    command_preview=f"Rscript enrichment_analysis.R {de_dir / 'deseq2_results.csv'}",
                    safety_level=SafetyLevel.WRITE,
                    estimated_duration_minutes=5,
                    estimated_cost_label="low",
                    requires_confirmation=False,
                    checkpoint_key="go-enrichment",
                    dependencies=["differential-expression"],
                    expected_outputs=["go_enrichment.csv"],
                    tool_name="go_enrichment",
                    tool_payload={
                        "run_id": run_id,
                        "input_csv": str(de_dir / "deseq2_results.csv"),
                        "organism": "human",
                        "output_dir": str(artifacts_root / "go"),
                    },
                )
            )
        else:
            steps.append(
                PlanStep(
                    name="differential-expression",
                    description="Run DESeq2 (skipped: requires a samplesheet with sample_id/condition columns).",
                    command_preview="Rscript deseq2_analysis.R",
                    safety_level=SafetyLevel.EXPENSIVE,
                    checkpoint_key="differential-expression",
                    dependencies=["quantify"],
                    skip_reason="No samplesheet discovered; DESeq2 needs sample_id and condition columns.",
                )
            )

        return steps

    def _anthropic_plan(self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool) -> Plan | None:
        if not self.settings.anthropic_api_key:
            return None

        # Lazy import: anthropic lives in the optional [llm] extra. Core installs must
        # still be able to import this module, so never import it at module scope.
        try:
            from anthropic import Anthropic
        except ImportError:
            logger.info(
                "ANTHROPIC_API_KEY is set but the 'anthropic' package is not installed. "
                "Install with `pip install ngs-agent[llm]` to enable AI planning; using heuristic planner."
            )
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
                            "You are an expert NGS workflow planner. Produce a concise JSON plan with steps, "
                            "duration estimates, risks, next_actions, and safety flags. "
                            "Use the current experiment context only. "
                            f"Objective: {objective}\n"
                            f"Workflow: {workflow}\n"
                            f"Context: {context.model_dump(mode='json')}\n"
                            f"Dry run: {dry_run}\n"
                            "Return only valid JSON."
                        ),
                    }
                ],
            )
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
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
            )
        except json.JSONDecodeError as exc:
            logger.warning(f"AI planner returned invalid JSON: {exc}")
            return None
        except Exception as exc:
            logger.warning(f"AI planning failed with exception: {exc}", exc_info=True)
            return None

    def create_plan(self, objective: str, workflow: str, context: ExperimentContext, dry_run: bool = False) -> Plan:
        plan = self._anthropic_plan(objective, workflow, context, dry_run)
        if plan is not None:
            return plan
        return self._heuristic_plan(objective, workflow, context, dry_run)
