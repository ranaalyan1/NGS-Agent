#!/usr/bin/env python3
"""Generate every fixture under fixtures/.

Fixtures are generated (not hand-typed) so they are reproducible byte-for-byte
and so a human can read exactly what was planted where. Run:

    python scripts/make_fixtures.py

Files this writes are tracked in git on purpose even though .gitignore drops
*.zip and *.log globally (see the negations at the bottom of that file).
"""

from __future__ import annotations

import gzip
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"

# Standard BGZF EOF marker (28 bytes). Its absence at the end of a BAM is how
# we detect a truncated file.
BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")


# ==========================================================================
# FastQC zips
# ==========================================================================
def _curve(points: list[tuple[str, float]]) -> str:
    return "".join(f"{label}\t{value}\n" for label, value in points)


def _bins(n: int, width: int = 10, max_len: int = 150) -> list[str]:
    """FastQC-style positional bin labels: 1,2,...  then 10-14, 15-19, ..."""
    labels: list[str] = []
    pos = 1
    while len(labels) < n:
        if pos <= 9:
            labels.append(str(pos))
            pos += 1
        else:
            end = min(pos + width - 1, max_len)
            labels.append(f"{pos}-{end}" if end > pos else str(pos))
            pos = end + 1
    return labels


def _quality_rows(means: list[float]) -> str:
    labels = _bins(len(means))
    out = [
        "#Base\tMean\tMedian\tLower Quartile\tUpper Quartile\t10th Percentile\t90th Percentile\n"
    ]
    for label, mean in zip(labels, means):
        out.append(
            f"{label}\t{mean:.1f}\t{round(mean)}\t{mean - 2:.1f}\t{mean + 2:.1f}\t"
            f"{mean - 5:.1f}\t{mean + 2:.1f}\n"
        )
    return "".join(out)


def _adapter_rows(percents: list[float], polyg: float = 0.0) -> str:
    labels = _bins(len(percents))
    out = [
        "#Position\tIllumina Universal Adapter\tIllumina Small RNA 3' Adapter\t"
        "Illumina Small RNA 5' Adapter\tNextera Transposase Sequence\tPolyG\n"
    ]
    # FastQC spreads the adapter signal over the adapter columns; we put most
    # of it in the universal adapter column and a little in PolyG.
    for label, pct in zip(labels, percents):
        out.append(
            f"{label}\t{pct:.6f}\t{pct * 0.35:.6f}\t{pct * 0.2:.6f}\t{pct * 0.1:.6f}\t{polyg:.6f}\n"
        )
    return "".join(out)


def _gc_rows(counts: list[float]) -> str:
    out = ["#GC\tCount\n"]
    for i, c in enumerate(counts):
        out.append(f"{i}\t{c:.6f}\n")
    return "".join(out)


def _n_rows(percents: list[float]) -> str:
    labels = _bins(len(percents), width=10, max_len=150)
    out = ["#Base\tN-Count\n"]
    for label, pct in zip(labels, percents):
        out.append(f"{label}\t{pct:.6f}\n")
    return "".join(out)


def _dup_module(dedup_pct: float) -> str:
    dup = 100.0 - dedup_pct
    return (
        f"#Total Deduplicated Percentage\t{dedup_pct:.6f}\n"
        "#Duplication Level\tPercentage of deduplicated\tPercentage of total\n"
        f"1\t100.0\t{dedup_pct:.6f}\n"
        f"2\t{dedup_pct + (100 - dedup_pct) * 0.4:.6f}\t{dup * 0.45:.6f}\n"
        f"3\t{dedup_pct + (100 - dedup_pct) * 0.7:.6f}\t{dup * 0.3:.6f}\n"
        f"4\t99.9\t{dup * 0.15:.6f}\n"
        f"5\t99.9\t{dup * 0.1:.6f}\n"
    )


def _basic_stats(filename: str, total: int, length: str, gc: int) -> str:
    return (
        "#Measure\tValue\n"
        f"Filename\t{filename}\n"
        "File type\tConventional base calls\n"
        "Encoding\tSanger / Illumina 1.9\n"
        f"Total Sequences\t{total}\n"
        f"Sequences flagged as poor quality\t0\n"
        f"Sequence length\t{length}\n"
        f"%GC\t{gc}\n"
    )


def _len_distribution(length: str) -> str:
    if "-" in length:
        lo, hi = (int(x) for x in length.split("-"))
        rows = "".join(f"{lo + i * (hi - lo) // 4}\t{0.2 + i * 0.2:.6f}\n" for i in range(5))
    else:
        rows = f"{length}\t1.0\n"
    return "#Length\tCount\n" + rows


def make_fastqc_zip(
    path: Path,
    *,
    sample: str = "sample_R1",
    quality: list[float] | None = None,
    adapter: list[float] | None = None,
    dedup_pct: float = 92.0,
    gc_counts: list[float] | None = None,
    n_percents: list[float] | None = None,
    length: str = "150",
    total: int = 1_000_000,
    statuses: dict[str, str] | None = None,
) -> Path:
    """Write a structurally real (but tiny) FastQC zip."""
    quality = quality if quality is not None else [36.0] * 15
    adapter = adapter if adapter is not None else [0.1] * 15
    n_percents = n_percents if n_percents is not None else [0.0] * 15

    if gc_counts is None:
        # A smooth, single-peak GC distribution centred on 45%.
        gc_counts = [round(1000 * _gauss(i, 45, 6), 3) for i in range(101)]

    statuses = statuses or {}
    modules: list[tuple[str, str, str]] = [
        (
            "Basic Statistics",
            statuses.get("Basic Statistics", "pass"),
            _basic_stats(f"{sample}.fastq.gz", total, length, 45),
        ),
        (
            "Per base sequence quality",
            statuses.get("Per base sequence quality", "pass"),
            _quality_rows(quality),
        ),
        ("Per tile sequence quality", "pass", "#Tile\tBase\tMean\n1\t1\t36.0\n"),
        ("Per sequence quality scores", "pass", "#Quality\tCount\n36\t1000000\n"),
        ("Per base sequence content", "pass", "#Base\tG\tA\tT\tC\n1\t25.0\t25.0\t25.0\t25.0\n"),
        (
            "Per sequence GC content",
            statuses.get("Per sequence GC content", "pass"),
            _gc_rows(gc_counts),
        ),
        ("Per base N content", statuses.get("Per base N content", "pass"), _n_rows(n_percents)),
        (
            "Sequence Length Distribution",
            statuses.get("Sequence Length Distribution", "pass"),
            _len_distribution(length),
        ),
        (
            "Sequence Duplication Levels",
            statuses.get("Sequence Duplication Levels", "pass"),
            _dup_module(dedup_pct),
        ),
        ("Overrepresented sequences", "pass", "#Sequence\tCount\tPercentage\tPossible Source\n"),
        ("Adapter Content", statuses.get("Adapter Content", "pass"), _adapter_rows(adapter)),
    ]

    data = ["##FastQC\t0.12.1\n", "\n"]
    for name, status, body in modules:
        data.append(f">>{name}\t{status}\n")
        data.append(body)
        data.append(">>END_MODULE\n\n")

    summary = "".join(f"{status}\t{name}\t{sample}.fastq.gz\n" for name, status, _ in modules)

    path.parent.mkdir(parents=True, exist_ok=True)
    folder = f"{sample}_fastqc"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in (
            (f"{folder}/fastqc_data.txt", "".join(data)),
            (f"{folder}/summary.txt", summary),
            (f"{folder}/fastqc_report.html", f"<html><body><h1>FastQC report for {sample}</h1></body></html>"),
        ):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, content)
    return path


def _gauss(x: float, mu: float, sigma: float) -> float:
    import math

    return math.exp(-((x - mu) ** 2) / (2 * sigma**2))


# ==========================================================================
# VCF / BAM / text fixtures
# ==========================================================================
VCF_BODY = """##fileformat=VCFv4.2
##fileDate=20260901
##source=fixtureGenerator
##reference=file:///refs/GRCh38.fa
##contig=<ID=chr1,length=248956422>
##contig=<ID=chr2,length=242193529>
##INFO=<ID=DP,Number=1,Type=Integer,Description="Total Depth">
##INFO=<ID=AF,Number=A,Type=Float,Description="Allele Frequency">
##FILTER=<ID=PASS,Description="All filters passed">
##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">
##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Read Depth">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1
chr1\t15274\t.\tA\tG\t112.4\tPASS\tDP=45;AF=0.5\tGT:DP\t0/1:45
chr1\t15820\t.\tC\tT\t88.1\tPASS\tDP=39;AF=0.48\tGT:DP\t0/1:39
chr2\t48327\t.\tG\tA\t221.9\tPASS\tDP=61;AF=1.0\tGT:DP\t1/1:61
"""


def _vcf_records(depths, genotypes=None, filters=None):
    genotypes = genotypes or ["0/1"] * len(depths)
    filters = filters or ["PASS"] * len(depths)
    header = VCF_BODY.split("#CHROM",1)[0] + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\n"
    rows=[]
    bases=[("A","G"),("C","T"),("A","C"),("G","T")]
    for i,depth in enumerate(depths):
        ref,alt=bases[i%len(bases)]
        rows.append(f"chr1\t{1000+i}\t.\t{ref}\t{alt}\t60\t{filters[i]}\tDP={depth}\tGT:DP\t{genotypes[i]}:{depth}\n")
    return header+"".join(rows)


def make_station10_vcfs():
    root=FIX/"vcf"
    _write(root/"clean.vcf", _vcf_records([30]*12,["0/1"]*6+["1/1"]*6))
    _write(root/"low_depth.vcf", _vcf_records([2,4,5,6,8,3,4,7,8,6]))
    gts=["./.","./.","./.","0/1","0/1","0/1","0/1","0/1","0/1","0/1"]
    _write(root/"high_missingness.vcf", _vcf_records([30]*10,gts))
    _write(root/"gvcf.vcf", "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\nchr1\t1\t.\tA\t<NON_REF>\t.\t.\tEND=100\tGT\t0/0\n")
    _write(root/"unnormalised.vcf", "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tsample1\nchr1\t1\t.\tAT\tATT\t50\tPASS\tDP=30\tGT:DP\t0/1:30\n")
    _write(root/"multi_sample.vcf", VCF_BODY.replace("sample1\n", "sample1\tsample2\n"))


def make_minimal_bam(contigs: list[str], eof: bool = True, truncated: bool = False) -> bytes:
    """A tiny but structurally valid BAM: magic, header text, ref list."""
    text = "@HD\tVN:1.6\tSO:coordinate\n"
    text += "@PG\tID:STAR\tPN:STAR\tVN:2.7.11a\n"
    for i, c in enumerate(contigs):
        text += f"@SQ\tSN:{c}\tLN:{248956422 - i}\n"
    text_bytes = text.encode()
    import struct

    body = b"BAM\x01" + struct.pack("<i", len(text_bytes)) + text_bytes
    body += struct.pack("<i", len(contigs))
    for i, c in enumerate(contigs):
        name = c.encode() + b"\x00"
        body += struct.pack("<i", len(name)) + name + struct.pack("<i", 248956422 - i)
    raw = gzip.compress(body, mtime=0)
    if truncated:
        return raw[: max(16, len(raw) // 3)]
    if eof:
        raw += BGZF_EOF
    return raw


ENSEMBL_CONTIGS = [str(i) for i in range(1, 23)] + ["X", "Y", "MT"]
UCSC_CONTIGS = [f"chr{i}" for i in range(1, 23)] + ["chrX", "chrY", "chrM"]


# ==========================================================================
# Nextflow logs
# ==========================================================================
def _nextflow_banner(version: str = "24.04.2") -> str:
    return f" N E X T F L O W  ~  version {version}\n"


def make_nextflow_ok_log() -> str:
    lines = [
        _nextflow_banner(),
        "Launching `https://github.com/nf-core/rnaseq` [focused_curie] DSL2 - revision: 3.14.0\n",
        "------------------------------------------------------\n",
        "executor >  local (12)\n",
        "[6a/1f2c34] process > NFCORE_RNASEQ:RNASEQ:INPUT_CHECK:SAMPLESHEET_CHECK (samplesheet.csv) [100%] 1 of 1 ✔\n",
    ]
    for i in range(1, 7):
        lines.append(
            f"[{i:02d}/abcdef] process > NFCORE_RNASEQ:RNASEQ:FASTQC_FASTQ:FASTQC (sample{i}) "
            f"[100%] {i} of 6 ✔\n"
        )
    for i in range(1, 7):
        lines.append(
            f"[{i:02d}/1234ab] process > NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN (sample{i}) "
            f"[100%] {i} of 6 ✔\n"
        )
    for i in range(1, 7):
        lines.append(
            f"[{i:02d}/998877] process > NFCORE_RNASEQ:RNASEQ:QUANTIFY_RSEM:RSEM_CALCULATEEXPRESSION "
            f"(sample{i}) [100%] {i} of 6 ✔\n"
        )
    # ~150 lines of routine progress so the file is a realistic excerpt size.
    for i in range(150):
        lines.append(
            f"Jun-1{i % 9 + 1} 0{i % 9 + 1}:1{i % 6}{i % 9}:2{i % 6} INFO  n.processor.TaskPollingMonitor "
            f"- Task completed > NFCORE_RNASEQ:RNASEQ:BAM_SORT_STATS_SAMTOOLS:SAMTOOLS_SORT (sample{i % 6 + 1})\n"
        )
    lines += [
        "-[nf-core/rnaseq] Pipeline completed successfully-\n",
        "WARN  ~ At least one process was retried; check the trace file for details.\n",
        "Completed at: 01-Jun-2026 09:41:22\n",
        "Duration    : 2h 11m 34s\n",
        "CPU hours   : 41.2\n",
        "Succeeded   : 12\n",
    ]
    return "".join(lines)


STAR_INDEX_FAIL_TAIL = """Jun-11 04:12:07.331 [Task monitor] ERROR ~ Error executing process > 'NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN (sample1)'

Caused by:
  Process `NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN (sample1)` terminated with an error exit status (137)

Command executed:

  STAR \
      --genomeDir /refs/GRCh38/star_index \
      --readFilesIn sample1_R1.fastq.gz sample1_R2.fastq.gz \
      --runThreadN 8

  cat <<-END_VERSIONS > versions.yml
  "NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN":
      STAR: 2.7.11a
  END_VERSIONS

Command exit status:
  137

Command output:
  (empty)

Command error:
  STAR version: 2.7.11a
  Aug 11 04:12:03 ..... started STAR run
  Aug 11 04:12:03 ..... loading genome
  EXITING because of FATAL ERROR in input: Genome version is incompatible with the genome index version
  SOLUTION: please BOOST the genome index with the same genome FASTA file, or re-generate the genome index with this genome FASTA
  SOLUTION: genome compatibility is defined by the genome length and the number of reference sequences
  The Genome was generated with the following parameters: --genomeSAindexNbases 14
  Aug 11 04:12:07 ...... FATAL ERROR, exiting

Work dir:
  /work/aa/1234abcd5678ef

Tip: you can try to figure out what's wrong by changing to the process work dir and showing the script file named `.command.sh`
"""

STAR_INDEX_FAIL_HEAD = (
    _nextflow_banner("24.04.2")
    + "Launching `https://github.com/nf-core/rnaseq` [admiring_lovelace] DSL2 - revision: 3.14.0\n"
    + "------------------------------------------------------\n"
    + "executor >  slurm (24)\n"
    + "[1a/2b3c4d] process > NFCORE_RNASEQ:RNASEQ:INPUT_CHECK:SAMPLESHEET_CHECK (samplesheet.csv) [100%] 1 of 1 ✔\n"
)


def make_star_index_fail_log() -> str:
    filler = "".join(
        f"Jun-11 04:0{i % 9}:{i % 6}0:{i % 6}0 INFO  n.processor.TaskPollingMonitor - "
        f"Task submitted > NFCORE_RNASEQ:RNASEQ:FASTQC_FASTQ:FASTQC (sample{i % 6 + 1})\n"
        for i in range(60)
    )
    return STAR_INDEX_FAIL_HEAD + filler + STAR_INDEX_FAIL_TAIL


def make_warnings_then_fatal_log() -> str:
    """50 harmless warnings, one fatal OOM kill."""
    lines = [
        _nextflow_banner("23.10.1"),
        "Launching `./main.nf` [silly_bhaskara] DSL2 - revision: 1.2.3\n",
    ]
    for i in range(50):
        lines.append(
            f"WARN  ~ The following invalid input values have been detected: sample{i} has an "
            f"empty 'strandedness' field; defaulting to 'auto'.\n"
        )
    for i in range(60):
        lines.append(
            f"Jun-02 1{i % 9}:2{i % 6}:3{i % 6} INFO  nextflow.processor.TaskRun - "
            f"Task completed > CAT_CAT (chunk_{i}) [100%]\n"
        )
    lines += [
        "Jun-02 19:44:11.120 [Task monitor] ERROR ~ Error executing process > 'NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN (sample3)'\n",
        "\n",
        "Caused by:\n",
        "  Process `NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN (sample3)` terminated with an error exit status (137)\n",
        "\n",
        "Command error:\n",
        "  .command.run: line 42: 31415 Killed                    STAR --genomeDir /refs/GRCh38/star_index\n",
        "  Command exit status:\n",
        "  137\n",
        "  slurmstepd: error: Detected 1 oom-kill event(s) in step. Some of your processes may have been killed by the cgroup out-of-memory handler.\n",
        "\n",
        "Work dir:\n",
        "  /work/cd/9988aabb\n",
        "\n",
        "Execution cancelled -- Finishing pending tasks before exit\n",
    ]
    return "".join(lines)


def make_nomatch_log() -> str:
    """A genuine Nextflow log whose failure matches no signature: -> unknown."""
    lines = [
        _nextflow_banner("24.04.2"),
        "Launching `./local_pipeline.nf` [peaceful_bose] DSL2 - revision: 0.9.1\n",
        "------------------------------------------------------\n",
        "executor >  local (4)\n",
    ]
    for i in range(1, 5):
        lines.append(f"[0{i}/aa11bb] process > PREPARE_REFERENCE (genome) [100%] {i} of 4 ✔\n")
    for i in range(40):
        lines.append(
            f"Jun-11 05:{i:02d}:11 INFO  n.processor.TaskPollingMonitor - "
            f"Task completed > CUSTOM_STEP (chunk_{i})\n"
        )
    lines += [
        "Jun-11 05:41:02.114 [Task monitor] ERROR ~ Error executing process > 'CUSTOM_STEP (chunk_41)'\n",
        "\n",
        "Caused by:\n",
        "  Process `CUSTOM_STEP (chunk_41)` terminated with an error exit status (255)\n",
        "\n",
        "Command error:\n",
        "  Segmentation fault (core dumped)\n",
        "  unexpected internal state in the vendor binary\n",
        "\n",
        "Execution cancelled -- Finishing pending tasks before exit\n",
    ]
    return "".join(lines)


# ==========================================================================
# MultiQC summaries, Snakemake / Cromwell logs, WDL source
# ==========================================================================
def make_multiqc_general_stats() -> str:
    """Four samples: one over-duplicated, one GC outlier, one short-read."""
    return (
        "Sample\tFastQC_percent_duplicates\tFastQC_percent_gc\t"
        "FastQC_avg_sequence_length\tFastQC_total_sequences\tFastQC_percent_fails\n"
        "sample1\t8.10\t45.20\t150\t20000000\t0.00\n"
        "sample2\t58.30\t44.10\t150\t18500000\t9.10\n"
        "sample3\t12.40\t62.00\t150\t19200000\t0.00\n"
        "sample4\t9.70\t46.30\t100\t17800000\t0.00\n"
    )


def make_multiqc_clean_general_stats() -> str:
    return (
        "Sample\tFastQC_percent_duplicates\tFastQC_percent_gc\t"
        "FastQC_avg_sequence_length\tFastQC_total_sequences\tFastQC_percent_fails\n"
        "clean1\t7.20\t45.10\t150\t20000000\t0.00\n"
        "clean2\t9.40\t44.80\t150\t21000000\t0.00\n"
    )


def make_multiqc_data_json() -> str:
    """Two samples: one must be re-sequenced, one needs adapter trimming."""
    import json

    quality_bad = [
        35.0,
        34.5,
        34.0,
        33.0,
        31.0,
        28.0,
        25.0,
        22.0,
        19.0,
        17.0,
        15.0,
        13.0,
        12.0,
        11.0,
        10.0,
    ]
    quality_good = [36.0] * 15
    adapter_bad = [0.1, 0.2, 0.4, 0.8, 1.5, 2.4, 3.6, 5.2, 7.1, 8.8, 10.2, 11.3, 12.0, 12.1, 12.1]
    adapter_good = [0.1] * 15
    data = {
        "report_multiqc_version": "1.21",
        "report_general_stats_data": [
            {
                "mqc_sample1": {
                    "FastQC_percent_duplicates": 74.2,
                    "FastQC_percent_gc": 41.0,
                    "FastQC_avg_sequence_length": 150,
                    "FastQC_total_sequences": 1200000,
                },
                "mqc_sample2": {
                    "FastQC_percent_duplicates": 11.3,
                    "FastQC_percent_gc": 43.5,
                    "FastQC_avg_sequence_length": 150,
                    "FastQC_total_sequences": 1500000,
                },
            }
        ],
        "report_plot_data": {
            "fastqc_per_base_sequence_quality_plot": {
                "datasets": [
                    {
                        "mqc_sample1": {str(i + 1): q for i, q in enumerate(quality_bad)},
                        "mqc_sample2": {str(i + 1): q for i, q in enumerate(quality_good)},
                    }
                ]
            },
            "fastqc_adapter_content_plot": {
                "datasets": [
                    {
                        "mqc_sample1": {str(i + 1): a for i, a in enumerate(adapter_good)},
                        "mqc_sample2": {str(i + 1): a for i, a in enumerate(adapter_bad)},
                    }
                ]
            },
        },
    }
    return json.dumps(data, indent=2) + "\n"


def make_multiqc_report_html() -> str:
    """A minimal but structurally real report: the General Statistics table."""
    return """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>MultiQC Report</title></head>
<body>
<h1>MultiQC v1.21</h1>
<h2>General Statistics</h2>
<table id="general_stats_table">
<tr><th>Sample Name</th><th>% Dups</th><th>% GC</th><th>Length</th><th>M Seqs</th><th>% Failed</th></tr>
<tr><td>html_sample1</td><td>66.4%</td><td>44.0%</td><td>150 bp</td><td>18.5</td><td>9.1%</td></tr>
<tr><td>html_sample2</td><td>10.2%</td><td>45.1%</td><td>150 bp</td><td>19.2</td><td>0.0%</td></tr>
</table>
</body>
</html>
"""


def make_snakemake_log() -> str:
    return """Snakemake v8.16.0
Building DAG of jobs...
Using shell: /bin/bash
[Wed Sep 24 10:12:01 2026]
rule align:
    input: data/sample1_R1.fastq.gz, data/sample1_R2.fastq.gz
    output: results/sample1.bam
    jobid: 3
    reason: Missing output files: results/sample1.bam
    resources: mem_mb=32000, runtime=120

[Wed Sep 24 10:14:33 2026]
Finished job 3.
1 of 4 steps (25%) done

[Wed Sep 24 10:14:34 2026]
rule count:
    input: results/sample1.bam
    output: results/sample1.counts.txt
    jobid: 5

[Wed Sep 24 10:15:02 2026]
Error in rule count:
    jobid: 5
    input: results/sample1.bam
    output: results/sample1.counts.txt
    shell:
        featureCounts -a /refs/genes.gtf -o results/sample1.counts.txt results/sample1.bam
        (one of the commands exited with non-zero exit code 7; note that snakemake uses bash strict mode!)

Exiting because a job execution failed
Complete log: /home/user/project/.snakemake/log/2026-09-24T101201.123456.snakemake.log
Shutting down, this might take some time
"""


def make_cromwell_log() -> str:
    return """[2026-09-24 10:12:01,234] [info] Cromwell 86 (service::cromwell)
[2026-09-24 10:12:01,240] [info] Starting Cromwell workflow engine (Horror Show edition)
[2026-09-24 10:12:02,111] [info] WorkflowManagerActor Workflow 8f3a2b1c submitted to Cromwell
[2026-09-24 10:12:02,115] [info] WDL workflow rnaseq version 1.0: 3 calls, 0 scatter blocks
[2026-09-24 10:12:02,120] [info] WorkflowActor-8f3a2b1c: Status change from Submitted to Running, workflowId=8f3a2b1c-4d2e-4f1a-9c3b-7e6a5d4c3b2a
[2026-09-24 10:12:02,300] [info] Call cache lookup started for call AlignTask.rnaseq
[2026-09-24 10:12:02,305] [info] call AlignTask: cache miss, executing in docker image quay.io/biocontainers/hisat2:2.2.1
[2026-09-24 10:47:19,881] [info] call AlignTask: execution failed (exit code 1): hisat2: ERR: missing index files
[2026-09-24 10:47:19,890] [error] WorkflowActor-8f3a2b1c: Workflow failed: one or more calls failed
[2026-09-24 10:47:19,891] [info] Workflow complete. Total time: 35m 17s
Final Outputs:
{
  "rnaseq.bam": null
}
"""


def make_wdl_source() -> str:
    return """version 1.0

workflow rnaseq {
  input {
    File fastq_r1
    File fastq_r2
  }

  call AlignTask {
    input:
      fastq_r1 = fastq_r1,
      fastq_r2 = fastq_r2
  }

  output {
    File bam = AlignTask.bam
  }
}

task AlignTask {
  input {
    File fastq_r1
    File fastq_r2
  }

  command <<<
    hisat2 -x /refs/grch38 -1 ~{fastq_r1} -2 ~{fastq_r2} | samtools sort -o aligned.bam
  >>>

  output {
    File bam = "aligned.bam"
  }

  runtime {
    docker: "quay.io/biocontainers/hisat2:2.2.1"
    memory: "32 GB"
  }
}
"""


# ==========================================================================
# Run folders (Station 6)
# ==========================================================================
def _samplesheet(paired: bool = True) -> str:
    if paired:
        rows = "".join(
            f"sample{i},/data/sample{i}_R1.fastq.gz,/data/sample{i}_R2.fastq.gz,reverse\n"
            for i in range(1, 4)
        )
        return "sample,fastq_1,fastq_2,strandedness\n" + rows
    rows = "".join(f"sample{i},/data/sample{i}.fastq.gz\n" for i in range(1, 4))
    return "sample,fastq\n" + rows


def _star_log_final(unique_pct: float, input_reads: int = 40_000_000) -> str:
    mapped = int(input_reads * unique_pct / 100)
    return (
        "                                 Started job on |       Jun 01 09:12:33\n"
        "                             Started mapping on |       Jun 01 09:14:02\n"
        f"                                    Input reads |       {input_reads}\n"
        f"                                Average input read length |       148\n"
        "                                    Uniquely mapped reads number |       "
        f"{mapped}\n"
        f"                                     Uniquely mapped reads % |       {unique_pct:.2f}%\n"
        "                                       Average mapped length |       146\n"
        "                                  Number of splices: Total |       41233881\n"
        "                       Number of reads mapped to multiple loci |       1203948\n"
        "                              % of reads mapped to multiple loci |       3.01%\n"
        "                              % of reads mapped to too many loci |       0.02%\n"
        "                              % of reads unmapped: too many mismatches |       1.10%\n"
        "                                  % of reads unmapped: too short |       0.40%\n"
        "                                      % of reads unmapped: other |       1.02%\n"
    )


def _featurecounts_summary(assigned: int, unassigned: int, paired: bool) -> str:
    total = assigned + unassigned
    lines = [
        "Status\tsample_counts/sample1.bam\n",
        f"Assigned\t{assigned}\n",
        f"Unassigned_Unmapped\t{int(unassigned * 0.45)}\n",
        f"Unassigned_NoFeatures\t{int(unassigned * 0.25)}\n",
        f"Unassigned_Ambiguity\t{int(unassigned * 0.20)}\n",
        f"Unassigned_MappingQuality\t{int(unassigned * 0.07)}\n",
        f"Unassigned_Chimera\t{int(unassigned * 0.03)}\n",
        "__no_feature\t31000\n",
        "__ambiguous\t12000\n",
    ]
    if paired:
        lines.append(f"Number of pairs\t{total}\n")
    else:
        lines.append(f"Number of reads\t{total}\n")
    return "".join(lines)


def _reads_per_gene(unstranded: int, forward: int, reverse: int) -> str:
    rows = [
        "N_unmapped\t1200000\t1200000\t1200000\n",
        "N_multimapping\t830000\t830000\t830000\n",
        "N_noFeature\t310000\t410000\t405000\n",
        "N_ambiguous\t12000\t14000\t13500\n",
    ]
    genes = [
        "ENSG00000000003",
        "ENSG00000000005",
        "ENSG00000000419",
        "ENSG00000000457",
        "ENSG00000000460",
        "ENSG00000000938",
        "ENSG00000000971",
        "ENSG00000001036",
        "ENSG00000001084",
        "ENSG00000001167",
        "ENSG00000001460",
        "ENSG00000001461",
    ]
    # Spread the three columns so the totals hit the requested numbers.
    per = len(genes)
    for i, g in enumerate(genes):
        weight = 1.0 + (i % 5)
        rows.append(
            f"{g}\t{int(unstranded / per * weight)}\t{int(forward / per * weight)}\t"
            f"{int(reverse / per * weight)}\n"
        )
    return "".join(rows)


def _dup_metrics(value: float) -> str:
    return (
        "## htsjdk.samtools.metrics.StringHeader\n"
        "# picard.sam.markduplicates.MarkDuplicates INPUT=[sample1.bam] "
        "OUTPUT=sample1.dedup.bam METRICS_FILE=sample1.MarkDuplicates.metrics.txt\n"
        "## METRICS CLASS\tpicard.sam.DuplicationMetrics\n"
        "LIBRARY\tUNPAIRED_READS_EXAMINED\tREAD_PAIRS_EXAMINED\tUNMAPPED_READS\t"
        "UNPAIRED_READ_DUPLICATES\tREAD_PAIR_DUPLICATES\tPERCENT_DUPLICATION\t"
        "ESTIMATED_LIBRARY_SIZE\n"
        f"sample1\t1203948\t20139844\t12033\t{int(value * 100000)}\t{int(value * 1800000)}\t"
        f"{value:.6f}\t48222991\n"
    )


def _selfsm(freemix: float) -> str:
    return (
        "#SEQ_ID\tRG\tCHIP_ID\tSNPS\tREADS\tAVG_DP\tFREEMIX\tFREELK1\tFREELK0\t"
        "FREE_RH\tFREE_RA\tCHIPMIX\tCHIPLK1\tCHIPLK0\tCHIP_RH\tCHIP_RA\t"
        "DPREF\n"
        f"sample1\t-\t-\t12441\t1029334\t18.32\t{freemix:.6f}\t{freemix - 0.001:.6f}\t"
        f"{freemix + 0.001:.6f}\t-\t-\t0.0\t0.0\t0.0\t-\t-\t-\n"
    )


def _insert_size(median: int) -> str:
    return (
        "## METRICS CLASS\tpicard.analysis.InsertSizeMetrics\n"
        "MEDIAN_INSERT_SIZE\tMODE_INSERT_SIZE\tMEAN_INSERT_SIZE\tSTANDARD_DEVIATION\n"
        f"{median}\t{median}\t{median + 12:.1f}\t62.4\n"
    )


def _gtf(contigs: list[str], n_rows: int = 400) -> str:
    lines = [
        "#!genome-build GRCh38\n" if contigs[0].startswith("chr") else "#!genome-build GRCh38\n"
    ]
    for i in range(n_rows):
        c = contigs[i % len(contigs)]
        start = 1000 + i * 500
        lines.append(
            f"{c}\tENSEMBL\texon\t{start}\t{start + 250}\t.\t+\t.\t"
            f'gene_id "ENSG{i:011d}"; transcript_id "ENST{i:011d}"; gene_name "GENE{i}";\n'
        )
    return "".join(lines)


def _execution_trace(processes: list[tuple[str, int, str]], path: Path) -> None:
    header = "task_id\thash\tnative_id\tname\tstatus\texit\trealtime\t%cpu\t%mem\trss\tvmem\n"
    rows = [header]
    for i, (name, exit_code, status) in enumerate(processes, start=1):
        rows.append(
            f"{i}\tabc{i:04d}\t{i}\t{name}\t{status}\t{exit_code}\t12.3s\t"
            f"99.1%\t1.2%\t1.1 GB\t2.4 GB\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(rows), encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_clean_run() -> None:
    root = FIX / "runs" / "clean_run"
    if root.exists():
        shutil.rmtree(root)
    _write(root / "samplesheet.csv", _samplesheet(paired=True))
    _write(
        root / "run_config.yaml",
        "genome: GRCh38\n"
        "annotation: /refs/GRCh38/gencode.v45.annotation.gtf\n"
        "star_index: /refs/GRCh38/star_index\n"
        "strandedness: reverse\n"
        "protocol: paired_end\n",
    )
    _write(root / "annotation" / "gencode.v45.annotation.gtf", _gtf(UCSC_CONTIGS))
    _write(root / "star" / "sample1" / "Log.final.out", _star_log_final(92.5))
    _write(
        root / "star" / "sample1" / "ReadsPerGene.out.tab",
        _reads_per_gene(unstranded=31_000_000, forward=600_000, reverse=30_400_000),
    )
    _write(
        root / "counts" / "sample1.featureCounts.txt.summary",
        _featurecounts_summary(assigned=7_800_000, unassigned=2_200_000, paired=True),
    )
    _write(
        root / "counts" / "sample1.featureCounts.txt",
        "Geneid\tChr\tStart\tEnd\tStrand\tLength\tsample1.bam\n"
        + "".join(
            f"ENSG{i:011d}\tchr1\t{1000 + i * 500}\t{1250 + i * 500}\t+\t1200\t{500 + i * 7}\n"
            for i in range(50)
        ),
    )
    bam = root / "star" / "sample1" / "Aligned.sortedByCoord.out.bam"
    bam.parent.mkdir(parents=True, exist_ok=True)
    bam.write_bytes(make_minimal_bam(UCSC_CONTIGS, eof=True))
    _write(root / "qc" / "sample1.MarkDuplicates.metrics.txt", _dup_metrics(0.081))
    _write(root / "qc" / "sample2.MarkDuplicates.metrics.txt", _dup_metrics(0.093))
    _write(root / "qc" / "sample3.MarkDuplicates.metrics.txt", _dup_metrics(0.075))
    _write(root / "qc" / "sample1.verify_bam.selfSM", _selfsm(0.0021))
    _write(root / "qc" / "sample1.insert_size_metrics.txt", _insert_size(212))
    _write(
        root / "trim" / "sample1.cutadapt.log",
        "Total read pairs processed:         20,000,000\n"
        "  Read 1 with adapter:               1,120,334 (5.6%)\n"
        "Pairs written (passing filters):    19,880,112 (99.4%)\n"
        "Total basepairs processed: 3,000,000,000 bp\n"
        "  Quality-trimmed:               12,004,331 bp (0.4%)\n",
    )
    _execution_trace(
        [
            ("NFCORE_RNASEQ:RNASEQ:FASTQC_FASTQ:FASTQC", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:TRIM_CUTADAPT:CUTADAPT", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:QUANTIFY_FEATURECOUNTS:FEATURECOUNTS", 0, "COMPLETED"),
        ],
        root / "pipeline_info" / "execution_trace_2026-06-01.txt",
    )
    _write(root / "nextflow.log", make_nextflow_ok_log())
    make_fastqc_zip(
        root / "fastqc" / "sample1_fastqc.zip",
        sample="sample1",
        quality=[36.0] * 15,
        adapter=[0.1] * 15,
        dedup_pct=92.0,
        n_percents=[0.0] * 15,
        length="150",
    )


def make_contig_mismatch_run() -> None:
    """Every exit code is 0, but the BAM and the annotation disagree.

    BAM contigs are Ensembl-style (1, 2, ... MT); the GTF used for counting is
    UCSC-style (chr1, chr2, ... chrM). Nothing crashes: reads simply fail to
    assign to features, so the assignment rate collapses to 18%.
    """
    root = FIX / "runs" / "contig_mismatch"
    if root.exists():
        shutil.rmtree(root)
    _write(root / "samplesheet.csv", _samplesheet(paired=True))
    _write(
        root / "run_config.yaml",
        "genome: GRCh38\n"
        "annotation: /refs/GRCh38/gencode.v45.annotation.gtf\n"
        "star_index: /refs/GRCh38/star_index_ensembl\n"
        "strandedness: reverse\n"
        "protocol: paired_end\n",
    )
    _write(root / "annotation" / "gencode.v45.annotation.gtf", _gtf(UCSC_CONTIGS))
    _write(root / "star" / "sample1" / "Log.final.out", _star_log_final(88.4))
    _write(
        root / "star" / "sample1" / "ReadsPerGene.out.tab",
        _reads_per_gene(unstranded=31_000_000, forward=700_000, reverse=30_300_000),
    )
    _write(
        root / "counts" / "sample1.featureCounts.txt.summary",
        _featurecounts_summary(assigned=1_800_000, unassigned=8_200_000, paired=True),
    )
    _write(
        root / "counts" / "sample1.featureCounts.txt",
        "Geneid\tChr\tStart\tEnd\tStrand\tLength\tsample1.bam\n"
        + "".join(
            f"ENSG{i:011d}\tchr1\t{1000 + i * 500}\t{1250 + i * 500}\t+\t1200\t{80 + i}\n"
            for i in range(50)
        ),
    )
    bam = root / "star" / "sample1" / "Aligned.sortedByCoord.out.bam"
    bam.parent.mkdir(parents=True, exist_ok=True)
    bam.write_bytes(make_minimal_bam(ENSEMBL_CONTIGS, eof=True))
    _write(root / "qc" / "sample1.MarkDuplicates.metrics.txt", _dup_metrics(0.091))
    _write(root / "qc" / "sample2.MarkDuplicates.metrics.txt", _dup_metrics(0.088))
    _write(root / "qc" / "sample3.MarkDuplicates.metrics.txt", _dup_metrics(0.102))
    _write(root / "qc" / "sample1.verify_bam.selfSM", _selfsm(0.0032))
    _write(root / "qc" / "sample1.insert_size_metrics.txt", _insert_size(208))
    _write(
        root / "trim" / "sample1.cutadapt.log",
        "Total read pairs processed:         20,000,000\n"
        "  Read 1 with adapter:               1,240,001 (6.2%)\n"
        "Pairs written (passing filters):    19,760,004 (98.8%)\n",
    )
    _execution_trace(
        [
            ("NFCORE_RNASEQ:RNASEQ:FASTQC_FASTQ:FASTQC", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:TRIM_CUTADAPT:CUTADAPT", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:QUANTIFY_FEATURECOUNTS:FEATURECOUNTS", 0, "COMPLETED"),
        ],
        root / "pipeline_info" / "execution_trace_2026-06-02.txt",
    )
    _write(root / "nextflow.log", make_nextflow_ok_log())
    make_fastqc_zip(
        root / "fastqc" / "sample1_fastqc.zip",
        sample="sample1",
        quality=[36.0] * 15,
        adapter=[0.2] * 15,
        dedup_pct=90.0,
        n_percents=[0.0] * 15,
        length="150",
    )


def make_strand_mismatch_run() -> None:
    """Declared reverse-stranded; the ReadsPerGene columns say otherwise."""
    root = FIX / "runs" / "strand_mismatch"
    if root.exists():
        shutil.rmtree(root)
    _write(root / "samplesheet.csv", _samplesheet(paired=True))
    _write(
        root / "run_config.yaml",
        "genome: GRCh38\n"
        "annotation: /refs/GRCh38/gencode.v45.annotation.gtf\n"
        "strandedness: reverse\n"
        "protocol: paired_end\n",
    )
    _write(root / "annotation" / "gencode.v45.annotation.gtf", _gtf(UCSC_CONTIGS))
    _write(root / "star" / "sample1" / "Log.final.out", _star_log_final(90.1))
    # forward ~ reverse -> the library carries no strand information at all.
    _write(
        root / "star" / "sample1" / "ReadsPerGene.out.tab",
        _reads_per_gene(unstranded=31_000_000, forward=15_400_000, reverse=15_600_000),
    )
    _write(
        root / "counts" / "sample1.featureCounts.txt.summary",
        _featurecounts_summary(assigned=7_100_000, unassigned=2_900_000, paired=True),
    )
    bam = root / "star" / "sample1" / "Aligned.sortedByCoord.out.bam"
    bam.parent.mkdir(parents=True, exist_ok=True)
    bam.write_bytes(make_minimal_bam(UCSC_CONTIGS, eof=True))
    _write(root / "qc" / "sample1.MarkDuplicates.metrics.txt", _dup_metrics(0.085))
    _write(root / "qc" / "sample1.verify_bam.selfSM", _selfsm(0.0018))
    _execution_trace(
        [
            ("NFCORE_RNASEQ:RNASEQ:ALIGN_STAR:STAR_ALIGN", 0, "COMPLETED"),
            ("NFCORE_RNASEQ:RNASEQ:QUANTIFY_FEATURECOUNTS:FEATURECOUNTS", 0, "COMPLETED"),
        ],
        root / "pipeline_info" / "execution_trace_2026-06-03.txt",
    )
    _write(root / "nextflow.log", make_nextflow_ok_log())


# ==========================================================================
# main
# ==========================================================================
def main() -> None:
    # --- Station 1 fixtures ------------------------------------------------
    make_fastqc_zip(
        FIX / "fastqc" / "sample_fastqc.zip",
        sample="sample_R1",
        quality=[
            35.8,
            35.9,
            35.7,
            35.5,
            35.2,
            34.8,
            34.1,
            33.0,
            31.2,
            28.4,
            25.1,
            21.3,
            17.8,
            14.2,
            11.6,
        ],
        adapter=[0.0, 0.0, 0.1, 0.1, 0.2, 0.3, 0.5, 0.9, 1.6, 2.8, 4.2, 5.6, 6.6, 7.2, 7.4],
        dedup_pct=42.0,
        n_percents=[0.0, 0.02, 0.03, 0.05, 12.4, 11.9, 0.4, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        length="150",
        statuses={
            "Per base sequence quality": "fail",
            "Adapter Content": "warn",
            "Sequence Duplication Levels": "warn",
            "Per base N content": "fail",
        },
    )
    make_fastqc_zip(
        FIX / "fastqc" / "clean_fastqc.zip",
        sample="clean_R1",
        quality=[36.0] * 15,
        adapter=[0.05] * 15,
        dedup_pct=93.0,
        n_percents=[0.0] * 15,
        length="150",
    )
    make_fastqc_zip(
        FIX / "fastqc" / "messy_fastqc.zip",
        sample="messy_R1",
        quality=[
            30.0,
            30.2,
            29.8,
            28.9,
            27.4,
            25.1,
            22.6,
            19.8,
            17.1,
            15.0,
            13.4,
            12.1,
            11.0,
            10.2,
            9.4,
        ],
        adapter=[0.1, 0.2, 0.4, 0.8, 1.4, 2.2, 3.4, 5.1, 8.2, 12.4, 18.1, 24.6, 31.2, 37.4, 41.0],
        dedup_pct=26.0,
        gc_counts=[round(1000 * _gauss(i, 45, 6), 3) for i in range(101)],
        n_percents=[0.1, 0.2, 0.3, 24.5, 22.1, 0.5, 0.2, 0.1, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        length="35-150",
        statuses={
            "Per base sequence quality": "fail",
            "Adapter Content": "fail",
            "Sequence Duplication Levels": "fail",
            "Per base N content": "fail",
            "Sequence Length Distribution": "warn",
        },
    )
    # GC spike fixture: a second organism-shaped peak at GC 72.
    gc_spike = [round(1000 * _gauss(i, 45, 6), 3) for i in range(101)]
    gc_spike[72] = 26000.0
    gc_spike[71] = 400.0
    gc_spike[73] = 500.0
    make_fastqc_zip(
        FIX / "fastqc" / "gc_spike_fastqc.zip",
        sample="gcspike_R1",
        quality=[35.5] * 15,
        adapter=[0.1] * 15,
        dedup_pct=88.0,
        gc_counts=gc_spike,
        n_percents=[0.0] * 15,
        length="150",
        statuses={"Per sequence GC content": "fail"},
    )

    make_station10_vcfs()
    _write(FIX / "vcf" / "sample.vcf", VCF_BODY)
    _write(FIX / "vcf" / "mystery.txt", VCF_BODY)  # TRAP: VCF named .txt
    _write(FIX / "vcf" / "vcf_no_extension", VCF_BODY)  # TRAP: no extension
    (FIX / "vcf").mkdir(parents=True, exist_ok=True)
    (FIX / "vcf" / "sample.vcf.gz").write_bytes(gzip.compress((FIX / "vcf" / "clean.vcf").read_bytes(), mtime=0))  # gzipped VCF

    (FIX / "bam").mkdir(parents=True, exist_ok=True)
    (FIX / "bam" / "truncated.bam").write_bytes(
        make_minimal_bam(UCSC_CONTIGS, eof=False, truncated=True)
    )
    (FIX / "bam" / "complete.bam").write_bytes(make_minimal_bam(UCSC_CONTIGS, eof=True))

    _write(FIX / "logs" / "nextflow.log", make_nextflow_ok_log())
    _write(FIX / "logs" / "nextflow_star_index.log", make_star_index_fail_log())
    _write(FIX / "logs" / "nextflow_warnings.log", make_warnings_then_fatal_log())
    _write(FIX / "logs" / "nextflow_nomatch.log", make_nomatch_log())
    _write(FIX / "logs" / "snakemake.log", make_snakemake_log())
    _write(FIX / "logs" / "cromwell.log", make_cromwell_log())
    growing=FIX/"logs"/"growing"
    _write(growing/"nextflow.early", _nextflow_banner()+"WARN: retrying a transient task\nWARN: waiting for a scheduled executor\n")
    _write(growing/"nextflow.part1", _nextflow_banner()+"WARN: retrying a transient task\njava.lang.OutOfMemoryError: Java heap space\n")
    _write(growing/"nextflow.part2", _nextflow_banner()+"WARN: retrying a transient task\njava.lang.OutOfMemoryError: Java heap space\nExecution cancelled\n")
    _write(growing/"clean.part", make_nextflow_ok_log())
    _write(growing/"truncated.part", _nextflow_banner()+"Command error:\njava.lang.OutOfMemoryError: Java heap")
    sm=FIX/"logs"/"snakemake"
    sm_cases={"missing_input":"MissingInputException: missing reads.fastq.gz\n", "ambiguous_rule":"AmbiguousRuleException: target output matches rules a and b\n", "unknown_target":"No rule to produce target.xyz\n", "wildcards":"WildcardError: Wildcards in input files cannot be determined\n", "conda":"ResolvePackageNotFound: missing package\n", "job_failed":"Error in rule align:\n(one of the commands exited with non-zero exit code 7)\n", "incomplete":"Removing output files of failed job: marked as incomplete\n", "cycle":"CyclicGraphException: cycle in the graph\n", "no_match":"Snakemake started workflow successfully\nBuilding DAG of jobs...\n"}
    for name,content in sm_cases.items(): _write(sm/(name+".log"),content)
    cw=FIX/"logs"/"cromwell"
    cw_cases={"failed_call":"Call AlignTask failed\n", "shard_retry":"Shard 2 failed after retries exhausted\n", "backend":"Backend error: failed to submit job\n", "localization":"Failed to localize input file\n", "capture":"Failed to read task stdout file\n", "no_match":"Cromwell workflow is running\n"}
    for name,content in cw_cases.items(): _write(cw/(name+".log"),content)
    _write(FIX / "wdl" / "example.wdl", make_wdl_source())

    _write(FIX / "multiqc" / "multiqc_general_stats.txt", make_multiqc_general_stats())
    _write(FIX / "multiqc" / "clean_general_stats.txt", make_multiqc_clean_general_stats())
    _write(FIX / "multiqc" / "multiqc_data.json", make_multiqc_data_json())
    _write(FIX / "multiqc" / "multiqc_report.html", make_multiqc_report_html())

    _write(
        FIX / "misc" / "plain.txt",
        "This is an ordinary text file.\n"
        "It mentions sequencing, adapters and quality scores,\n"
        "but it is not any recognised NGS file format.\n",
    )
    (FIX / "misc").mkdir(parents=True, exist_ok=True)
    (FIX / "misc" / "empty.txt").write_bytes(b"")
    (FIX / "misc" / "garbage.bin").write_bytes(bytes(range(256)) * 4)

    # A folder containing a mix of the above.
    folder = FIX / "folder"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    for src, dest in [
        (FIX / "fastqc" / "sample_fastqc.zip", "sample_fastqc.zip"),
        (FIX / "vcf" / "mystery.txt", "mystery.txt"),
        (FIX / "vcf" / "sample.vcf", "sample.vcf"),
        (FIX / "bam" / "truncated.bam", "truncated.bam"),
        (FIX / "logs" / "nextflow.log", "nextflow.log"),
        (FIX / "misc" / "plain.txt", "notes.txt"),
        (FIX / "misc" / "empty.txt", "empty.txt"),
    ]:
        shutil.copy2(src, folder / dest)

    # --- Station 6 run folders --------------------------------------------
    make_clean_run()
    make_contig_mismatch_run()
    make_strand_mismatch_run()

    print(f"Fixtures written under {FIX}")


if __name__ == "__main__":
    main()
