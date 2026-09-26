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
| Snakemake log, Cromwell/WDL log, WDL source | Recognised by content; full diagnosis planned (below) |
| VCF, BAM alone, anything else | Honest "I can't interpret this yet" — never a guess |

## Next

Ordered by demand. Each item ships with fixtures, receipts, and the same
template-only plain language as everything above.

1. **VCF interpretation (variant QC first).** The most requested input. First
   cut judges call quality, not biology: depth distribution, missingness,
   Ti/Tv balance, het/hom ratio, filter-flag profile — with per-file receipts
   like every other verdict. Clinical prioritisation and ACMG classification
   stay out of scope (see below).
2. **Snakemake log diagnosis.** Failure signatures for the Snakemake runner in
   the same ranked style as the Nextflow set: the one root cause with line
   numbers, or honest unknown with the tail attached.
3. **Cromwell/WDL log diagnosis.** Same treatment for Cromwell execution logs:
   failed calls, shard failures, backend errors, localisation faults.

## Later

These are real gaps, recorded with their reasons rather than quietly ignored:

* **CRAM support** — the audit's completeness check reads the BAM end marker;
  CRAM needs its own.
* **Watch mode** — judging a log that is still being written, for runs that
  take days.
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
