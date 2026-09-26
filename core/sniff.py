"""The Sniffer — decide what a file is from its CONTENT, never from its name.

Detection order is fixed by spec and matters: a file that satisfies two
detectors must resolve the same way every time.

    1. ZIP containing ``fastqc_data.txt``  -> fastqc_zip
    2. first line starts with ##fileformat=VCF -> vcf
    3. magic bytes ``BAM\\x01``            -> bam
    4. MultiQC fingerprints               -> multiqc_report
    5. Nextflow fingerprints               -> nextflow_log
    6. Snakemake fingerprints              -> snakemake_log
    7. Cromwell / WDL fingerprints         -> cromwell_log
    8. directory                           -> folder
    9. otherwise                           -> unknown (confidence "none")

Gzip is transparent: if the file starts with 1f 8b we look at the first ~8KB
of decompressed content instead of the compressed bytes.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .models import (
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_NONE,
    KIND_BAM,
    KIND_CROMWELL_LOG,
    KIND_FASTQC_ZIP,
    KIND_FOLDER,
    KIND_MULTIQC,
    KIND_NEXTFLOW_LOG,
    KIND_SNAKEMAKE_LOG,
    KIND_UNKNOWN,
    KIND_VCF,
    SniffResult,
)
from .util import (
    decode,
    first_line,
    gzip_stream_is_intact,
    read_gzip_head,
    read_head,
)

GZIP_MAGIC = b"\x1f\x8b"
BAM_MAGIC = b"BAM\x01"
ZIP_MAGIC = b"PK"
FASTQC_MARKER = "fastqc_data.txt"
VCF_MAGIC = "##fileformat=VCF"

#: How much of a plain file we look at, and how much of a gzip stream we
#: decompress, to make the call. Small on purpose: the sniffer must be fast
#: enough to run on every file in a folder.
GZIP_SAMPLE_BYTES = 8192
TEXT_SAMPLE_BYTES = 65536

#: Suggested actions (routing tokens — doors map these to core functions).
ACTION_PARSE_FASTQC = "parse_fastqc"
ACTION_PARSE_MULTIQC = "parse_multiqc"
ACTION_AUDIT_FOLDER = "audit_folder"
ACTION_DIAGNOSE_LOG = "diagnose_log"
ACTION_UNSUPPORTED_VCF = "unsupported:vcf"
ACTION_UNSUPPORTED_BAM_ALONE = "unsupported:bam_alone"
ACTION_UNSUPPORTED_SNAKEMAKE = "unsupported:snakemake_log"
ACTION_UNSUPPORTED_CROMWELL = "unsupported:cromwell_log"
ACTION_UNKNOWN = "unknown"

# --- Nextflow fingerprints -------------------------------------------------
NF_BANNER = "N E X T F L O W"
NF_PROCESS_RE = re.compile(
    r"^\s*(?:Process|process)\s+`[^`]+`\s+(?:.*)?(completed|terminated|failed|submitted)"
)
NF_MARKERS = (
    NF_BANNER,
    "nextflow.io",
    "Launching `",
    "WorkflowStats",
    "Execution cancelled",
    "Execution complete",
)


def is_gzipped(head: bytes) -> bool:
    return head[:2] == GZIP_MAGIC


def _looks_like_fastqc_zip(path: Path) -> bool:
    try:
        if not zipfile.is_zipfile(path):
            return False
        with zipfile.ZipFile(path) as zf:
            return any(FASTQC_MARKER in name for name in zf.namelist())
    except (zipfile.BadZipFile, OSError):
        return False


def _downgrade(confidence: str, notes: list[str]) -> str:
    """A damaged container lowers how sure we are allowed to sound."""
    if any("truncated or damaged" in note for note in notes):
        return CONFIDENCE_MEDIUM if confidence == CONFIDENCE_HIGH else confidence
    return confidence


def _looks_like_nextflow(text: str) -> bool:
    if NF_BANNER in text:
        return True
    for line in text.splitlines():
        if NF_PROCESS_RE.match(line):
            return True
    # Two or more generic Nextflow markers is enough to call it a Nextflow log
    # even when the banner is missing (e.g. an excerpt from the middle).
    hits = sum(1 for marker in NF_MARKERS[1:] if marker in text)
    return hits >= 2


# --- MultiQC fingerprints ---------------------------------------------------
#: Keys that only ever appear in the machine-readable side of a MultiQC report
#: (multiqc_data.json). A JSON document carrying any of them is MultiQC output.
MULTIQC_JSON_MARKERS = (
    "report_general_stats_data",
    "report_plot_data",
    "report_saved_raw_data",
    "report_multiqc_version",
)

#: Column fragments from a general-stats table (multiqc_general_stats.txt or the
#: General Statistics table inside multiqc_report.html). The header must also
#: name the sample column, so a samplesheet can never match by accident.
MULTIQC_STATS_MARKERS = (
    "percent_duplicates",
    "percent_gc",
    "avg_sequence_length",
    "median_sequence_length",
    "total_sequences",
    "percent_fails",
    "% dups",
    "% gc",
    "m seqs",
)


def _looks_like_multiqc_json(head: bytes, text: str) -> bool:
    if not head.lstrip().startswith(b"{"):
        return False
    return any(marker in text for marker in MULTIQC_JSON_MARKERS)


def _looks_like_multiqc_general_stats(text: str) -> bool:
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) < 3 or cells[0].strip().lower() not in ("sample", "sample name"):
            return False
        lowered = line.lower()
        return any(marker in lowered for marker in MULTIQC_STATS_MARKERS)
    return False


def _looks_like_multiqc_html(text: str) -> bool:
    lowered = text.lower()
    if "<html" not in lowered or "multiqc" not in lowered:
        return False
    return "general statistics" in lowered or "general_stats" in lowered


def _looks_like_multiqc(head: bytes, text: str) -> bool:
    return (
        _looks_like_multiqc_json(head, text)
        or _looks_like_multiqc_general_stats(text)
        or _looks_like_multiqc_html(text)
    )


# --- Snakemake fingerprints --------------------------------------------------
SNAKEMAKE_MARKERS = (
    "Building DAG of jobs",
    "Using shell:",
    "Finished job",
    "Error in rule",
    "Complete log:",
    "Shutting down, this might take some time",
    "Exiting because a job execution failed",
    "Nothing to be done",
    "Workflow defines that rule",
)


def _looks_like_snakemake(text: str) -> bool:
    lowered = text.lower()
    hits = sum(1 for marker in SNAKEMAKE_MARKERS if marker.lower() in lowered)
    if "snakemake" in lowered and hits >= 1:
        return True
    # A long excerpt may never name the runner; three distinctive phrases do.
    return hits >= 3


# --- Cromwell / WDL fingerprints ----------------------------------------------
CROMWELL_MARKERS = (
    "WorkflowManagerActor",
    "WorkflowActor",
    "workflowId",
    "Final Outputs",
    "cromwell.engine",
    "call cache",
)

WDL_TASK_RE = re.compile(r"\btask\s+\w+\s*\{")
WDL_CALL_RE = re.compile(r"\bcall\s+\w+")
WDL_WORKFLOW_RE = re.compile(r"\bworkflow\s+\w*\s*\{")


def _looks_like_cromwell_log(text: str) -> bool:
    lowered = text.lower()
    if "cromwell" in lowered and any(
        word in lowered for word in ("workflow", "wdl", "call ", "actor", "final outputs")
    ):
        return True
    hits = sum(1 for marker in CROMWELL_MARKERS if marker.lower() in lowered)
    return hits >= 2


def _looks_like_wdl_source(text: str) -> bool:
    """A WDL workflow definition (not a log): workflow/task/call blocks."""
    if not (WDL_WORKFLOW_RE.search(text) or WDL_TASK_RE.search(text)):
        return False
    return bool(WDL_CALL_RE.search(text) or "version 1." in text)


def sniff(path: str | Path) -> SniffResult:
    """Return what ``path`` is. Never raises, never guesses."""
    p = Path(path)
    result_path = str(p)

    # 8. Directories are checked first in practice — they have no content.
    if p.is_dir():
        return SniffResult(
            kind=KIND_FOLDER,
            confidence=CONFIDENCE_HIGH,
            suggested_action=ACTION_AUDIT_FOLDER,
            path=result_path,
            notes=["Directory: audited as a run folder."],
        )

    if not p.exists():
        return SniffResult(
            kind=KIND_UNKNOWN,
            confidence=CONFIDENCE_NONE,
            suggested_action=ACTION_UNKNOWN,
            path=result_path,
            notes=["Path does not exist."],
        )
    if not p.is_file():
        return SniffResult(
            kind=KIND_UNKNOWN,
            confidence=CONFIDENCE_NONE,
            suggested_action=ACTION_UNKNOWN,
            path=result_path,
            notes=["Not a regular file."],
        )

    head = read_head(p, TEXT_SAMPLE_BYTES)
    notes: list[str] = []

    if is_gzipped(head):
        content_bytes, gzip_intact = read_gzip_head(p, GZIP_SAMPLE_BYTES)
        if gzip_intact is None:
            # We stopped early (enough content to identify). For small files it
            # is cheap to also learn whether the file is whole.
            gzip_intact = gzip_stream_is_intact(p)
        if gzip_intact is False:
            notes.append(
                "Gzip stream is truncated or damaged: identified from the part "
                "that could still be read, so the file may be incomplete."
            )
        else:
            notes.append("Gzip-compressed: judged on decompressed content.")
        # A gzip stream can also carry a zip? No — but keep the raw head for
        # magic checks that are only valid on uncompressed bytes.
        zip_check_head = b""
    else:
        content_bytes = head
        zip_check_head = head

    content = decode(content_bytes)

    # 1. FastQC zip
    if zip_check_head[:2] == ZIP_MAGIC and _looks_like_fastqc_zip(p):
        return SniffResult(
            kind=KIND_FASTQC_ZIP,
            confidence=CONFIDENCE_HIGH,
            suggested_action=ACTION_PARSE_FASTQC,
            path=result_path,
            notes=["ZIP archive containing fastqc_data.txt."],
        )

    # 2. VCF
    if first_line(content).startswith(VCF_MAGIC):
        return SniffResult(
            kind=KIND_VCF,
            confidence=_downgrade(CONFIDENCE_HIGH, notes),
            suggested_action=ACTION_UNSUPPORTED_VCF,
            path=result_path,
            notes=notes + ["First line declares ##fileformat=VCF."],
        )

    # 3. BAM
    if content_bytes[:4] == BAM_MAGIC:
        return SniffResult(
            kind=KIND_BAM,
            confidence=_downgrade(CONFIDENCE_HIGH, notes),
            suggested_action=ACTION_UNSUPPORTED_BAM_ALONE,
            path=result_path,
            notes=notes + ["Magic bytes 'BAM\\x01'."],
        )

    # 4. MultiQC report (machine-readable JSON, general-stats table, or HTML)
    if _looks_like_multiqc(content_bytes, content):
        return SniffResult(
            kind=KIND_MULTIQC,
            confidence=_downgrade(CONFIDENCE_HIGH, notes),
            suggested_action=ACTION_PARSE_MULTIQC,
            path=result_path,
            notes=notes + ["MultiQC fingerprints found in content."],
        )

    # 5. Nextflow log
    if _looks_like_nextflow(content):
        return SniffResult(
            kind=KIND_NEXTFLOW_LOG,
            confidence=_downgrade(CONFIDENCE_MEDIUM, notes),
            suggested_action=ACTION_DIAGNOSE_LOG,
            path=result_path,
            notes=notes + ["Nextflow fingerprints found in text."],
        )

    # 6. Snakemake log — recognised; full diagnosis is planned (see ROADMAP.md).
    if _looks_like_snakemake(content):
        return SniffResult(
            kind=KIND_SNAKEMAKE_LOG,
            confidence=_downgrade(CONFIDENCE_MEDIUM, notes),
            suggested_action=ACTION_UNSUPPORTED_SNAKEMAKE,
            path=result_path,
            notes=notes + ["Snakemake fingerprints found in text."],
        )

    # 7. Cromwell log or WDL source — recognised; diagnosis is planned.
    if _looks_like_cromwell_log(content):
        return SniffResult(
            kind=KIND_CROMWELL_LOG,
            confidence=_downgrade(CONFIDENCE_MEDIUM, notes),
            suggested_action=ACTION_UNSUPPORTED_CROMWELL,
            path=result_path,
            notes=notes + ["Cromwell fingerprints found in text."],
        )
    if _looks_like_wdl_source(content):
        return SniffResult(
            kind=KIND_CROMWELL_LOG,
            confidence=_downgrade(CONFIDENCE_MEDIUM, notes),
            suggested_action=ACTION_UNSUPPORTED_CROMWELL,
            path=result_path,
            notes=notes + ["WDL source: workflow/task/call blocks found in text."],
        )

    # 9. Unknown — honest, and never a guess.
    return SniffResult(
        kind=KIND_UNKNOWN,
        confidence=CONFIDENCE_NONE,
        suggested_action=ACTION_UNKNOWN,
        path=result_path,
        notes=notes + ["No known content fingerprint."],
    )
