"""Shared helpers for bioinformatics file naming conventions."""

from __future__ import annotations

from pathlib import Path

# FastQC strips the full compound extension of the input file: an input
# ``sample_R1.fastq.gz`` produces ``sample_R1_fastqc.zip`` (not
# ``sample_R1.fastq_fastqc.zip``).
_FASTQC_SUFFIXES = (
    ".fastq.gz",
    ".fq.gz",
    ".fastq.bz2",
    ".fq.bz2",
    ".fastq",
    ".fq",
    ".sam.gz",
    ".sam",
    ".bam",
    ".gz",
)


def fastqc_stem(path: Path | str) -> str:
    """Return the base name FastQC uses for its output files.

    ``Path("S1_R1.fastq.gz").stem`` is ``S1_R1.fastq`` (only the last suffix
    is stripped), but FastQC itself removes the whole ``.fastq.gz`` suffix,
    so its report for ``S1_R1.fastq.gz`` is ``S1_R1_fastqc.html``.
    """
    name = Path(path).name
    for suffix in _FASTQC_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return Path(path).stem
