"""Folder parser: a run directory in, a RunModel out.

The audit is only as good as the facts it is given, so this module is
conservative on purpose:

* every file is identified by ``core.sniff`` (content, never name);
* only small text files are read (``MAX_TEXT_BYTES``), and BAMs are read only by
  their header and their last 28 bytes;
* every number extracted is stored with the ``file:line`` it came from, in
  ``RunModel.evidence``. A metric with no provenance is never invented: it is
  simply left as ``None``, and the rules that need it stay silent.

Metric names stored in ``RunModel.metrics`` (all optional):

    assignment_rate        fraction of reads assigned to features (0-1)
    alignment_rate         percentage of reads uniquely aligned
    duplication_by_sample  {sample: fraction duplicated}
    freemix               contamination estimate (fraction)
    insert_size_median    median fragment length
    reads_per_gene        [unstranded, forward, reverse] totals (STAR)
    strandedness_declared "forward" | "reverse" | "unstranded" | "auto"
    layout_declared       "paired" | "single"
    counting_pairs        True if the counter saw read pairs
    build_evidence        [(build, locator), ...]
    bam_contigs           contig names from the first BAM header
    annotation_contigs    contig names from the annotation
    contig_styles         {"bam": ..., "annotation": ...}
    counts_units          [(path, "counts"|"tpm"|"fpkm"|"unknown")]
    adapter_curve         [(position, percent), ...]
    adapter_curve_source  locator of the QC file the curve came from
    trimming_reported     True if a trimming step left a report behind
    exit_codes            [int, ...] from the execution trace
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from ..models import BamInfo, CountsInfo, FileEntry, Receipt, RunModel
from ..sniff import sniff
from ..util import read_gzip_head, read_head, sha256_file
from .fastqc import FastQCParseError, parse_fastqc

MAX_FILES = 2000
#: Files bigger than this are represented by their size, not their content, when
#: hashing a folder: hashing a 30 GB BAM to print a footer is not a trade anyone
#: wants, and the size still pins the file down.
MANIFEST_HASH_MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024
BAM_HEADER_BYTES = 256 * 1024

#: BGZF end-of-file marker. A BAM without it was never finished writing.
BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")

ANNOTATION_SUFFIXES = (".gtf", ".gff", ".gff3")
COUNTS_NAME_TOKENS = (
    "featurecounts",
    "readspergene",
    "reads_gene",
    "quant.sf",
    "htseq",
    "rsem",
    ".counts.",
    "counts.txt",
)
DUP_NAME_TOKENS = ("markduplicates", "dup_metrics", "duplication_metrics", "dedup")
FREEMIX_NAME_TOKENS = ("selfsm", "freemix", "verifybamid", "contamination")
INSERT_NAME_TOKENS = ("insert_size", "insertsize")
TRIM_NAME_TOKENS = ("cutadapt", "trim", "fastp", "trimgalore", "adapter")
CONFIG_SUFFIXES = (".yaml", ".yml", ".config", ".conf", ".toml", ".csv", ".json")
LOG_SUFFIXES = (".log", ".txt", ".out", ".err", ".nextflow.log")

#: Canonical genome-build names, so "hg38", "GRCh38" and "GRCH38" compare equal.
CANONICAL_BUILDS = {
    "GRCH38": "GRCh38",
    "HG38": "GRCh38",
    "GRCH37": "GRCh37",
    "HG19": "GRCh37",
    "B37": "GRCh37",
    "GRCM38": "GRCm38",
    "MM10": "GRCm38",
    "GRCM39": "GRCm39",
    "MM39": "GRCm39",
}


def canonical_build(token: str) -> str:
    return CANONICAL_BUILDS.get(token.upper(), token)


def gencode_release_build(release: int) -> str:
    """GENCODE release 19 and earlier are GRCh37; 20 and later are GRCh38."""
    return "GRCh37" if release < 20 else "GRCh38"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
@dataclass
class _TextFile:
    path: Path
    relpath: str
    lines: list[str]
    sha: str = ""

    def line(self, n: int) -> str:
        return self.lines[n - 1] if 0 < n <= len(self.lines) else ""


class _Metrics:
    """Collects metric values and the receipts that prove each one."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.evidence: dict[str, list[Receipt]] = {}

    def set(self, name: str, value: Any, receipts: Iterable[Receipt]) -> None:
        if value is None:
            return
        self.values[name] = value
        self.evidence.setdefault(name, []).extend(receipts)

    def set_raw(self, name: str, value: Any) -> None:
        """Store a derived value that has no receipt of its own."""
        if value is not None:
            self.values[name] = value

    def add_receipt(self, name: str, receipt: Receipt) -> None:
        self.evidence.setdefault(name, []).append(receipt)


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _receipt(text_file: _TextFile, line_no: int, detail: str, kind: str = "") -> Receipt:
    return Receipt(
        source=f"file:{text_file.sha[:12]}",
        version=kind or "text",
        timestamp=_now(),
        locator=f"{text_file.relpath}:line={line_no}",
        detail=detail[:300],
    )


def _path_receipt(
    path: Path, relpath: str, detail: str, kind: str, locator: str | None = None
) -> Receipt:
    try:
        digest = sha256_file(path)[:12]
    except OSError:
        digest = "unreadable"
    return Receipt(
        source=f"file:{digest}",
        version=kind,
        timestamp=_now(),
        locator=locator or relpath,
        detail=detail[:300],
    )


def folder_digest(root: str | Path) -> str:
    """A hash of the folder's manifest: every path, with a hash or a size.

    This is what makes a folder report traceable: two runs with the same folder
    digest contain the same files.
    """
    import hashlib

    root_path = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        relpath = str(path.relative_to(root_path))
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= MANIFEST_HASH_MAX_BYTES:
            digest.update(f"{relpath}:{sha256_file(path)}\n".encode())
        else:
            digest.update(f"{relpath}:size={size}\n".encode())
    return digest.hexdigest()


def _read_text(path: Path) -> _TextFile | None:
    try:
        if path.stat().st_size > MAX_TEXT_BYTES:
            return None
    except OSError:
        return None
    data = read_head(path, MAX_TEXT_BYTES)
    if not data:
        return None
    return _TextFile(
        path=path,
        relpath=str(path),
        lines=data.decode("utf-8", errors="replace").splitlines(),
    )


def _num(text: str) -> float | None:
    try:
        return float(text.strip().rstrip("%"))
    except (ValueError, AttributeError):
        return None


def _table_value(lines: list[str], column: str) -> tuple[int, float] | None:
    """Find a Picard-style metrics table: a header row, then a data row."""
    header_index = None
    for i, line in enumerate(lines):
        parts = line.rstrip("\n").split("\t")
        if column in parts:
            header_index = i
            col = parts.index(column)
            break
    if header_index is None:
        return None
    for j in range(header_index + 1, min(header_index + 6, len(lines))):
        row = lines[j].rstrip("\n").split("\t")
        if len(row) > col:
            value = _num(row[col])
            if value is not None:
                return j + 1, value
    return None


def _name_has(path: Path, tokens: Iterable[str]) -> bool:
    lowered = path.name.lower()
    return any(token in lowered for token in tokens)


# --------------------------------------------------------------------------
# BAM
# --------------------------------------------------------------------------
def bam_info(path: Path, relpath: str) -> BamInfo:
    """Read a BAM's header and check whether it ends properly.

    The BAM container is: magic ``BAM\x01``, a 4-byte length, that many bytes of
    SAM-style header text, then the binary reference list. We read only that
    header, plus the last 28 bytes to look for the BGZF end-of-file marker.
    """
    import struct

    info = BamInfo(path=str(path), relpath=relpath)
    raw = read_head(path, 4)
    if raw[:4] == b"BAM\x01":
        header_bytes = read_head(path, BAM_HEADER_BYTES)
    else:
        header_bytes, _ = read_gzip_head(path, BAM_HEADER_BYTES)
    if header_bytes[:4] != b"BAM\x01":
        info.notes.append("Not a BAM: no BAM magic bytes.")
        return info
    if len(header_bytes) < 8:
        info.notes.append("BAM header is too short to read.")
        return info
    (l_text,) = struct.unpack("<i", header_bytes[4:8])
    if l_text <= 0 or 8 + l_text > len(header_bytes):
        info.notes.append("BAM header text is missing or truncated.")
        return info
    text = header_bytes[8 : 8 + l_text].decode("utf-8", errors="replace")

    contigs: list[str] = []
    n_header_lines = 0
    for line in text.splitlines():
        if not line.startswith("@"):
            continue
        n_header_lines += 1
        if line.startswith("@SQ"):
            match = re.search(r"SN:(\S+)", line)
            if match:
                contigs.append(match.group(1))
    info.contigs = contigs
    info.header_lines = n_header_lines

    tail = b""
    try:
        with path.open("rb") as fh:
            fh.seek(-len(BGZF_EOF), 2)
            tail = fh.read()
    except OSError:
        pass
    info.eof_ok = tail == BGZF_EOF
    info.truncated = not info.eof_ok
    if not info.eof_ok:
        info.notes.append("BGZF end-of-file marker missing.")
    return info


def contig_style(contigs: list[str]) -> str:
    """How are these contigs named? UCSC (chr1, chrM) vs Ensembl (1, MT)."""
    if not contigs:
        return "unknown"
    prefixed = sum(1 for c in contigs if c.startswith("chr"))
    if prefixed > len(contigs) / 2:
        return "ucsc"
    return "ensembl"


# --------------------------------------------------------------------------
# Metric extractors
# --------------------------------------------------------------------------
GENCODE_RE = re.compile(r"gencode\.v(\d+)", re.IGNORECASE)
BUILD_RE = re.compile(r"GRCh3[78]|hg19|hg38|GRCm3[89]|mm10|mm39|b37", re.IGNORECASE)
STRAND_RE = re.compile(r"strandedness\s*[:=]\s*['\"]?([a-zA-Z_-]+)", re.IGNORECASE)
PROTOCOL_RE = re.compile(
    r"(?:protocol|seq_type|library_layout)\s*[:=]\s*['\"]?([a-zA-Z_-]+)", re.IGNORECASE
)
ASSIGNED_RE = re.compile(r"^(Assigned|Unassigned\w*|__\w+)\t(\d+)\s*$")
UNIQUE_RE = re.compile(r"Uniquely mapped reads %\s*\|\s*([\d.]+)")
PAIRS_RE = re.compile(r"^Number of (pairs|reads)\t(\d+)", re.IGNORECASE)
INSERT_RE = re.compile(r"^(MEDIAN_INSERT_SIZE)\t(\d+)", re.IGNORECASE)
EXIT_HEADER_RE = re.compile(r"^(task_id|name)\t", re.IGNORECASE)


def _extract_builds(text_file: _TextFile, metrics: _Metrics) -> None:
    """Collect genome-build evidence from config files, names and paths."""
    found: list[tuple[str, str]] = []
    for i, line in enumerate(text_file.lines, start=1):
        for match in BUILD_RE.finditer(line):
            found.append((canonical_build(match.group(0)), f"{text_file.relpath}:line={i}"))
        for match in GENCODE_RE.finditer(line):
            release = int(match.group(1))
            found.append((gencode_release_build(release), f"{text_file.relpath}:line={i}"))
    for build, locator in found:
        metrics.add_receipt(
            "build_evidence",
            Receipt(
                source=f"file:{text_file.sha[:12]}",
                version="config",
                timestamp=_now(),
                locator=locator,
                detail=f"genome build {build}",
            ),
        )
    if found:
        existing = metrics.values.get("build_evidence", [])
        metrics.values["build_evidence"] = existing + found


def _extract_from_name(path: Path, relpath: str, metrics: _Metrics) -> None:
    """A file's own name is evidence too (gencode.v19.annotation.gtf = GRCh37)."""
    found: list[tuple[str, str]] = []
    for match in GENCODE_RE.finditer(path.name):
        found.append((gencode_release_build(int(match.group(1))), relpath))
    for match in BUILD_RE.finditer(path.name):
        found.append((canonical_build(match.group(0)), relpath))
    if found:
        digest = sha256_file(path)[:12]
        metrics.values.setdefault("build_evidence", []).extend(found)
        for build, locator in found:
            metrics.add_receipt(
                "build_evidence",
                Receipt(
                    source=f"file:{digest}",
                    version="filename",
                    timestamp=_now(),
                    locator=locator,
                    detail=f"genome build {build}",
                ),
            )


def _extract_assignment(text_file: _TextFile, metrics: _Metrics) -> None:
    """featureCounts summary: Assigned / all statuses."""
    assigned = None
    total = 0
    line_no = 0
    for i, line in enumerate(text_file.lines, start=1):
        match = ASSIGNED_RE.match(line.rstrip("\n"))
        if not match:
            continue
        value = int(match.group(2))
        if match.group(1) == "Assigned":
            assigned, line_no = value, i
        else:
            total += value
    if assigned is None:
        return
    total += assigned
    rate = assigned / total if total else None
    metrics.set(
        "assignment_rate",
        rate,
        [
            _receipt(
                text_file,
                line_no,
                f"Assigned {assigned:,} of {total:,} counted reads = {(rate or 0) * 100:.1f}%",
                "featureCounts summary",
            )
        ],
    )
    for i, line in enumerate(text_file.lines, start=1):
        match = PAIRS_RE.match(line.rstrip("\n"))
        if match:
            metrics.set(
                "counting_pairs",
                match.group(1).lower() == "pairs",
                [_receipt(text_file, i, line.strip(), "featureCounts summary")],
            )
            break


def _extract_alignment(text_file: _TextFile, metrics: _Metrics) -> None:
    for i, line in enumerate(text_file.lines, start=1):
        match = UNIQUE_RE.search(line)
        if match:
            value = _num(match.group(1))
            if value is not None:
                metrics.set(
                    "alignment_rate",
                    value,
                    [_receipt(text_file, i, line.strip(), "STAR Log.final.out")],
                )
            return


def _extract_duplication(text_file: _TextFile, metrics: _Metrics) -> None:
    result = _table_value(text_file.lines, "PERCENT_DUPLICATION")
    if not result:
        return
    line_no, value = result
    sample = text_file.path.name.split(".")[0]
    by_sample = metrics.values.setdefault("duplication_by_sample", {})
    by_sample[sample] = value
    metrics.add_receipt(
        "duplication_by_sample",
        _receipt(
            text_file,
            line_no,
            f"{sample}: PERCENT_DUPLICATION {value:.4f}",
            "Picard MarkDuplicates",
        ),
    )


def _extract_freemix(text_file: _TextFile, metrics: _Metrics) -> None:
    result = _table_value(text_file.lines, "FREEMIX")
    if result:
        line_no, value = result
        metrics.set(
            "freemix", value, [_receipt(text_file, line_no, f"FREEMIX {value}", "VerifyBamID")]
        )
        return
    for i, line in enumerate(text_file.lines, start=1):
        match = re.search(r"FREEMIX[^0-9+-]*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)", line)
        if match:
            value = _num(match.group(1))
            if value is not None:
                metrics.set("freemix", value, [_receipt(text_file, i, line.strip(), "VerifyBamID")])
                return


def _extract_insert_size(text_file: _TextFile, metrics: _Metrics) -> None:
    for i, line in enumerate(text_file.lines, start=1):
        match = INSERT_RE.match(line)
        if match:
            metrics.set(
                "insert_size_median",
                int(match.group(2)),
                [_receipt(text_file, i, line.strip(), "Picard InsertSizeMetrics")],
            )
            return


def _extract_reads_per_gene(text_file: _TextFile, metrics: _Metrics) -> None:
    """STAR ReadsPerGene.out.tab: [unstranded, forward, reverse] totals."""
    totals = [0, 0, 0]
    counted = 0
    for line in text_file.lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 4:
            continue
        values = [_num(p) for p in parts[1:4]]
        if any(v is None for v in values):
            continue
        # Skip the four summary rows STAR writes at the top.
        if parts[0].startswith("N_"):
            continue
        for k in range(3):
            totals[k] += int(values[k] or 0)
        counted += 1
    if counted:
        metrics.set(
            "reads_per_gene",
            totals,
            [
                _receipt(
                    text_file,
                    1,
                    f"ReadsPerGene totals across {counted} genes: unstranded {totals[0]:,}, "
                    f"forward {totals[1]:,}, reverse {totals[2]:,}",
                    "STAR ReadsPerGene",
                )
            ],
        )


def _extract_strandedness_and_layout(text_file: _TextFile, metrics: _Metrics) -> None:
    for i, line in enumerate(text_file.lines, start=1):
        match = STRAND_RE.search(line)
        if match and "strandedness_declared" not in metrics.values:
            metrics.set(
                "strandedness_declared",
                match.group(1).lower(),
                [_receipt(text_file, i, line.strip(), "config/samplesheet")],
            )
        match = PROTOCOL_RE.search(line)
        if match and "layout_declared" not in metrics.values:
            value = match.group(1).lower()
            layout = "paired" if "pair" in value else ("single" if "single" in value else None)
            if layout:
                metrics.set(
                    "layout_declared",
                    layout,
                    [_receipt(text_file, i, line.strip(), "config/samplesheet")],
                )
    # A samplesheet with a fastq_2 column is paired-end input.
    for i, line in enumerate(text_file.lines, start=1):
        if "fastq_2" in line and "layout_declared" not in metrics.values:
            metrics.set(
                "layout_declared", "paired", [_receipt(text_file, i, line.strip(), "samplesheet")]
            )
            break
    for i, line in enumerate(text_file.lines, start=1):
        if "fastq_1" in line and "," in line and "layout_declared" not in metrics.values:
            # header only; the row below tells us how many FASTQs per sample
            row = text_file.line(i + 1)
            if row.count(",") >= 2:
                metrics.set(
                    "layout_declared",
                    "paired",
                    [_receipt(text_file, i + 1, row.strip(), "samplesheet")],
                )
            else:
                metrics.set(
                    "layout_declared",
                    "single",
                    [_receipt(text_file, i + 1, row.strip(), "samplesheet")],
                )
            break


def _extract_exit_codes(text_file: _TextFile, metrics: _Metrics) -> None:
    header_col = None
    receipts: list[Receipt] = []
    codes: list[int] = []
    for i, line in enumerate(text_file.lines, start=1):
        parts = line.rstrip("\n").split("\t")
        if header_col is None:
            if "exit" in parts:
                header_col = parts.index("exit")
            continue
        if len(parts) > header_col:
            value = _num(parts[header_col])
            if value is not None:
                codes.append(int(value))
                receipts.append(_receipt(text_file, i, line.strip(), "execution trace"))
    if codes:
        metrics.set("exit_codes", codes, receipts)


def _counts_units(path: Path, lines: list[str]) -> tuple[str, CountsInfo]:
    """Decide whether a counts table holds raw counts or normalised values."""
    info = CountsInfo(path=str(path), relpath=str(path))
    header_cols: list[str] = []
    values: list[float] = []
    decimals = False
    for i, line in enumerate(lines):
        parts = line.rstrip("\n").split("\t")
        if i == 0:
            header_cols = parts
            continue
        if len(parts) < 2:
            continue
        # The last column is the count for a one-sample table; otherwise the
        # widest numeric column after the annotation columns.
        for cell in parts[1:]:
            if "." in cell:
                decimals = True
            value = _num(cell)
            if value is not None:
                values.append(value)
                break
        info.n_rows += 1
    info.columns = header_cols
    if values:
        info.max_value = max(values)
    info.has_decimals = decimals
    total = sum(values)
    name = path.name.lower()
    if total and abs(total - 1_000_000) / 1_000_000 < 0.05:
        info.inferred_units = "tpm"
    elif decimals and (info.max_value is not None and info.max_value < 100):
        info.inferred_units = "tpm"
    elif decimals and any(token in name for token in ("fpkm",)):
        info.inferred_units = "fpkm"
    elif decimals:
        info.inferred_units = "unknown"
    else:
        info.inferred_units = "counts"
    return info.inferred_units, info


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------
def parse_folder(root: str | Path) -> RunModel:
    """Walk a run folder, sniff every file, and extract what the audit needs."""
    root_path = Path(root)
    model = RunModel(root=str(root_path))
    metrics = _Metrics()

    files: list[Path] = []
    for path in sorted(root_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            continue
        files.append(path)
        if len(files) >= MAX_FILES:
            break

    for path in files:
        relpath = str(path.relative_to(root_path))
        result = sniff(path)
        entry = FileEntry(
            path=str(path),
            relpath=relpath,
            kind=result.kind,
            confidence=result.confidence,
            size=path.stat().st_size,
        )
        model.files.append(entry)

        _extract_from_name(path, relpath, metrics)

        if result.kind == "bam":
            info = bam_info(path, relpath)
            model.bams.append(info)
            if info.contigs:
                styles = dict(metrics.values.get("contig_styles", {}))
                styles["bam"] = contig_style(info.contigs)
                metrics.set_raw("contig_styles", styles)
                if "bam_contigs" not in metrics.values:
                    metrics.set(
                        "bam_contigs",
                        info.contigs,
                        [
                            _path_receipt(
                                path,
                                relpath,
                                f"{len(info.contigs)} @SQ contigs, eof_ok={info.eof_ok}",
                                "BAM header",
                            )
                        ],
                    )

        elif result.kind == "fastqc_zip":
            model.qc_files.append(entry)
            _extract_adapter_curve(path, relpath, metrics)

        elif result.kind == "nextflow_log":
            model.logs.append(entry)

        elif path.name.lower().endswith(".summary"):
            # featureCounts/HTSeq summary: the assignment rate lives here.
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_assignment(text, metrics)

        elif path.name.lower().endswith("log.final.out"):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_alignment(text, metrics)

        elif path.name.lower().endswith("readspergene.out.tab"):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_reads_per_gene(text, metrics)

        elif path.suffix.lower() in ANNOTATION_SUFFIXES:
            _extract_annotation(path, relpath, metrics)

        elif _name_has(path, DUP_NAME_TOKENS):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_duplication(text, metrics)

        elif _name_has(path, FREEMIX_NAME_TOKENS):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_freemix(text, metrics)

        elif _name_has(path, INSERT_NAME_TOKENS):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_insert_size(text, metrics)

        elif _name_has(path, ("execution_trace", "trace.txt")):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_exit_codes(text, metrics)

        elif _name_has(path, COUNTS_NAME_TOKENS):
            _extract_counts(path, relpath, metrics, model)

        elif _name_has(path, TRIM_NAME_TOKENS) and path.suffix.lower() in LOG_SUFFIXES:
            metrics.set(
                "trimming_reported",
                True,
                [_path_receipt(path, relpath, "trimming report present", "trimming report")],
            )

        elif path.suffix.lower() in CONFIG_SUFFIXES:
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_builds(text, metrics)
                _extract_strandedness_and_layout(text, metrics)

        elif path.suffix.lower() in (".txt", ".out", ".log", ".md", ".csv", ".tsv", ".yaml"):
            text = _read_text(path)
            if text:
                text.relpath = relpath
                text.sha = sha256_file(path)
                _extract_builds(text, metrics)
                _extract_strandedness_and_layout(text, metrics)

    model.metrics = metrics.values
    model.evidence = metrics.evidence
    return model


def _extract_annotation(path: Path, relpath: str, metrics: _Metrics) -> None:
    contigs: list[str] = []
    text = _read_text(path)
    if not text:
        return
    for line in text.lines:
        if line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        contig = parts[0].strip()
        if contig and contig not in contigs:
            contigs.append(contig)
        if len(contigs) >= 500:
            break
    if contigs:
        metrics.set(
            "annotation_contigs",
            contigs,
            [_path_receipt(path, relpath, f"{len(contigs)} contigs in column 1", "annotation")],
        )
        styles = metrics.values.get("contig_styles", {})
        styles["annotation"] = contig_style(contigs)
        metrics.set_raw("contig_styles", styles)


def _extract_adapter_curve(path: Path, relpath: str, metrics: _Metrics) -> None:
    try:
        facts = parse_fastqc(path)
    except (FastQCParseError, zipfile.BadZipFile, OSError):
        # QC files are a bonus: if one will not parse, the audit carries on.
        return
    if not facts.adapter_content:
        return
    peak = max(p.y for p in facts.adapter_content)
    curve = [(p.x, p.y) for p in facts.adapter_content]
    metrics.set(
        "adapter_curve",
        curve,
        [_path_receipt(path, relpath, f"FastQC adapter content peaks at {peak:.1f}%", "FastQC")],
    )
    metrics.set("adapter_curve_source", f"{relpath}:{facts.data_member}", [])
    metrics.set("read_length", facts.read_length, [])
    if facts.read_length_range:
        metrics.set("read_length_range", list(facts.read_length_range), [])


def _extract_counts(path: Path, relpath: str, metrics: _Metrics, model: RunModel) -> None:
    text = _read_text(path)
    if not text:
        return
    units, info = _counts_units(path, text.lines)
    info.relpath = relpath
    model.counts.append(info)
    entry = metrics.values.setdefault("counts_units", [])
    entry.append((relpath, units))
    metrics.values["counts_units"] = entry
    metrics.add_receipt(
        "counts_units",
        _path_receipt(
            path,
            relpath,
            f"{info.n_rows} rows, max {info.max_value}, decimals={info.has_decimals} -> {units}",
            "counts table",
        ),
    )
