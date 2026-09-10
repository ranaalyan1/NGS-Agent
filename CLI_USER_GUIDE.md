# NGS‑Agent CLI – User Guide

> **Goal:** Make the NGS‑Agent command‑line interface as easy and frictionless as possible for bench‑researchers, post‑docs, core‑facility staff, and developers. This guide walks through installation, the new *quick* command, and the existing commands, with many examples so you can start analyzing data in minutes.

---

## Table of Contents

1. [Quick Install](#quick-install)
2. [New `quick` Command – One‑Line RNA‑Seq](#new-quick-command---one-line-rna-seq)
3. [Existing Commands (Brief Overview)](#existing-commands-brief-overview)
4. [Full `submit` Command – When You Need More Control](#full-submit-command---when-you-need-more-control)
5. [Batch Operations (`submit‑batch` + `wizard`)](#batch-operations-submit-batch--wizard)
6. [Configuration & Environment](#configuration--environment)
7. [Troubleshooting & FAQ](#troubleshooting--faq)
8. [Changelog (what changed for simplicity)](#changelog-what-changed-for-simplicity)

---

## 1. Quick Install

The CLI is a regular Python script that requires a few packages. The easiest way is to install the project's requirements once:

```bash
# From the repository root
cd NGS-Agent
pip install --break-system-packages -r requirements.txt
```

> **What’s inside `requirements.txt`?**  
> `click`, `temporalio`, `boto3`, `python-dotenv`, and a few other bio‑informatics utils. If you already have a Python environment with these packages, you can skip the install step.

### Verify the installation

```bash
python cli.py --help
```

You should see a list of commands similar to:

```
Commands:
  quick         Quick submit a single pipeline run with minimal flags.
  status        Get status of a run.
  submit        Submit a single pipeline run.
  submit-batch  Submit a batch pipeline run using a CSV sample sheet.
  wizard        Interactive setup wizard for batch analysis.
```

---

## 2. New `quick` Command – One‑Line RNA‑Seq

### Purpose

Submit a **single‑sample RNA‑Seq** run with the absolute minimum of typing. The command picks sensible defaults:

| Parameter | Default |
|-----------|---------|
| Experiment type | `RNA‑Seq` |
| Organism | `human` |
| Reference genome | `hg38` (human) / `mm10` (mouse) |
| Paired‑end | `False` (single‑end) |
| GTF / reference‑FASTA | omitted (counting step is skipped) |

### Syntax

```bash
python cli.py quick --fastq <PATH_TO_FASTQ> [--organism <SPECIES>]
```

| Flag | Description | Required? |
|------|-------------|-----------|
| `--fastq` | Path to a **single‑end** FASTQ file | **Yes** |
| `--organism` | Species: `human`, `mouse`, `rat`, `zebrafish`, `yeast`, `other`. Default: `human` | No |

### Examples

```bash
# Minimal – just the FASTQ file (human, hg38)
python cli.py quick --fastq data.fastq

# Choose mouse (mm10 reference)
python cli.py quick --fastq data.fastq --organism mouse

# If you have a custom organism, it will fall back to hg38
python cli.py quick --fastq data.fastq --organism other
```

### What happens under the hood

1. **Validate** that the FASTQ file exists.
2. **Derive** the reference genome from the organism (`hg38` / `mm10`).
3. **Connect** to the Temporal server (default `localhost:7233`).
4. **Start** the `NGSPipelineWorkflow` with a generated `run‑id`.
5. **Print** a monitoring URL, e.g.:

```
Quick run submitted: run-3f9a2c1d
Monitor at http://localhost:8080/namespaces/default/workflows/ngs-run-3f9a2c1d
```

> **Note:** The Temporal server must be reachable. In a local development setup you typically have it running; otherwise you’ll get a “Connection refused” error – that’s expected in this sandbox.

### When to use `quick` vs. `submit`

| Situation | Recommendation |
|-----------|----------------|
| You just want a **quick smoke test** of a new FASTQ file. | `quick` – one flag only. |
| You need **DNA‑Seq (WGS/WES)**, **paired‑end**, **GTF‑based counting**, or **custom references**. | Use `submit` (see below). |
| You are running a **batch of many samples**. | Use `submit‑batch` or the `wizard`. |

---

## 3. Existing Commands (Brief Overview)

| Command | When to use | Minimal flags |
|---------|-------------|---------------|
| `status RUN_ID` | Check status / retrieve result of a previously submitted run. | `RUN_ID` (argument) |
| `submit` | Full‑featured single‑sample submission. | `--fastq` / `--fastq-r1` / `--fastq-r2`, `--organism`, `--ref-genome`, `[--reference-fasta]`, `[--gtf]`, `[--panel-bed]`, `[--known-sites]`, `[--paired/--single]` |
| `submit-batch` | Submit many samples at once via a CSV sheet. | `--sample-sheet`, `--organism`, `--ref-genome`, `[--reference-fasta]`, `[--gtf]`, `[--paired/--single]` |
| `wizard` | Interactive prompt that creates an `.env` and a sample‑sheet for you. | None (prompts you step‑by‑step) |

All of these commands share the same underlying Temporal workflow, so the monitoring URL pattern is consistent: `http://localhost:8080/namespaces/default/workflows/ngs-<run‑id>`.

---

## 4. Full `submit` Command – When You Need More Control

The original `submit` command remains unchanged for advanced use‑cases. Its help (run `python cli.py submit --help`) lists every option, but the most frequently used minimal subset is:

```bash
python cli.py submit \
    --fastq data.fastq \
    --organism human \
    --ref-genome hg38 \
    --experiment RNA-Seq
```

- **`--experiment`** chooses `RNA‑Seq`, `WGS`, or `WES`.
- **`--organism`** and **`--ref-genome`** let you pick any supported species/index.
- **`--gtf`**, **`--panel‑bed`**, **`--known‑sites`** are optional; they are only validated when you actually supply them.

Use `submit` when you need **full control** (e.g., DNA‑Seq with BQSR, custom GTF‑based gene counting, or multi‑panel experiments).

---

## 5. Batch Operations (`submit‑batch` + `wizard`)

### `submit‑batch`

Run a batch analysis from a CSV sample sheet:

```bash
python cli.py submit-batch \
    --sample-sheet samples.csv \
    --organism human \
    --ref-genome hg38 \
    --paired
```

The CSV must have columns: `sample_id`, `condition`, `replicate_group`, `species`, `fastq`, `fastq_r1`, `fastq_r2`. The guide’s `wizard` command can generate this file for you.

### `wizard`

Start an interactive setup:

```bash
python cli.py wizard
```

You’ll be prompted for:

1. Analysis type (`RNA‑Seq`, `WGS`, `WES`)
2. Paired‑end? (yes/no)
3. Default organism (`hg38`, `mm10`, `mixed`)
4. Number of samples to configure
5. Per‑sample details (ID, condition, replicate, species, FASTQ paths)
6. Reference genome and GTF path

At the end, the wizard writes:

- **`.env`** file with key‑value pairs (experiment, organism, paired‑end, reference genome, GTF)
- **`sample_sheet.csv`** ready for `submit‑batch`

Then it prints the exact `cli.py submit-batch` command you should run.

---

## 6. Configuration & Environment

| Variable | Default | Where it’s used |
|----------|---------|-----------------|
| `TEMPORAL_HOST` | `localhost:7233` | All CLI commands that connect to Temporal |
| `.env` file | (created by `wizard`) | Loaded by `load_dotenv()` at script start |

You can override `TEMPORAL_HOST` environment‑wide, e.g.:

```bash
export TEMPORAL_HOST="my-temporal-instance.example.com:7233"
python cli.py quick --fastq data.fastq
```

If you frequently use a remote Temporal service, add the export to your shell profile (`~/.bashrc`, `~/.zshrc`) or create a persistent `.env` file in the project root.

---

## 7. Troubleshooting & FAQ

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `RuntimeError: Failed client connect: Connection refused` | Temporal server not running or `TEMPORAL_HOST` wrong. | Start a local Temporal server, or set `TEMPORAL_HOST` to the correct host:port. |
| `click.BadParameter: --fastq path does not exist: ...` | FASTQ file path typo or missing file. | Verify the path, create the file, or use an absolute path. |
| `Unrecognized argument: --organism` | Typo or unsupported species. | Use one of: `human`, `mouse`, `rat`, `zebrafish`, `yeast`, `other`. |
| `ModuleNotFoundError: No module named 'click'` | Packages not installed. | Run `pip install --break-system-packages -r requirements.txt`. |
| Want to run **paired‑end** with `quick`? | `quick` is single‑end only. | Use `submit` with `--fastq-r1` / `--fastq-r2` and `--paired`. |

### Getting help for any command

```bash
python cli.py <command> --help
```

Example:

```bash
python cli.py quick --help
python cli.py submit --help
```

---

## 8. Changelog – What Changed for Simplicity

| Change | Reason |
|--------|--------|
| **Added `quick` command** (single‑line RNA‑Seq) | Removes the need to remember 8‑10 flags for a routine RNA‑Seq run. |
| **Made `--gtf` optional in `submit`** | Researchers who don’t need counting can skip the GTF file entirely. |
| **Simplified help text** | All option descriptions now explicitly mark which are required vs. optional. |
| **Default organism & reference‑genome mapping** (`human → hg38`, `mouse → mm10`) | One fewer decision for the most common use‑case. |
| **`wizard` now writes `.env` + sample‑sheet automatically** | One‑step generation of the configuration needed for batch runs. |
| **Removed mandatory `RNA‑Seq +‑gtf` check** | Prevents an unnecessary roadblock when you just want a quick alignment‑only run. |

---

## 🎯 Quick Start Summary (one‑liner)

```bash
# 1️⃣ Install (once)
cd NGS-Agent && pip install --break-system-packages -r requirements.txt

# 2️⃣ Make sure you have a Temporal server reachable at localhost:7233
#    (or set TEMPORAL_HOST env var)

# 3️⃣ Submit a quick RNA‑Seq run
python cli.py quick --fastq /path/to/your_data.fastq

# 4️⃣ Monitor the run
#    → Open the URL printed, e.g. http://localhost:8080/namespaces/default/workflows/ngs-run-...
```

That’s it! You now have a frictionless pathway from FASTQ file to pipeline monitoring in a single command. Happy sequencing!