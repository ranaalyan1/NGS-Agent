# NGS-Agent roadmap

What is covered today, what comes next, and what will never be in scope. This
file is the public answer to "will it read my file type?" — the tool itself
points here whenever it meets input it recognises but cannot judge yet.

## Covered today

| Input | What you get |
|---|---|
| FastQC report (`.zip`) | 6 QC rules; verdict: healthy / trim and proceed / re-sequence |
| Combined quality summary (MultiQC data JSON, general-stats table, report HTML) | Per-sample QC verdicts plus cohort checks; verdict: healthy / trim and proceed / review / re-sequence |
| Run folder | 10 audit rules across files; verdict: healthy / review / fix and re-run |
| Nextflow log | 10 failure signatures; verdict: the one root cause, or honest unknown |
| Nextflow log still being written | Read-only `--watch` polling; LIVE snapshots and normal final verdict |
| VCF / `.vcf.gz` | Five call-quality checks; QC healthy or review, never variant truth |
| Snakemake log | 8 ranked failure signatures; root cause or honest unknown with tail |
| Cromwell run log | 5 failure signatures; root cause or honest unknown with tail |
| WDL source | Recognised; static analysis is out of scope |
| gVCF, VCF with more than 1 sample, multiallelic or visibly non-minimal VCF | Recognised, not judged; one sample is the documented limit |
| BAM alone, anything else | Honest "I can't interpret this yet" — never a guess |

## Next

No queued parser stations at present. Future work is listed under Later.

## Later

These are real gaps, recorded with their reasons rather than quietly ignored:

* **CRAM support** — the audit's completeness check reads the BAM end marker;
  CRAM needs its own.
* **Cohort-aware thresholds** — comparing one run against your lab's history
  instead of fixed cut-offs only.
* **More report charts** — adapter and GC curves alongside the quality curve.
* **`ngs --out DIR`** — writing the HTML report straight from the CLI.
* **Localisation** — English only today.

## Never in scope (by design, not by accident)

NGS-Agent is an **interpreter**, not a runner. These will not be built here:

* running or orchestrating pipelines (Nextflow, Snakemake, Docker, Slurm);
* variant prioritisation or ACMG classification;
* multi-model debate or persona discussion;
* an MCP server, accounts, authentication, or any cloud upload.

The reasoning is architectural: the tool never executes anything, never sends
bytes anywhere, and every sentence it prints comes from a template plus a
number. Anything on this list would break one of those three properties.
