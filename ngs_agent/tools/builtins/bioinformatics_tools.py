from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import BaseModel, Field
from rich.console import Console

from ngs_agent.bioinformatics.common import fastqc_stem
from ngs_agent.common import anthropic_create_message
from ngs_agent.config.settings import DEFAULT_ANTHROPIC_MODEL
from ngs_agent.execution.models import CommandSpec
from ngs_agent.tools.base import Tool, ToolContext
from ngs_agent.tools.permissions import SafetyLevel

logger = logging.getLogger(__name__)

STRANDNESS_CODES = {"unstranded": 0, "forward": 1, "reverse": 2}


class ToolExecutionError(RuntimeError):
    pass


def _package_script(name: str) -> Path:
    """Locate an R script shipped inside the package (bioinformatics/scripts)."""
    return Path(__file__).resolve().parents[2] / "bioinformatics" / "scripts" / name


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


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
    duration_seconds: float = 0.0


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
    summary_files: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class TrimmomaticInput(BaseModel):
    run_id: str = "unknown"
    paired_end: bool = True
    fastqc_summary: str = ""
    fastqc_summary_path: Path | None = None
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
    missing_outputs: list[str] = Field(default_factory=list)
    trim_params: dict[str, Any] = Field(default_factory=dict)
    decision_source: str = "heuristic"
    confidence: float = 0.5
    confidence_reasons: list[str] = Field(default_factory=list)
    read_stats: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class HISAT2Input(BaseModel):
    run_id: str = "unknown"
    index_basename: str
    reads: list[Path]
    output_bam: Path
    threads: int = 4
    sample_name: str | None = None
    # Splice-aware alignment: extract known splice sites from the annotation.
    gtf: Path | None = None
    known_splicesites: Path | None = None
    # 'unstranded' (default), 'forward' (FR), or 'reverse' (RF) libraries.
    rna_strandness: str | None = None
    # Do not emit unaligned records; keeps BAMs small for counting.
    no_unal: bool = True


class HISAT2Output(BaseModel):
    tool_name: str = "hisat2"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    sam_path: str = ""
    bam_path: str = ""
    bam_index: str = ""
    splicesites_file: str = ""
    mapping_rate: str = "unknown"
    provenance: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0


class HISAT2BuildInput(BaseModel):
    run_id: str = "unknown"
    reference_fasta: Path
    index_basename: str


class HISAT2BuildOutput(BaseModel):
    tool_name: str = "hisat2-build"
    status: str = "ok"
    command: list[str] = Field(default_factory=list)
    tool_version: str = "unknown"
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    index_prefix: str = ""
    index_files: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0


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
    duration_seconds: float = 0.0


class FeatureCountsInput(BaseModel):
    run_id: str = "unknown"
    bam_paths: list[Path]
    gtf: Path
    output_dir: Path
    threads: int = 4
    sample_name: str | None = None
    # 'unstranded' | 'forward' | 'reverse' -> featureCounts -s 0/1/2.
    strandness: str = "unstranded"
    # Count fragments (read pairs) instead of reads.
    paired_end: bool = False
    # Count multi-mapping reads fractionally (-M --fraction). Off by default:
    # multi-mappers are excluded, which is the conservative default for
    # differential expression.
    count_multimappers: bool = False
    # Pre-filter alignments below this MAPQ with `samtools view -q`. HISAT2
    # assigns MAPQ 0/1 to multi-mapping reads; without this filter those
    # primary alignments would otherwise be counted.
    min_mapq: int = 0
    feature_type: str = "exon"
    attribute_type: str = "gene_id"


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
    filter_commands: list[list[str]] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: float = 0.0


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
    duration_seconds: float = 0.0


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
    duration_seconds: float = 0.0


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
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class ToolSpec:
    binary: str
    version_args: tuple[str, ...] = ("--version",)
    safety_level: SafetyLevel = SafetyLevel.READ
    estimated_cost: str = "low"
    dry_run_support: bool = True


# ---------------------------------------------------------------------------
# Base helpers
# ---------------------------------------------------------------------------


BioInputT = TypeVar("BioInputT", bound=BaseModel)
BioOutputT = TypeVar("BioOutputT", bound=BaseModel)


class BioinformaticsTool(Tool[BioInputT, BioOutputT]):
    """Generic base for the bioinformatics tools: subclasses bind their own
    payload/output models so execute signatures stay LSP-compatible."""

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
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("Could not determine version of %s: %s", self.spec.binary, exc)
            return "unknown"

    def _run(
        self, argv: list[str], context: ToolContext, description: str
    ) -> tuple[str, str, int, str, float]:
        backend = self._backend(context)
        console = context.console if context.console is not None else Console()
        spec = CommandSpec(argv=argv, stream_output=True, description=description)
        result = backend.run_command(spec, console)
        return (
            result.stdout,
            result.stderr,
            result.returncode,
            result.backend,
            result.duration_seconds,
        )

    def _provenance(
        self, payload: BaseModel, context: ToolContext, command: list[str], backend_name: str
    ) -> dict[str, Any]:
        return {
            "command": command,
            "backend": backend_name,
            "tool_version": self._tool_version(),
            "dry_run": context.dry_run,
            "parameters": payload.model_dump(mode="json"),
        }


def _status(returncode: int) -> str:
    return "ok" if returncode == 0 else "failed"


# ---------------------------------------------------------------------------
# FastQC
# ---------------------------------------------------------------------------


class FastQCTool(BioinformaticsTool[FastQCInput, FastQCOutput]):
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
        command = [
            "fastqc",
            "--outdir",
            str(payload.output_dir),
            *[str(read) for read in payload.reads],
        ]
        if context.dry_run:
            return FastQCOutput(
                command=command, provenance=self._provenance(payload, context, command, "dry-run")
            )
        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "FastQC quality control"
        )
        html_reports = [
            str(payload.output_dir / f"{fastqc_stem(read)}_fastqc.html") for read in payload.reads
        ]
        zip_reports = [
            str(payload.output_dir / f"{fastqc_stem(read)}_fastqc.zip") for read in payload.reads
        ]
        # Extract fastqc_summary.txt from each result zip so downstream steps
        # (trim decisions, reports) can read machine-readable QC data without
        # parsing HTML.
        summary_files = self._extract_summaries(payload.output_dir, payload.reads)
        return FastQCOutput(
            command=command,
            status=_status(returncode),
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            html_reports=[path for path in html_reports if Path(path).exists()],
            zip_reports=[path for path in zip_reports if Path(path).exists()],
            summary_files=summary_files,
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )

    @staticmethod
    def _extract_summaries(output_dir: Path, reads: list[Path]) -> list[str]:
        summaries: list[str] = []
        for read in reads:
            stem = fastqc_stem(read)
            zip_path = output_dir / f"{stem}_fastqc.zip"
            if not zip_path.exists():
                continue
            target = output_dir / f"{stem}_fastqc.summary.txt"
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    member_name = f"{stem}/fastqc_summary.txt"
                    if member_name in archive.namelist():
                        target.write_text(
                            archive.read(member_name).decode("utf-8"), encoding="utf-8"
                        )
                        summaries.append(str(target))
            except (zipfile.BadZipFile, OSError) as exc:
                logger.warning("Could not extract fastqc_summary.txt from %s: %s", zip_path, exc)
        return summaries


# ---------------------------------------------------------------------------
# Trimmomatic
# ---------------------------------------------------------------------------


def parse_fastqc_summary(text: str) -> list[tuple[str, str, str]]:
    """Parse FastQC ``fastqc_summary.txt`` content into (status, module, file).

    Proper line-based parsing replaces substring counting, which both
    mis-counted (the word "FAIL" can appear elsewhere) and ignored *which*
    module failed - and the module identity is what should drive trimming.
    """
    entries: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].strip().upper() in {"PASS", "WARN", "FAIL"}:
            entries.append((parts[0].strip().upper(), parts[1].strip(), parts[2].strip()))
    return entries


TRIMMOMATIC_DEFAULT_PARAMS: dict[str, Any] = {
    "LEADING": 3,
    "TRAILING": 3,
    "SLIDINGWINDOW": "4:20",
    "MINLEN": 36,
}


class TrimmomaticTool(BioinformaticsTool[TrimmomaticInput, TrimmomaticOutput]):
    name = "trimmomatic"
    description = "Trim reads with Trimmomatic using QC-informed parameter selection."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "medium"
    dry_run_support = True
    input_model = TrimmomaticInput
    output_model = TrimmomaticOutput
    spec = ToolSpec(binary="trimmomatic", safety_level=SafetyLevel.EXPENSIVE)

    # ------------------------------------------------------------------
    # Parameter decision
    # ------------------------------------------------------------------
    def _load_summary(self, payload: TrimmomaticInput) -> str:
        if payload.fastqc_summary.strip():
            return payload.fastqc_summary
        if payload.fastqc_summary_path is not None and payload.fastqc_summary_path.exists():
            try:
                return payload.fastqc_summary_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "Could not read FastQC summary %s: %s", payload.fastqc_summary_path, exc
                )
        return ""

    def _ai_trim_params(
        self, summary: str, default_params: dict[str, Any]
    ) -> tuple[dict[str, Any], str, float, list[str]] | None:
        """Optionally ask the configured Anthropic model for trim parameters."""
        import os

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key or not summary.strip():
            return None
        try:
            from anthropic import Anthropic
        except ImportError:
            logger.debug("anthropic SDK not installed; using heuristic trim parameters")
            return None

        model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        try:
            client = Anthropic(api_key=api_key)
            response = anthropic_create_message(
                client,
                model=model,
                max_tokens=400,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "You are an NGS trimming expert. Return strict JSON with trim params"
                                "and confidence. "
                            f"FastQC summary: {summary}\n"
                            'Schema: {"params": {...}, "confidence": 0.0-1.0, "source": string}'
                        ),
                    }
                ],
            )
            text = "".join(
                block.text for block in response.content if getattr(block, "type", "") == "text"
            )
            parsed = json.loads(text)
            params = {**default_params, **parsed.get("params", {})}
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.7))))
            return (
                params,
                f"ai:{model}",
                confidence,
                [f"Parameters suggested by {model} from FastQC summary"],
            )
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning(
                "AI trim parameter selection returned invalid data (%s); falling back to"
                    "heuristics",
                exc,
            )
            return None
        except Exception as exc:
            logger.warning(
                "AI trim parameter selection failed (%s); falling back to heuristics", exc
            )
            return None

    def _decide_trim_params(
        self, payload: TrimmomaticInput
    ) -> tuple[dict[str, Any], str, float, list[str]]:
        """Decide trim parameters and an explainable confidence score.

        The confidence is a transparent 0-1 mapping over evidence quality, not
        a raw score: it starts at 0.5 (sensible defaults, no evidence), rises
        with the amount of parsed FastQC evidence and whether an actionable
        module (adapter content / per-base quality) was actually detected, and
        is capped at 0.95 because heuristics are never certain. Each
        contribution is returned as a human-readable reason.
        """
        default_params = dict(TRIMMOMATIC_DEFAULT_PARAMS)
        reasons: list[str] = []

        if payload.trim_params:
            reasons.append("Parameters supplied by the user; no inference needed")
            return {**default_params, **payload.trim_params}, "user-supplied", 1.0, reasons

        summary = self._load_summary(payload)
        if not summary.strip():
            reasons.append(
                "No FastQC summary available; using conservative defaults (confidence 0.5)"
            )
            return dict(default_params), "heuristic:defaults", 0.5, reasons

        ai = self._ai_trim_params(summary, default_params)
        if ai is not None:
            return ai

        entries = parse_fastqc_summary(summary)
        if not entries:
            reasons.append(
                "FastQC summary could not be parsed; using conservative defaults (confidence 0.5)"
            )
            return dict(default_params), "heuristic:defaults", 0.5, reasons

        failed_modules = {module for status, module, _ in entries if status == "FAIL"}
        warned_modules = {module for status, module, _ in entries if status == "WARN"}
        params = dict(default_params)
        reasons.append(
            f"Parsed {len(entries)} FastQC module results ({len(failed_modules)} FAIL,"
                f"{len(warned_modules)} WARN)"
        )

        # Module-driven policy: adapter contamination and low per-base quality
        # are the two findings trimming can actually act on.
        if any("adapter" in module.lower() for module in failed_modules | warned_modules):
            adapter_file = "TruSeq3-PE.fa" if payload.paired_end else "TruSeq3-SE.fa"
            params["ILLUMINACLIP"] = f"{adapter_file}:2:30:10"
            reasons.append(
                f"Adapter content flagged by FastQC -> added ILLUMINACLIP ({adapter_file})"
            )
        if any("per base quality" in module.lower() for module in failed_modules):
            params.update({"LEADING": 5, "TRAILING": 5, "SLIDINGWINDOW": "4:22"})
            reasons.append(
                "Per-base quality FAIL -> more aggressive LEADING/TRAILING and sliding window"
            )
        elif len(failed_modules) >= 2:
            params.update({"SLIDINGWINDOW": "4:22", "MINLEN": 40})
            reasons.append("Multiple FAIL modules -> tightened sliding window and MINLEN")

        # Confidence: 0.5 base + 0.05 per distinct FAIL module + 0.02 per WARN
        # module + 0.1 when an actionable module drove a policy change,
        # capped at 0.95. Bounded and monotonic in the evidence available.
        confidence = 0.5 + 0.05 * len(failed_modules) + 0.02 * len(warned_modules)
        if (
            any(key in params for key in ("ILLUMINACLIP",))
            or params["SLIDINGWINDOW"] != default_params["SLIDINGWINDOW"]
        ):
            confidence += 0.10
        confidence = min(0.95, round(confidence, 2))
        reasons.append(
            f"Confidence {confidence:.2f} = 0.5 base + FAIL/WARN evidence + actionable-module"
                f"bonus (capped 0.95)"
        )
        return params, "heuristic:fastqc", confidence, reasons

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute(self, payload: TrimmomaticInput, context: ToolContext) -> TrimmomaticOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        trim_params, decision_source, confidence, reasons = self._decide_trim_params(payload)
        command = ["trimmomatic", "PE" if payload.paired_end else "SE"]
        if payload.paired_end:
            if payload.input_r1 is None or payload.input_r2 is None:
                raise ToolExecutionError("Paired-end trimming requires both input_r1 and input_r2.")
            label = payload.sample_name or payload.run_id
            trimmed_r1 = payload.output_dir / f"{label}_R1.trimmed.fastq.gz"
            trimmed_r2 = payload.output_dir / f"{label}_R2.trimmed.fastq.gz"
            unpaired_r1 = payload.output_dir / f"{label}_R1.unpaired.fastq.gz"
            unpaired_r2 = payload.output_dir / f"{label}_R2.unpaired.fastq.gz"
            command.extend(
                [
                    str(payload.input_r1),
                    str(payload.input_r2),
                    str(trimmed_r1),
                    str(unpaired_r1),
                    str(trimmed_r2),
                    str(unpaired_r2),
                ]
            )
        else:
            if payload.input_single is None:
                raise ToolExecutionError("Single-end trimming requires input_single.")
            trimmed_single = (
                payload.output_dir / f"{payload.sample_name or payload.run_id}.trimmed.fastq.gz"
            )
            command.extend([str(payload.input_single), str(trimmed_single)])
        for key, value in trim_params.items():
            command.extend([str(key), str(value)])

        if context.dry_run:
            return TrimmomaticOutput(
                command=command,
                trim_params=trim_params,
                decision_source=decision_source,
                confidence=confidence,
                confidence_reasons=reasons,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )

        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "Trimmomatic trimming"
        )

        # Discover outputs from the filesystem instead of trusting the paths
        # we *expected* Trimmomatic to write: only existing files are reported
        # as artifacts, and missing ones are called out explicitly.
        expected = (
            [trimmed_r1, trimmed_r2, unpaired_r1, unpaired_r2]
            if payload.paired_end
            else [trimmed_single]
        )
        trimmed_reads = [
            str(path) for path in expected[: 2 if payload.paired_end else 1] if path.exists()
        ]
        discarded_reads = [
            str(path) for path in expected[2:] if payload.paired_end and path.exists()
        ]
        missing_outputs = [str(path) for path in expected if not path.exists()]
        if returncode == 0 and missing_outputs:
            logger.error(
                "Trimmomatic reported success but outputs are missing: %s",
                ", ".join(missing_outputs),
            )

        read_stats = self._parse_trim_stats(stderr or stdout)
        status = _status(returncode)
        if returncode == 0 and not trimmed_reads:
            status = "failed"

        return TrimmomaticOutput(
            status=status,
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            trimmed_reads=trimmed_reads,
            discarded_reads=discarded_reads,
            missing_outputs=missing_outputs,
            trim_params=trim_params,
            decision_source=decision_source,
            confidence=confidence,
            confidence_reasons=reasons,
            read_stats=read_stats,
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )

    @staticmethod
    def _parse_trim_stats(text: str) -> dict[str, Any]:
        """Parse Trimmomatic's read-survival statistics from its log output."""
        stats: dict[str, Any] = {}
        pattern = re.compile(
            r"Input Read Pairs: (?P<pairs>[\d]+) "
            r"Both Surviving: (?P<both>[\d]+) \((?P<both_pct>[0-9.]+)%\) "
            r"Forward Only Surviving: (?P<fwd>[\d]+) \((?P<fwd_pct>[0-9.]+)%\) "
            r"Reverse Only Surviving: (?P<rev>[\d]+) \((?P<rev_pct>[0-9.]+)%\) "
            r"Dropped: (?P<dropped>[\d]+) \((?P<dropped_pct>[0-9.]+)%\)"
        )
        for line in text.splitlines():
            match = pattern.search(line)
            if match:
                stats = {
                    key: int(value) if not key.endswith("_pct") else float(value)
                    for key, value in match.groupdict().items()
                }
                break
        single = re.search(
            r"Input Reads: (?P<reads>\d+) Surviving: (?P<surviving>\d+) "
            r"\((?P<pct>[0-9.]+)%\) Dropped: (?P<dropped>\d+)",
            text or "",
        )
        if single and not stats:
            stats = {
                "reads": int(single.group("reads")),
                "surviving": int(single.group("surviving")),
                "surviving_pct": float(single.group("pct")),
                "dropped": int(single.group("dropped")),
            }
        return stats


# ---------------------------------------------------------------------------
# HISAT2 (+ index building)
# ---------------------------------------------------------------------------


class HISAT2Tool(BioinformaticsTool[HISAT2Input, HISAT2Output]):
    name = "hisat2"
    description = (
        "Splice-aware alignment with HISAT2, using known splice sites extracted  from the GTF"
            "annotation (--known-splicesites-infile)."
    )
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "high"
    dry_run_support = True
    input_model = HISAT2Input
    output_model = HISAT2Output
    spec = ToolSpec(binary="hisat2", safety_level=SafetyLevel.EXPENSIVE)

    def _extract_splicesites(
        self, gtf: Path, target: Path, context: ToolContext
    ) -> tuple[str, str, int]:
        """Run hisat2_extract_splice_sites.py and capture its stdout."""
        command = ["hisat2_extract_splice_sites.py", str(gtf)]
        stdout, stderr, returncode, _backend, _duration = self._run(
            command, context, "HISAT2 splice-site extraction"
        )
        if returncode == 0 and stdout.strip():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(stdout, encoding="utf-8")
        return stdout, stderr, returncode

    def execute(self, payload: HISAT2Input, context: ToolContext) -> HISAT2Output:
        payload.output_bam.parent.mkdir(parents=True, exist_ok=True)
        sam_path = payload.output_bam.with_suffix(".sam")
        command = ["hisat2", "-p", str(payload.threads), "-x", payload.index_basename]

        splicesites_path = payload.known_splicesites
        if splicesites_path is None and payload.gtf is not None:
            splicesites_path = payload.output_bam.parent / "splicesites.txt"
        if payload.rna_strandness and payload.rna_strandness.lower() in STRANDNESS_CODES:
            command.extend(["--rna-strandness", payload.rna_strandness.lower()])
        if payload.no_unal:
            command.append("--no-unal")

        if len(payload.reads) >= 2:
            command.extend(["-1", str(payload.reads[0]), "-2", str(payload.reads[1])])
        else:
            command.extend(["-U", str(payload.reads[0])])
        command.extend(["-S", str(sam_path)])

        # Splice-site extraction happens first (its output feeds the aligner).
        splice_stdout = splice_stderr = ""
        splice_rc = 0
        if splicesites_path is not None and payload.gtf is not None:
            if context.dry_run:
                # Preview the splice-aware command without running the extractor.
                command.extend(["--known-splicesites-infile", str(splicesites_path)])
            else:
                if splicesites_path.exists():
                    splice_rc = 0  # reuse a previously extracted file
                else:
                    splice_stdout, splice_stderr, splice_rc = self._extract_splicesites(
                        payload.gtf, splicesites_path, context
                    )
                if splice_rc == 0 and splicesites_path.exists():
                    # Standard of care for splice-aware alignment: feed known
                    # junctions from the annotation to the aligner.
                    command.extend(["--known-splicesites-infile", str(splicesites_path)])

        if context.dry_run:
            return HISAT2Output(
                command=command,
                sam_path=str(sam_path),
                bam_path=str(payload.output_bam),
                bam_index=str(payload.output_bam) + ".bai",
                splicesites_file=str(splicesites_path) if splicesites_path else "",
                provenance=self._provenance(payload, context, command, "dry-run"),
            )

        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "HISAT2 alignment"
        )
        combined_stderr = "\n".join(part for part in (splice_stderr, stderr) if part)
        mapping_rate = self._parse_mapping_rate(stderr)
        return HISAT2Output(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=combined_stderr,
            returncode=returncode,
            sam_path=str(sam_path) if sam_path.exists() else "",
            bam_path=str(payload.output_bam),
            bam_index=str(payload.output_bam) + ".bai",
            splicesites_file=str(splicesites_path)
            if splicesites_path and splicesites_path.exists()
            else "",
            mapping_rate=mapping_rate,
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )

    @staticmethod
    def _parse_mapping_rate(stderr: str) -> str:
        match = re.search(r"overall alignment rate: ([0-9.]+%)", stderr or "")
        return match.group(1) if match else "unknown"


class HISAT2BuildTool(BioinformaticsTool[HISAT2BuildInput, HISAT2BuildOutput]):
    name = "hisat2-build"
    description = "Build a HISAT2 index from a reference FASTA."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "high"
    dry_run_support = True
    input_model = HISAT2BuildInput
    output_model = HISAT2BuildOutput
    spec = ToolSpec(
        binary="hisat2-build", version_args=("--version",), safety_level=SafetyLevel.EXPENSIVE
    )

    def execute(self, payload: HISAT2BuildInput, context: ToolContext) -> HISAT2BuildOutput:
        command = ["hisat2-build", str(payload.reference_fasta), payload.index_basename]
        if context.dry_run:
            return HISAT2BuildOutput(
                command=command,
                index_prefix=payload.index_basename,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )
        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "HISAT2 index build"
        )
        prefix = Path(payload.index_basename)
        index_files = (
            [str(prefix.with_name(f"{prefix.name}.{suffix}.ht2")) for suffix in range(1, 9)]
            if prefix.parent != Path("")
            else [f"{prefix.name}.{suffix}.ht2" for suffix in range(1, 9)]
        )
        index_files = [path for path in index_files if Path(path).exists()]
        return HISAT2BuildOutput(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            index_prefix=payload.index_basename,
            index_files=index_files,
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# samtools
# ---------------------------------------------------------------------------


class SamtoolsTool(BioinformaticsTool[SamtoolsInput, SamtoolsOutput]):
    name = "samtools"
    description = "Run samtools sort, index, flagstat, or view."
    safety_level = SafetyLevel.WRITE
    estimated_cost = "low"
    dry_run_support = True
    input_model = SamtoolsInput
    output_model = SamtoolsOutput
    spec = ToolSpec(binary="samtools", safety_level=SafetyLevel.WRITE)

    def build_command(self, payload: SamtoolsInput) -> tuple[list[str], Path]:
        """Return (argv, artifact_path) for the requested action."""
        if payload.action == "sort":
            if payload.output_path is None:
                raise ToolExecutionError("samtools sort requires output_path.")
            command = [
                "samtools",
                "sort",
                "-@",
                str(payload.threads),
                "-o",
                str(payload.output_path),
                str(payload.input_path),
            ]
            return command, payload.output_path

        if payload.action == "index":
            command = ["samtools", "index", str(payload.input_path)]
            return command, Path(str(payload.input_path) + ".bai")

        if payload.action == "view":
            # `view` without an output path would dump records to stdout (or
            # an uncompressed stream), silently losing the result; require an
            # explicit output file instead.
            if payload.output_path is None:
                raise ToolExecutionError(
                    "samtools view requires output_path (records are otherwise written to"
                        "stdout, not a file)."
                )
            command = [
                "samtools",
                "view",
                "-@",
                str(payload.threads),
                "-o",
                str(payload.output_path),
                str(payload.input_path),
            ]
            return command, payload.output_path

        # flagstat: output on stdout is the artifact.
        command = ["samtools", "flagstat", "-@", str(payload.threads), str(payload.input_path)]
        return command, payload.input_path

    def execute(self, payload: SamtoolsInput, context: ToolContext) -> SamtoolsOutput:
        command, artifact_path = self.build_command(payload)
        if context.dry_run:
            return SamtoolsOutput(
                command=command,
                artifact_path=str(artifact_path),
                provenance=self._provenance(payload, context, command, "dry-run"),
            )
        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, f"samtools {payload.action}"
        )
        return SamtoolsOutput(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            artifact_path=str(artifact_path),
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# featureCounts
# ---------------------------------------------------------------------------


class FeatureCountsTool(BioinformaticsTool[FeatureCountsInput, FeatureCountsOutput]):
    name = "featureCounts"
    description = (
        "Gene-level quantification with featureCounts: strandness-aware  (-s), multi-mapping"
            "reads excluded by default, optional MAPQ pre-filtering."
    )
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "medium"
    dry_run_support = True
    input_model = FeatureCountsInput
    output_model = FeatureCountsOutput
    spec = ToolSpec(binary="featureCounts", safety_level=SafetyLevel.EXPENSIVE)

    def build_command(self, payload: FeatureCountsInput, bam_paths: list[Path]) -> list[str]:
        command = [
            "featureCounts",
            "-T",
            str(payload.threads),
            "-a",
            str(payload.gtf),
            "-t",
            payload.feature_type,
            "-g",
            payload.attribute_type,
            "-s",
            str(STRANDNESS_CODES.get(payload.strandness.lower(), 0)),
        ]
        if payload.paired_end:
            # Count fragments (pairs) rather than reads.
            command.extend(["-p", "--countReadPairs"])
        if payload.count_multimappers:
            # Opt-in: fractional counting of multi-mapping reads.
            command.extend(["-M", "--fraction"])
        command.extend(
            ["-o", str(self._count_matrix_path(payload)), *[str(bam) for bam in bam_paths]]
        )
        return command

    @staticmethod
    def _count_matrix_path(payload: FeatureCountsInput) -> Path:
        # Without an explicit sample name the matrix is simply counts.txt;
        # with one, <sample>.counts.txt (e.g. one matrix per sample).
        prefix = payload.sample_name
        name = f"{prefix}.counts.txt" if prefix else "counts.txt"
        return payload.output_dir / name

    def execute(self, payload: FeatureCountsInput, context: ToolContext) -> FeatureCountsOutput:
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        count_matrix = self._count_matrix_path(payload)
        summary_file = Path(str(count_matrix) + ".summary")

        filter_commands: list[list[str]] = []
        bam_paths: list[Path] = list(payload.bam_paths)

        if payload.min_mapq > 0:
            # featureCounts has no native MAPQ filter (and multi-mapping
            # primaries from HISAT2 carry MAPQ 0/1), so pre-filter the BAMs
            # with samtools view -q. The filter commands are recorded in the
            # provenance so the step remains fully auditable.
            filtered_dir = payload.output_dir / "mapq_filtered"
            filtered_dir.mkdir(parents=True, exist_ok=True)
            filtered_paths: list[Path] = []
            for bam in bam_paths:
                # Namespace by the BAM's parent directory so identically named
                # BAMs from different samples cannot collide.
                filtered = filtered_dir / bam.parent.name / f"{bam.stem}.mapq{payload.min_mapq}.bam"
                filter_command = [
                    "samtools",
                    "view",
                    "-@",
                    str(payload.threads),
                    "-b",
                    "-q",
                    str(payload.min_mapq),
                    "-o",
                    str(filtered),
                    str(bam),
                ]
                filter_commands.append(filter_command)
                if context.dry_run:
                    filtered_paths.append(filtered)
                    continue
                self._run(filter_command, context, f"samtools MAPQ filter ({bam.name})")
                if not filtered.exists():
                    return FeatureCountsOutput(
                        status="failed",
                        command=filter_command,
                        stderr=f"samtools view -q {payload.min_mapq} produced no output for {bam}",
                        returncode=1,
                        filter_commands=filter_commands,
                        provenance=self._provenance(payload, context, filter_command, "native"),
                    )
                filtered_paths.append(filtered)
            bam_paths = filtered_paths

        command = self.build_command(payload, bam_paths)
        if context.dry_run:
            return FeatureCountsOutput(
                command=command,
                count_matrix=str(count_matrix),
                summary_file=str(summary_file),
                filter_commands=filter_commands,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )

        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "featureCounts quantification"
        )
        return FeatureCountsOutput(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            count_matrix=str(count_matrix) if count_matrix.exists() else "",
            summary_file=str(summary_file) if summary_file.exists() else "",
            filter_commands=filter_commands,
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# MultiQC
# ---------------------------------------------------------------------------


class MultiQCTool(BioinformaticsTool[MultiQCInput, MultiQCOutput]):
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
        command = [
            "multiqc",
            "-o",
            str(payload.output_dir),
            *[str(path) for path in payload.input_paths],
        ]
        report_html = str(payload.output_dir / "multiqc_report.html")
        if context.dry_run:
            return MultiQCOutput(
                command=command,
                report_html=report_html,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )
        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "MultiQC aggregation"
        )
        return MultiQCOutput(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            report_html=report_html if Path(report_html).exists() else "",
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# DESeq2 / GO enrichment (R scripts shipped with the package)
# ---------------------------------------------------------------------------


class DESeq2Tool(BioinformaticsTool[DESeq2Input, DESeq2Output]):
    name = "deseq2"
    description = "Run DESeq2 differential expression analysis and generate plots."
    safety_level = SafetyLevel.EXPENSIVE
    estimated_cost = "medium"
    dry_run_support = True
    input_model = DESeq2Input
    output_model = DESeq2Output
    spec = ToolSpec(binary="Rscript", safety_level=SafetyLevel.EXPENSIVE)

    def execute(self, payload: DESeq2Input, context: ToolContext) -> DESeq2Output:
        script = _package_script("deseq2_analysis.R")
        if not script.exists():
            raise ToolExecutionError(
                f"DESeq2 analysis script not found: {script}. The R components ship with the"
                    f"package;  reinstall ngs-agent or report a packaging bug."
            )
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "Rscript",
            str(script),
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
            return DESeq2Output(
                command=command,
                results_csv=results_csv,
                pca_png=pca_png,
                volcano_png=volcano_png,
                heatmap_png=heatmap_png,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )
        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "DESeq2 differential expression"
        )
        return DESeq2Output(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            results_csv=results_csv if Path(results_csv).exists() else "",
            pca_png=pca_png if Path(pca_png).exists() else "",
            volcano_png=volcano_png if Path(volcano_png).exists() else "",
            heatmap_png=heatmap_png if Path(heatmap_png).exists() else "",
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )


class GOEnrichmentTool(BioinformaticsTool[GOEnrichmentInput, GOEnrichmentOutput]):
    name = "go_enrichment"
    description = "Run GO enrichment analysis from DESeq2 results or a DEG list."
    safety_level = SafetyLevel.WRITE
    estimated_cost = "low"
    dry_run_support = True
    input_model = GOEnrichmentInput
    output_model = GOEnrichmentOutput
    spec = ToolSpec(binary="Rscript", safety_level=SafetyLevel.WRITE)

    def execute(self, payload: GOEnrichmentInput, context: ToolContext) -> GOEnrichmentOutput:
        script = _package_script("enrichment_analysis.R")
        if not script.exists():
            raise ToolExecutionError(
                f"Enrichment analysis script not found: {script}. The R components ship with the"
                    f"package;  reinstall ngs-agent or report a packaging bug."
            )
        payload.output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "Rscript",
            str(script),
            str(payload.input_csv),
            str(payload.organism),
            str(payload.output_dir),
        ]
        html_report = str(payload.output_dir / "go_enrichment.html")
        csv_report = str(payload.output_dir / "go_enrichment.csv")
        if context.dry_run:
            return GOEnrichmentOutput(
                command=command,
                html_report=html_report,
                csv_report=csv_report,
                provenance=self._provenance(payload, context, command, "dry-run"),
            )
        stdout, stderr, returncode, backend_name, duration = self._run(
            command, context, "GO enrichment analysis"
        )
        return GOEnrichmentOutput(
            status=_status(returncode),
            command=command,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            html_report=html_report if Path(html_report).exists() else "",
            csv_report=csv_report if Path(csv_report).exists() else "",
            provenance=self._provenance(payload, context, command, backend_name),
            duration_seconds=duration,
        )


def register_bioinformatics_tools(registry: Any) -> Any:
    registry.register(FastQCTool())
    registry.register(TrimmomaticTool())
    registry.register(HISAT2Tool())
    registry.register(HISAT2BuildTool())
    registry.register(SamtoolsTool())
    registry.register(FeatureCountsTool())
    registry.register(MultiQCTool())
    registry.register(DESeq2Tool())
    registry.register(GOEnrichmentTool())
    return registry
