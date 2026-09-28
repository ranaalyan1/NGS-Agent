# NGS-Agent CLI – User Guide

> **Goal:** one command shape, one answer. `ngs <path>` reads a quality-control
> report, a run folder, or a pipeline log and prints a plain-language verdict
> with receipts. This guide covers installing it, running it, and reading what
> it says. The same assessment code powers the web Box, so the verdict is
> identical either way.

---

## Table of Contents

1. [Install](#1-install)
2. [The command](#2-the-command)
3. [Options](#3-options)
4. [Supported inputs and verdicts](#4-supported-inputs-and-verdicts)
5. [Reading the output](#5-reading-the-output)
6. [Exit codes](#6-exit-codes)
7. [Watch mode](#7-watch-mode)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. Install

Python 3.11 or newer. Pick one:

```bash
# Isolated command-line install (recommended)
pipx install "ngs-agent[box] @ git+https://github.com/ranaalyan1/NGS-Agent.git"

# From a clone, editable (what the quickstart uses)
git clone https://github.com/ranaalyan1/NGS-Agent.git && cd NGS-Agent
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Conda
git clone https://github.com/ranaalyan1/NGS-Agent.git && cd NGS-Agent
conda env create -f environment-box.yml && conda activate ngs-agent-box

# Docker (Box on port 8000; the image also carries the CLI)
docker build -t ngs-agent .
docker run --rm -p 8000:8000 ngs-agent
```

`pyproject.toml` is the single source of truth for dependencies; there is no
separate requirements file. The install ships one console script, `ngs`.

Verify:

```bash
ngs --version
# ngs-agent 1.0.0 (ruleset 2026-09.1)
```

There is no configuration file and no file-type option: the tool identifies
its input by content.

---

## 2. The command

```bash
ngs <path>
```

`<path>` is one of:

| Input | Example |
|---|---|
| FastQC `.zip` | `ngs sample_fastqc.zip` |
| MultiQC JSON, table, or HTML | `ngs multiqc_data.json` |
| Run folder (alignment, counts, QC metrics) | `ngs results/sample1/` |
| Nextflow log | `ngs results/nextflow.log` |
| Snakemake log | `ngs .snakemake/log/run.log` |
| Cromwell run log | `ngs execution.log` |
| VCF or `.vcf.gz` (one sample) | `ngs calls.vcf.gz` |

Anything else — or a recognised file the rules cannot judge — comes back as
**Unknown**, never a guess. See [ROADMAP.md](ROADMAP.md) for what is covered,
what is recognised-only, and what is permanently out of scope.

Examples:

```bash
ngs fixtures/fastqc/sample_fastqc.zip          # verdict cards
ngs fixtures/runs/contig_mismatch              # cross-file audit
ngs sample_fastqc.zip --json                   # verdict + receipts as JSON
```

---

## 3. Options

| Option | Effect |
|---|---|
| `--json` | Print the full verdict, findings, and receipts as JSON instead of cards. |
| `--watch` | Poll a growing Nextflow log read-only; see [Watch mode](#7-watch-mode). |
| `--interval SECONDS`, `--poll-seconds SECONDS` | Watch poll interval; default 15. Must be greater than zero. |
| `--version` | Print tool version and ruleset version, then exit. |
| `-h`, `--help` | Usage, exit codes, examples. |

Colour follows the terminal and is disabled when `NO_COLOR` is set or output
is piped.

---

## 4. Supported inputs and verdicts

The verdict vocabulary is fixed (README has the full rules table):

| Input | Verdicts |
|---|---|
| FastQC `.zip` | healthy · trim and proceed · re-sequence |
| MultiQC summary | the same per sample, plus review for cohort findings |
| Run folder | healthy · review · fix and re-run |
| Nextflow / Snakemake / Cromwell log | one root cause with evidence, or honest unknown |
| VCF / `.vcf.gz` | call-quality QC only: healthy or review — never variant truth |
| Everything else | Unknown |

Scope guardrails the CLI will not break:

* No variant interpretation — no pathogenicity, gene context, or ACMG.
* No pipeline execution — the tool reads results, it never runs anything.
* No LLM, no network calls from the assessment code, no uploads.

---

## 5. Reading the output

Each finding carries a receipt: the rule or signature id, the file and line it
came from, and a content hash. For example:

```text
QC-QUAL-01: rule:QC-QUAL-01 @ Per base sequence quality — ruleset 2026-09.1
QC-QUAL-01: file:751e79c3d819 @ sample_fastqc/fastqc_data.txt:line=28
            — Per base sequence quality mean = 17.8 at position 40-49 (threshold Q20)
```

`--json` prints the same verdict and receipts machine-readable
(`verdict.tool_version`, `verdict.ruleset_version`, findings with receipts).
If evidence is insufficient, the verdict is **Unknown** rather than a guess.

Thresholds (duplication 20/50/70%, freemix 3/5%, alignment 75/50%,
assignment 30%) are defaults pending expert sign-off — see the Thresholds
section of the README.

---

## 6. Exit codes

| Code | Meaning |
|---:|---|
| `0` | Pass or warnings only |
| `1` | At least one failed finding |
| `2` | Input could not be interpreted |

---

## 7. Watch mode

Poll a growing Nextflow log every 15 seconds (change with `--interval`):

```bash
ngs --watch --interval 15 results/nextflow.log
```

The watcher reads without modifying and holds judgement on an incomplete final
line. Complete evidence triggers a `LIVE` verdict and a next action. Ctrl+C
prints a final snapshot (exit 1 if a failed finding has appeared, else 0). At
normal completion the final card matches one-shot `ngs results/nextflow.log`
output.

---

## 8. Troubleshooting

| Symptom | Meaning | Fix |
|---|---|---|
| Exit code `2`, verdict **Unknown** | The input is unsupported, or the rules cannot judge it with the evidence present. | See the verdict's reason and [ROADMAP.md](ROADMAP.md); unknown is a valid answer, not a crash. |
| `ngs: error: the following arguments are required: path` | The command needs a file or folder. | `ngs <path>` — the path is the only positional. |
| `--interval must be greater than zero` | Watch interval validation. | Pass a positive number of seconds. |
| A folder comes back Unknown | Nothing in it matched a known layout. | Check the sniffer kinds in the README's supported-inputs table. |
| Colours in logs/pipes | Not a bug. | Colour auto-disables on pipes; set `NO_COLOR` to force plain text. |

Everything the tool says is a template plus a measured number. If that is not
enough to act on, it says so instead of guessing.
