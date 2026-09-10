"""Samplesheet parsing for per-sample pipeline expansion.

Accepts CSV/TSV samplesheets using the nf-core column convention with
generous header aliases::

    sample,fastq_1,fastq_2,strandedness,condition
    WT_1,data/WT_1_R1.fastq.gz,data/WT_1_R2.fastq.gz,unstranded,control
    KO_1,data/KO_1_R1.fastq.gz,data/KO_1_R2.fastq.gz,unstranded,treated
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_ALIASES = {"sample", "sample_id", "samplename", "sample_name"}
FASTQ1_ALIASES = {"fastq_1", "fastq1", "r1", "read1", "forward", "fq1"}
FASTQ2_ALIASES = {"fastq_2", "fastq2", "r2", "read2", "reverse", "fq2"}
STRANDEDNESS_ALIASES = {"strandedness", "strandness", "library_type"}
CONDITION_ALIASES = {"condition", "group", "treatment"}

VALID_STRANDEDNESS = {"unstranded", "forward", "reverse", ""}


class SampleSheetError(ValueError):
    """Raised for malformed samplesheets."""


@dataclass(frozen=True)
class SampleRecord:
    sample: str
    fastq_r1: Path
    fastq_r2: Path | None = None
    strandedness: str = "unstranded"
    condition: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def paired_end(self) -> bool:
        return self.fastq_r2 is not None


def _normalize_header(column: str) -> str:
    return column.strip().lower().replace(" ", "_").replace("-", "_")


def _match_column(column: str, aliases: set[str]) -> bool:
    return _normalize_header(column) in aliases


def _resolve_path(raw: str, bases: list[Path]) -> Path:
    """Resolve a samplesheet path against a chain of base directories.

    Relative paths are tried against (1) the samplesheet's own directory,
    (2) its parent directory (the experiment root for the common
    ``data/samplesheet.csv`` layout), and (3) the process working directory.
    The first candidate that exists wins; otherwise the samplesheet-relative
    candidate is returned so validation can report a sensible missing path.
    """
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    resolved = [base / candidate for base in bases]
    for option in resolved:
        if option.exists():
            return option
    return resolved[0]


def parse_samplesheet(path: Path, validate_files: bool = True) -> list[SampleRecord]:
    """Parse a CSV/TSV samplesheet into validated ``SampleRecord`` objects.

    Relative FASTQ paths are resolved against the samplesheet directory, its
    parent, and the CWD (first existing match wins) — see ``_resolve_path``.

    Raises:
        SampleSheetError: for missing/ambiguous columns or duplicate samples.
        FileNotFoundError: when ``validate_files`` is set and a FASTQ is absent.
    """
    if not path.exists():
        raise SampleSheetError(f"Samplesheet does not exist: {path}")

    delimiter = "\t" if path.suffix.lower() in {".tsv", ".tab", ".txt"} else ","
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise SampleSheetError(f"Samplesheet is empty: {path}")
        header = [name.strip() for name in reader.fieldnames]

        sample_col = next((c for c in header if _match_column(c, SAMPLE_ALIASES)), None)
        r1_col = next((c for c in header if _match_column(c, FASTQ1_ALIASES)), None)
        r2_col = next((c for c in header if _match_column(c, FASTQ2_ALIASES)), None)
        strand_col = next((c for c in header if _match_column(c, STRANDEDNESS_ALIASES)), None)
        condition_col = next((c for c in header if _match_column(c, CONDITION_ALIASES)), None)

        if sample_col is None:
            raise SampleSheetError(
                f"Samplesheet {path.name} has no sample column (expected one of"
                    f"{sorted(SAMPLE_ALIASES)})"
            )
        if r1_col is None:
            raise SampleSheetError(
                f"Samplesheet {path.name} has no fastq_1 column (expected one of"
                    f"{sorted(FASTQ1_ALIASES)})"
            )

        records: list[SampleRecord] = []
        seen: set[str] = set()
        bases = [path.parent, path.parent.parent, Path.cwd()]
        for line_number, row in enumerate(reader, start=2):
            sample = (row.get(sample_col) or "").strip()
            if not sample:
                continue  # skip blank lines
            if sample in seen:
                raise SampleSheetError(
                    f"Duplicate sample name '{sample}' in {path.name} (line {line_number})"
                )

            r1_raw = (row.get(r1_col) or "").strip()
            if not r1_raw:
                raise SampleSheetError(
                    f"Sample '{sample}' (line {line_number}) has an empty fastq_1 path"
                )

            r1 = _resolve_path(r1_raw, bases)
            r2: Path | None = None
            if r2_col and (row.get(r2_col) or "").strip():
                r2 = _resolve_path(row[r2_col].strip(), bases)

            strandedness = "unstranded"
            if strand_col and (row.get(strand_col) or "").strip():
                strandedness = row[strand_col].strip().lower()
                if strandedness not in VALID_STRANDEDNESS:
                    raise SampleSheetError(
                        f"Sample '{sample}' has invalid strandedness '{strandedness}' "
                        f"(expected one of {sorted(VALID_STRANDEDNESS - {''})})"
                    )

            condition = (row.get(condition_col) or "").strip() if condition_col else ""

            extra: dict[str, str] = {}
            for column in header:
                if column not in {sample_col, r1_col, r2_col, strand_col, condition_col}:
                    value = (row.get(column) or "").strip()
                    if value:
                        extra[column] = value

            seen.add(sample)
            records.append(
                SampleRecord(
                    sample=sample,
                    fastq_r1=r1,
                    fastq_r2=r2,
                    strandedness=strandedness,
                    condition=condition,
                    metadata=extra,
                )
            )

    if not records:
        raise SampleSheetError(f"Samplesheet contains no samples: {path}")

    if validate_files:
        missing = [
            str(read.fastq_r1 if index is None else read.fastq_r2)
            for read in records
            for index in (None, 1)
            if (read.fastq_r1 if index is None else read.fastq_r2) is not None
            and not (read.fastq_r1 if index is None else read.fastq_r2).exists()  # type: ignore[union-attr]
        ]
        if missing:
            raise FileNotFoundError(
                f"Samplesheet references missing FASTQ files: {', '.join(missing[:5])}"
                + (" ..." if len(missing) > 5 else "")
            )

    return records
