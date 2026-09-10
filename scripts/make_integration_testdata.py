#!/usr/bin/env python3
"""Generate a tiny deterministic RNA-Seq test dataset for integration tests.

Creates, under the output directory:
  - genome.fa        mini reference (2 chromosomes, ~2 kb each)
  - genes.gtf        two genes with exons on each chromosome
  - reads_r1/r2.fastq.gz  paired reads extracted verbatim from exons
                      (perfect matches -> ~100% HISAT2 mapping rate)
  - index/           HISAT2 index (built with local hisat2-build when
                     available, otherwise via the biocontainers image)

Usage: python scripts/make_integration_testdata.py [output_dir]
"""

from __future__ import annotations

import gzip
import random
import shutil
import subprocess
import sys
from pathlib import Path

HISAT2_IMAGE = "quay.io/biocontainers/hisat2:2.2.1--h503566f_8"

# Deterministic output for reproducible CI.
rng = random.Random(42)

CHROMOSOMES = {"chr1": 2000, "chr2": 2000}
# gene -> (chrom, strand, [(exon_start, exon_end)]), 1-based inclusive.
GENES = {
    "GENE1": ("chr1", "+", [(101, 400), (601, 900)]),
    "GENE2": ("chr1", "-", [(1101, 1400), (1601, 1900)]),
    "GENE3": ("chr2", "+", [(101, 500), (701, 1100)]),
    "GENE4": ("chr2", "-", [(1201, 1600), (1701, 1950)]),
}
READ_LENGTH = 50
FRAGMENT_LENGTH = 150
READS_PER_GENE = 40


def random_sequence(length: int) -> str:
    return "".join(rng.choice("ACGT") for _ in range(length))


def reverse_complement(sequence: str) -> str:
    return sequence.translate(str.maketrans("ACGT", "TGCA"))[::-1]


def build_genome() -> dict[str, str]:
    return {chrom: random_sequence(length) for chrom, length in CHROMOSOMES.items()}


def write_reference(genome: dict[str, str], out_dir: Path) -> Path:
    fasta = out_dir / "genome.fa"
    with fasta.open("w", encoding="utf-8") as handle:
        for chrom, sequence in genome.items():
            handle.write(f">{chrom}\n")
            for i in range(0, len(sequence), 60):
                handle.write(sequence[i : i + 60] + "\n")
    return fasta


def write_gtf(out_dir: Path) -> Path:
    gtf = out_dir / "genes.gtf"
    lines = []
    for gene_id, (chrom, strand, exons) in GENES.items():
        gene_start = exons[0][0]
        gene_end = exons[-1][1]
        lines.append(
            f'{chrom}\ttest\tgene\t{gene_start}\t{gene_end}\t.\t{strand}\t.\tgene_id "{gene_id}";\n'
        )
        for index, (start, end) in enumerate(exons, start=1):
            lines.append(
                f'{chrom}\ttest\texon\t{start}\t{end}\t.\t{strand}\t.\t'
                f'gene_id "{gene_id}"; transcript_id "{gene_id}.t1"; exon_number "{index}";\n'
            )
    gtf.write_text("".join(lines), encoding="utf-8")
    return gtf


def write_reads(genome: dict[str, str], out_dir: Path) -> tuple[Path, Path]:
    records: list[tuple[str, str, str]] = []  # (name, r1, r2)
    for gene_id, (chrom, strand, exons) in GENES.items():
        sequence = genome[chrom]
        for i in range(READS_PER_GENE):
            # Pick an exon and a fragment fully inside it.
            start, end = exons[i % len(exons)]
            max_offset = (end - start + 1) - FRAGMENT_LENGTH
            offset = rng.randint(0, max(0, max_offset))
            fragment_start = start + offset  # 1-based
            fragment = sequence[fragment_start - 1 : fragment_start - 1 + FRAGMENT_LENGTH]
            if len(fragment) < FRAGMENT_LENGTH:
                continue
            r1 = fragment[:READ_LENGTH]
            r2 = reverse_complement(fragment[-READ_LENGTH:])
            if strand == "-":
                # For a stranded RF library the reads swap; emit unstranded-
                # style pairs (both orientations map fine for this test).
                pass
            records.append((f"read_{gene_id}_{i}", r1, r2))

    r1_path = out_dir / "reads_r1.fastq.gz"
    r2_path = out_dir / "reads_r2.fastq.gz"
    with gzip.open(r1_path, "wt") as r1_handle, gzip.open(r2_path, "wt") as r2_handle:
        for name, r1, r2 in records:
            quality = "I" * READ_LENGTH
            r1_handle.write(f"@{name}/1\n{r1}\n+\n{quality}\n")
            r2_handle.write(f"@{name}/2\n{r2}\n+\n{quality}\n")
    return r1_path, r2_path


def build_index(fasta: Path, index_dir: Path) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    index_basename = index_dir / "genome_index"
    if shutil.which("hisat2-build"):
        subprocess.run(
            ["hisat2-build", str(fasta), str(index_basename)],
            check=True,
            capture_output=True,
            text=True,
        )
        return
    # Fall back to the pinned biocontainer image.
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{fasta.parent.resolve()}:/ref:rw",
            HISAT2_IMAGE,
            "hisat2-build",
            f"/ref/{fasta.name}",
            f"/ref/{index_dir.name}/genome_index",
        ],
        check=True,
    )


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "testdata")
    out_dir.mkdir(parents=True, exist_ok=True)
    genome = build_genome()
    fasta = write_reference(genome, out_dir)
    gtf = write_gtf(out_dir)
    r1, r2 = write_reads(genome, out_dir)
    build_index(fasta, out_dir / "index")

    print(f"Genome:      {fasta}")
    print(f"Annotation:  {gtf}")
    print(f"Reads:       {r1}, {r2}")
    print(f"HISAT2 index files: {sorted(p.name for p in (out_dir / 'index').glob('*.ht2'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
