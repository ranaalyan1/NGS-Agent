from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from anthropic import Anthropic
from pydantic import BaseModel, Field
from rich.console import Console

from ngs_agent.execution.models import CommandSpec
from ngs_agent.tools.base import Tool, ToolContext
from ngs_agent.tools.permissions import SafetyLevel


class ToolExecutionError(RuntimeError):
    pass


class BioToolInput(BaseModel):
    run_id: str = "unknown"
    working_directory: Path | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class BioToolOutput(BaseModel):
    tool_name: str
    status: str
    command: list[str]
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    artifacts: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


class FastQCInput(BaseModel):
    run_id: str = "unknown"
    reads: list[Path]
    output_dir: Path
    sample_name: str | None = None
    paired_end: bool = False


class FastQCOutput(BaseModel):
    tool_name: str = "fastqc"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    html_reports: list[str] = Field(default_factory=list)
    zip_reports: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class TrimmomaticInput(BaseModel):
    run_id: str = "unknown"
    paired_end: bool = True
    fastqc_summary: str = ""
    trim_params: dict[str, Any] | None = None
    input_r1: Path | None = None
    input_r2: Path | None = None
    input_single: Path | None = None
    output_dir: Path
    sample_name: str | None = None


class TrimmomaticOutput(BaseModel):
    tool_name: str = "trimmomatic"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    trimmed_reads: list[str] = Field(default_factory=list)
    discarded_reads: list[str] = Field(default_factory=list)
    trim_params: dict[str, Any] = Field(default_factory=dict)
    decision_source: str = "heuristic"
    confidence: float = 0.5
    provenance: dict[str, Any] = Field(default_factory=dict)


class HISAT2Input(BaseModel):
    run_id: str = "unknown"
    index_basename: str
    reads: list[Path]
    output_bam: Path
    threads: int = 4
    sample_name: str | None = None


class HISAT2Output(BaseModel):
    tool_name: str = "hisat2"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    bam_path: str = ""
    bam_index: str = ""
    mapping_rate: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class SamtoolsInput(BaseModel):
    run_id: str = "unknown"
    action: Literal["sort", "index", "flagstat", "view"]
    input_path: Path
    output_path: Path | None = None
    threads: int = 4


class SamtoolsOutput(BaseModel):
    tool_name: str = "samtools"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    artifact_path: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class FeatureCountsInput(BaseModel):
    run_id: str = "unknown"
    bam_paths: list[Path]
    gtf: Path
    output_dir: Path
    threads: int = 4
    sample_name: str | None = None


class FeatureCountsOutput(BaseModel):
    tool_name: str = "featureCounts"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    count_matrix: str = ""
    summary_file: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class MultiQCInput(BaseModel):
    run_id: str = "unknown"
    input_paths: list[Path]
    output_dir: Path


class MultiQCOutput(BaseModel):
    tool_name: str = "multiqc"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    report_html: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class DESeq2Input(BaseModel):
    run_id: str = "unknown"
    count_matrix: Path
    sample_sheet: Path
    output_dir: Path
    contrast: str | None = None


class DESeq2Output(BaseModel):
    tool_name: str = "DESeq2"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    results_csv: str = ""
    pca_png: str = ""
    volcano_png: str = ""
    heatmap_png: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


class GOEnrichmentInput(BaseModel):
    run_id: str = "unknown"
    input_csv: Path
    organism: str = "human"
    output_dir: Path


class GOEnrichmentOutput(BaseModel):
    tool_name: str = "go_enrichment"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    html_report: str = ""
    csv_report: str = ""
    provenance: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    binary: str
    version_args: tuple[str, ...] = ("--version",)
    safety_level: SafetyLevel = SafetyLevel.READ
    estimated_cost: str = "low"
    dry_run_support: bool = True


class BioinformaticsTool(Tool[BaseModel, BaseModel]):
    spec: ClassVar[ToolSpec]

    def _backend(self, context: ToolContext):
        if context.backend_selector is None:
            raise RuntimeError("No backend selector configured in tool context.")
        return context.backend_selector.select(context.backend_preference).backend

    def _tool_version(self) -> str:
        if shutil.which(self.spec.binary) is None:
            return "unavailable"
        try:
            completed = subprocess.run(
                [self.spec.binary, *self.spec.version_args],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            output = (completed.stdout or completed.stderr).strip()
            return output.splitlines()[0][:120] if output else "unknown"
        except Exception:
            return "unknown"

    def _run(self, argv: list[str], context: ToolContext, description: str) -> tuple[str, str, int, str]:
        backend = self._backend(context)
        if context.console is None:
            console = Console()
        else:
            console = context.console
        spec = CommandSpec(argv=argv, stream_output=True, description=description)
        result = backend.run_command(spec, console)
        return result.stdout, result.stderr, result.returncode, result.backend

    def _provenance(self, payload: BaseModel, context: ToolContext, command: list[str], backend_name: str) -> dict[str, Any]:
        return {
            "command": command,
            "backend": backend_name,
            "tool_version": self._tool_version(),
            "dry_run": context.dry_run,
            "parameters": payload.model_dump(mode="json"),
        }


class FastQCTool(BioinformaticsTool):
    name = "fastqc"
    description = "Run FastQC quality control on one or more FASTQ files."
    safety_level = SafetyLevel.READ
    estimated_cost = "low"
    dry_run_support = True
    input_model = FastQCInput
    output_model = FastQCOutput
    spec = ToolSpec(binary="fastqc", safety_level=SafetyLevel.READ)

    def execute(self, payload: FastQCInput, context: ToolContext) -> FastQCOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        command = ["fastqc", "--outdir", str(payload.output_dir), *[str(read) for read in payload.reads]]
        if context.dry_run:
            return FastQCOutput(command=command, provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, "FastQC quality control")
        html_reports = [str(payload.output_dir / f"{read.stem}_fastqc.html") for read in payload.reads]
        zip_reports = [str(payload.output_dir / f"{read.stem}_fastqc.zip") for read in payload.reads]
        return FastQCOutput(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            html_reports=html_reports,
            zip_reports=zip_reports,
            provenance=self._provenance(payload, context, command, backend_name),
        )


class TrimmomaticTool(BioinformaticsTool):
    name = "trimmomatic"
    description = "Trim reads with Trimmomatic using AI-assisted parameter selection."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "medium"
    dry_run_support = True
    input_model = TrimmomaticInput
    output_model = TrimmomaticOutput
    spec = ToolSpec(binary="trimmomatic", safety_level=SafetyLevel.EXPENSIVE)

    def _decide_trim_params(self, payload: TrimmomaticInput) -> tuple[dict[str, Any], str, float]:
        default_params = {
            "LEADING": 3,
            "TRAILING": 3,
            "SLIDINGWINDOW": "4:20",
            "MINLEN": 36,
        }
        if payload.trim_params:
            return {**default_params, **payload.trim_params}, "user-supplied", 1.0

        fastqc_summary = payload.fastqc_summary.strip()
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
        if api_key and fastqc_summary:
            try:
                client = Anthropic(api_key=api_key)
                response = client.messages.create(
                    model=model,
                    max_tokens=400,
                    temperature=0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "You are an NGS trimming expert. Return strict JSON with trim params and confidence. "
                                f"FastQC summary: {fastqc_summary}\n"
                                "Schema: {\"params\": {...}, \"confidence\": 0.0-1.0, \"source\": string}"
                            ),
                        }
                    ],
                )
                text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
                parsed = json.loads(text)
                params = {**default_params, **parsed.get("params", {})}
                return params, str(parsed.get("source", "claude-opus-4-7")), float(parsed.get("confidence", 0.7))
            except Exception:
                pass

        fail_count = fastqc_summary.upper().count("FAIL")
        warn_count = fastqc_summary.upper().count("WARN")
        params = dict(default_params)
        if fail_count >= 2:
            params.update({"LEADING": 5, "TRAILING": 5, "SLIDINGWINDOW": "4:22", "MINLEN": 40})
        elif warn_count >= 3:
            params.update({"LEADING": 4, "TRAILING": 4, "SLIDINGWINDOW": "4:21", "MINLEN": 38})
        confidence = min(0.95, 0.55 + 0.08 * fail_count + 0.03 * warn_count)
        return params, "heuristic", confidence

    def execute(self, payload: TrimmomaticInput, context: ToolContext) -> TrimmomaticOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        trim_params, decision_source, confidence = self._decide_trim_params(payload)
        command = ["trimmomatic", "PE" if payload.paired_end else "SE"]
        if payload.paired_end:
            if payload.input_r1 is None or payload.input_r2 is None:
                raise ToolExecutionError("Paired-end trimming requires both input_r1 and input_r2.")
            trimmed_r1 = payload.output_dir / f"{payload.sample_name or payload.run_id}_R1.trimmed.fastq.gz"
            trimmed_r2 = payload.output_dir / f"{payload.sample_name or payload.run_id}_R2.trimmed.fastq.gz"
            discarded_r1 = payload.output_dir / f"{payload.sample_name or payload.run_id}_R1.unpaired.fastq.gz"
            discarded_r2 = payload.output_dir / f"{payload.sample_name or payload.run_id}_R2.unpaired.fastq.gz"
            command.extend(
                [
                    str(payload.input_r1),
                    str(payload.input_r2),
                    str(trimmed_r1),
                    str(discarded_r1),
                    str(trimmed_r2),
                    str(discarded_r2),
                ]
            )
        else:
            if payload.input_single is None:
                raise ToolExecutionError("Single-end trimming requires input_single.")
            trimmed_single = payload.output_dir / f"{payload.sample_name or payload.run_id}.trimmed.fastq.gz"
            command.extend([str(payload.input_single), str(trimmed_single)])
        for key, value in trim_params.items():
            command.extend([str(key), str(value)])

        if context.dry_run:
            return TrimmomaticOutput(
                command=command,
                trim_params=trim_params,
                decision_source=decision_source,
                confidence=confidence,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )

        stdout, stderr, returncode, backend_name = self._run(command, context, "Trimmomatic trimming")
        trimmed_reads: list[str] = []
        discarded_reads: list[str] = []
        if payload.paired_end:
            trimmed_reads = [
                str(payload.output_dir / f"{payload.sample_name or payload.run_id}_R1.trimmed.fastq.gz"),
                str(payload.output_dir / f"{payload.sample_name or payload.run_id}_R2.trimmed.fastq.gz"),
            ]
            discarded_reads = [
                str(payload.output_dir / f"{payload.sample_name or payload.run_id}_R1.unpaired.fastq.gz"),
                str(payload.output_dir / f"{payload.sample_name or payload.run_id}_R2.unpaired.fastq.gz"),
            ]
        else:
            trimmed_reads = [str(payload.output_dir / f"{payload.sample_name or payload.run_id}.trimmed.fastq.gz")]
        return TrimmomaticOutput(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            trimmed_reads=trimmed_reads,
            discarded_reads=discarded_reads,
            trim_params=trim_params,
            decision_source=decision_source,
            confidence=confidence,
            provenance=self._provenance(payload, context, command, backend_name),
        )


class HISAT2Tool(BioinformaticsTool):
    name = "hisat2"
    description = "Align reads to a reference genome with HISAT2."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "high"
    dry_run_support = True
    input_model = HISAT2Input
    output_model = HISAT2Output
    spec = ToolSpec(binary="hisat2", safety_level=SafetyLevel.EXPENSIVE)

    def execute(self, payload: HISAT2Input, context: ToolContext) -> HISAT2Output:
        payload.output_bam.parent.mkdir(parents=True, exist_ok=True)
        sam_path = payload.output_bam.with_suffix(".sam")
        command = ["hisat2", "-x", payload.index_basename, "-p", str(payload.threads)]
        if len(payload.reads) >= 2:
            command.extend(["-1", str(payload.reads[0]), "-2", str(payload.reads[1])])
        else:
            command.extend(["-U", str(payload.reads[0])])
        command.extend(["-S", str(sam_path)])
        if context.dry_run:
            return HISAT2Output(command=command, bam_path=str(payload.output_bam), bam_index=str(payload.output_bam) + ".bai", provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, "HISAT2 alignment")
        return HISAT2Output(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            bam_path=str(payload.output_bam),
            bam_index=str(payload.output_bam) + ".bai",
            mapping_rate="unknown",
            provenance=self._provenance(payload, context, command, backend_name),
        )


class SamtoolsTool(BioinformaticsTool):
    name = "samtools"
    description = "Run samtools operations like sort, index, flagstat, or view."
    safety_level = SafetyLevel.WRITE
    estimated_cost = "low"
    dry_run_support = True
    input_model = SamtoolsInput
    output_model = SamtoolsOutput
    spec = ToolSpec(binary="samtools", safety_level=SafetyLevel.WRITE)

    def execute(self, payload: SamtoolsInput, context: ToolContext) -> SamtoolsOutput:
        command = ["samtools", payload.action]
        artifact_path = payload.output_path or payload.input_path
        if payload.action == "sort":
            if payload.output_path is None:
                raise ToolExecutionError("samtools sort requires output_path.")
            command.extend(["-@", str(payload.threads), "-o", str(payload.output_path), str(payload.input_path)])
            artifact_path = payload.output_path
        elif payload.action == "index":
            command.append(str(payload.input_path))
            artifact_path = Path(str(payload.input_path) + ".bai")
        else:
            command.append(str(payload.input_path))
        if context.dry_run:
            return SamtoolsOutput(command=command, artifact_path=str(artifact_path), provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, f"samtools {payload.action}")
        return SamtoolsOutput(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            artifact_path=str(artifact_path),
            provenance=self._provenance(payload, context, command, backend_name),
        )


class FeatureCountsTool(BioinformaticsTool):
    name = "featureCounts"
    description = "Quantify reads against genes using featureCounts from subread."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "medium"
    dry_run_support = True
    input_model = FeatureCountsInput
    output_model = FeatureCountsOutput
    spec = ToolSpec(binary="featureCounts", safety_level=SafetyLevel.EXPENSIVE)

    def execute(self, payload: FeatureCountsInput, context: ToolContext) -> FeatureCountsOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        count_matrix = payload.output_dir / f"{payload.sample_name or payload.run_id}.counts.txt"
        summary_file = payload.output_dir / f"{payload.sample_name or payload.run_id}.summary.txt"
        command = [
            "featureCounts",
            "-T",
            str(payload.threads),
            "-a",
            str(payload.gtf),
            "-o",
            str(count_matrix),
            *[str(bam) for bam in payload.bam_paths],
        ]
        if context.dry_run:
            return FeatureCountsOutput(command=command, count_matrix=str(count_matrix), summary_file=str(summary_file), provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, "featureCounts quantification")
        return FeatureCountsOutput(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            count_matrix=str(count_matrix),
            summary_file=str(summary_file),
            provenance=self._provenance(payload, context, command, backend_name),
        )


class MultiQCTool(BioinformaticsTool):
    name = "multiqc"
    description = "Aggregate QC and pipeline outputs with MultiQC."
    safety_level = SafetyLevel.WRITE
    estimated_cost = "low"
    dry_run_support = True
    input_model = MultiQCInput
    output_model = MultiQCOutput
    spec = ToolSpec(binary="multiqc", safety_level=SafetyLevel.WRITE)

    def execute(self, payload: MultiQCInput, context: ToolContext) -> MultiQCOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        command = ["multiqc", "-o", str(payload.output_dir), *[str(path) for path in payload.input_paths]]
        report_html = str(payload.output_dir / "multiqc_report.html")
        if context.dry_run:
            return MultiQCOutput(command=command, report_html=report_html, provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, "MultiQC aggregation")
        return MultiQCOutput(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            report_html=report_html,
            provenance=self._provenance(payload, context, command, backend_name),
        )


class DESeq2Tool(BioinformaticsTool):
    name = "deseq2"
    description = "Run DESeq2 differential expression analysis and generate plots."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "medium"
    dry_run_support = True
    input_model = DESeq2Input
    output_model = DESeq2Output
    spec = ToolSpec(binary="Rscript", safety_level=SafetyLevel.EXPENSIVE)

    def execute(self, payload: DESeq2Input, context: ToolContext) -> DESeq2Output:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "Rscript",
            str(Path(__file__).resolve().parents[4] / "scripts" / "deseq2_analysis.R"),
            str(payload.count_matrix),
            str(payload.sample_sheet),
            str(payload.output_dir),
        ]
        if payload.contrast:
            command.extend(["--contrast", payload.contrast])
        results_csv = str(payload.output_dir / "deseq2_results.csv")
        pca_png = str(payload.output_dir / "pca_plot.png")
        volcano_png = str(payload.output_dir / "volcano_plot.png")
        heatmap_png = str(payload.output_dir / "heatmap.png")
        if context.dry_run:
            return DESeq2Output(command=command, results_csv=results_csv, pca_png=pca_png, volcano_png=volcano_png, heatmap_png=heatmap_png, provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, "DESeq2 differential expression")
        return DESeq2Output(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            results_csv=results_csv,
            pca_png=pca_png,
            volcano_png=volcano_png,
            heatmap_png=heatmap_png,
            provenance=self._provenance(payload, context, command, backend_name),
        )


class GOEnrichmentTool(BioinformaticsTool):
    name = "go_enrichment"
    description = "Run GO enrichment analysis from DESeq2 results or a DEG list."
    safety_level = SafetyLevel.WRITE
    estimated_cost = "low"
    dry_run_support = True
    input_model = GOEnrichmentInput
    output_model = GOEnrichmentOutput
    spec = ToolSpec(binary="Rscript", safety_level=SafetyLevel.WRITE)

    def execute(self, payload: GOEnrichmentInput, context: ToolContext) -> GOEnrichmentOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "Rscript",
            str(Path(__file__).resolve().parents[4] / "scripts" / "enrichment_analysis.R"),
            str(payload.input_csv),
            str(payload.organism),
            str(payload.output_dir),
        ]
        html_report = str(payload.output_dir / "go_enrichment.html")
        csv_report = str(payload.output_dir / "go_enrichment.csv")
        if context.dry_run:
            return GOEnrichmentOutput(command=command, html_report=html_report, csv_report=csv_report, provenance=self._provenance(payload, context, command, "dry-run"))
        stdout, stderr, returncode, backend_name = self._run(command, context, "GO enrichment analysis")
        return GOEnrichmentOutput(
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            html_report=html_report,
            csv_report=csv_report,
            provenance=self._provenance(payload, context, command, backend_name),
        )


def register_bioinformatics_tools(registry: Any) -> Any:
    registry.register(FastQCTool())
    registry.register(TrimmomaticTool())
    registry.register(HISAT2Tool())
    registry.register(SamtoolsTool())
    registry.register(FeatureCountsTool())
    registry.register(MultiQCTool())
    registry.register(DESeq2Tool())
    registry.register(GOEnrichmentTool())
    return registry
