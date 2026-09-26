"""The Sniffer — decide what a file is from its CONTENT, never from its name.

Detection order is fixed by spec and matters: a file that satisfies two
detectors must resolve the same way every time.

    1. ZIP containing ``fastqc_data.txt``  -> fastqc_zip
    2. first line starts with ##fileformat=VCF -> vcf
    3. magic bytes ``BAM\\x01``            -> bam
    4. Nextflow fingerprints               -> nextflow_log
    5. directory                           -> folder
    6. otherwise                           -> unknown (confidence "none")

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
    KIND_FASTQC_ZIP,
    KIND_FOLDER,
    KIND_NEXTFLOW_LOG,
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
ACTION_AUDIT_FOLDER = "audit_folder"
ACTION_DIAGNOSE_LOG = "diagnose_log"
ACTION_UNSUPPORTED_VCF = "unsupported:vcf"
ACTION_UNSUPPORTED_BAM_ALONE = "unsupported:bam_alone"
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


def sniff(path: str | Path) -> SniffResult:
    """Return what ``path`` is. Never raises, never guesses."""
    p = Path(path)
    result_path = str(p)

    # 5. Directories are checked first in practice — they have no content.
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

    # 4. Nextflow log
    if _looks_like_nextflow(content):
        return SniffResult(
            kind=KIND_NEXTFLOW_LOG,
            confidence=_downgrade(CONFIDENCE_MEDIUM, notes),
            suggested_action=ACTION_DIAGNOSE_LOG,
            path=result_path,
            notes=notes + ["Nextflow fingerprints found in text."],
        )

    # 6. Unknown — honest, and never a guess.
    return SniffResult(
        kind=KIND_UNKNOWN,
        confidence=CONFIDENCE_NONE,
        suggested_action=ACTION_UNKNOWN,
        path=result_path,
        notes=notes + ["No known content fingerprint."],
    )
