# NGS-Agent roadmap

What is covered today, what may be considered later, and what is permanently out of scope. This file answers "will it read my file type?" The tool points here when it recognises input it cannot yet judge.

## Covered today

| Input | What you get |
|---|---|
| FastQC report (`.zip`) | 6 QC rules; verdict: healthy / trim and proceed / re-sequence |
| Combined quality summary (MultiQC data JSON, general-stats table, report HTML) | Per-sample QC verdicts plus cohort checks; verdict: healthy / trim and proceed / review / re-sequence |
| Run folder | 10 audit rules across files; verdict: healthy / review / fix and re-run |
| Nextflow log | 10 failure signatures; one root cause or `UNKNOWN` |
| Nextflow log still being written | Read-only `--watch` polling; live snapshots and normal final verdict |
| VCF / `.vcf.gz` | Five call-quality checks; QC healthy or review, never variant truth |
| Snakemake log | 8 ranked failure signatures; root cause or `UNKNOWN` with tail |
| Cromwell run log | 5 failure signatures; root cause or `UNKNOWN` with tail |
| WDL source | Recognised; static analysis is out of scope |
| gVCF, VCF with more than one sample, multiallelic or visibly non-minimal VCF | Recognised, not judged; one sample is the documented limit |
| BAM alone, anything else | `UNKNOWN` — never a guess |

## Later

* **CRAM support** — the audit's completeness check reads the BAM end marker; CRAM needs its own.
* **Cohort-aware thresholds** — comparing one run against a lab's history instead of fixed cut-offs only.
* **More report charts** — adapter and GC curves alongside the quality curve.
* **`ngs --out DIR`** — writing the HTML report straight from the CLI.
* **Localisation** — English only today.

## Permanently out of scope

NGS-Agent interprets existing results; it is not a pipeline runner. It will not execute or orchestrate workflows, nor make network calls or use an LLM.

* **Variant interpretation** — pathogenicity, gene context, and ACMG classification are permanently out of scope. VCF call-quality QC is supported.
* Pipeline execution or orchestration (including Nextflow, Snakemake, containers, and schedulers).
* MCP server, accounts, authentication, or cloud upload.

The architecture stays local and deterministic: parsers extract evidence, rules evaluate it, and fixed templates explain the verdict. When evidence is insufficient, the result is `UNKNOWN`.
