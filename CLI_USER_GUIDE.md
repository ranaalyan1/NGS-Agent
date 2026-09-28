# NGS-Agent CLI user guide

The CLI accepts one input path, identifies its format by content, and prints a plain-language verdict with evidence receipts. It does not execute a sequencing workflow or infer unsupported results. `UNKNOWN` is a valid outcome.

## Install

Python 3.11 or newer:

```bash
pipx install "ngs-agent[box] @ git+https://github.com/ranaalyan1/NGS-Agent.git"
```

For a source checkout and development install:

```bash
git clone https://github.com/ranaalyan1/NGS-Agent.git
cd NGS-Agent
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Both installs provide the same `ngs` command. The `box` extra includes the web interface dependencies. The source checkout also supports running the CLI as `python -m doors.cli`.

## Basic use

```bash
ngs sample_fastqc.zip
ngs results/run_folder
ngs results/nextflow.log
ngs sample.vcf
```

Machine-readable JSON, including findings and receipts:

```bash
ngs --json sample_fastqc.zip
```

The input can be a FastQC archive, MultiQC report, run folder, supported workflow log, or a supported single-sample VCF. See the [README supported-input table](README.md#supported-inputs) and [ROADMAP](ROADMAP.md) for exact limits.

## Watch a growing Nextflow log

```bash
ngs --watch --interval 15 results/nextflow.log
```

The watcher reads without modifying the log and waits for complete evidence before judging an incomplete final line. Ctrl+C displays a final snapshot.

## Verdicts and exit codes

A verdict can be healthy, require review or action, or be `UNKNOWN` when evidence is insufficient or the input is outside supported scope. The CLI does not guess.

| Exit code | Meaning |
|---:|---|
| `0` | Pass or warnings only |
| `1` | At least one failed finding |
| `2` | Input could not be interpreted |

Every finding includes its rule or signature identifier and source receipts. File receipts identify the evidence file, line or location, and content hash; rule receipts identify the rule and ruleset version.

## Scope

The product is a no-LLM results interpreter. Core has no network calls and the tool does not upload files or run pipelines. VCF support is call-quality QC only; pathogenicity, gene context, and ACMG interpretation are permanently out of scope. Thresholds are defaults and labs should tune them by assay type; see the [README Thresholds section](README.md#thresholds).

## Box

Install the `box` extra, then start the web interface from a source checkout:

```bash
.venv/bin/python -m uvicorn doors.gui.app:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`, drop in a supported file, and download the self-contained report. The Box and CLI use the same assessment path.
