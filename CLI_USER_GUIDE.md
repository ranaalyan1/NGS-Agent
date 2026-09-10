# NGS‑Agent CLI – User Guide

> **Goal:** Make the NGS‑Agent command‑line interface as easy and frictionless as possible for bench‑researchers, post‑docs, core‑facility staff, and developers. This guide walks through installation, the *quick* command, and the other commands, with many examples so you can start analyzing data in minutes — **with no Temporal server required**.

---

## Table of Contents

1. [Quick Install](#quick-install)
2. [Local Mode vs. Temporal Mode](#local-mode-vs-temporal-mode)
3. [New `quick` Command – One‑Line RNA‑Seq](#new-quick-command---one-line-rna-seq)
4. [Existing Commands (Brief Overview)](#existing-commands-brief-overview)
5. [Full `submit` Command – When You Need More Control](#full-submit-command---when-you-need-more-control)
6. [Batch Operations (`submit‑batch` + `wizard`)](#batch-operations-submit-batch--wizard)
7. [Configuration & Environment](#configuration--environment)
8. [Troubleshooting & FAQ](#troubleshooting--faq)
9. [Changelog (what changed)](#changelog-what-changed)

---

## 1. Quick Install

The CLI is a regular Python script that requires a few packages. The easiest way is to install the project's requirements once:

```bash
# From the repository root
cd NGS-Agent
pip install -r requirements.txt
```

> **What’s inside `requirements.txt`?**  
> `click` and `python-dotenv` are all the **local** mode needs. `temporalio`, `redis`, `boto3`, and `anthropic` are marked optional and are only imported when you use Temporal mode, the shared cache, or LLM-backed decision steps (the agents fall back to deterministic heuristics without them).

### Verify the installation

```bash
python cli.py --help
```

You should see a list of commands similar to:

```
Commands:
  quick         Quick submit a single pipeline run with minimal flags.
  status        Get status of a run (local store by default).
  submit        Submit a single pipeline run.
  submit-batch  Submit a batch pipeline run using a CSV sample sheet.
  wizard        Interactive setup wizard for batch analysis.
```

---

## 2. Local Mode vs. Temporal Mode

The swarm pipeline has **two execution modes**:

| | Local mode (default) | Temporal mode (`--temporal`) |
|---|---|---|
| Orchestrator | In-process (`cli.py`) | Temporal server + `worker.py` |
| Extra services | Docker + MinIO only | + Temporal, Postgres, Redis |
| Run history | JSON files under `~/.ngsagent/runs` | Temporal workflow history + web UI |
| Best for | Lone researchers, laptops, small labs | Core facilities, durable audit trails |

Start the **local** infrastructure once:

```bash
docker compose -f docker-compose.lite.yml up -d   # MinIO only — no Temporal
bash scripts/build-agents.sh                      # build the per-tool agent images
```

Then every command below just works. No `python worker.py` process, no `localhost:7233`, no Postgres. To opt into Temporal for a single command, add `--temporal` (or set `NGS_MODE=temporal` and run `python worker.py`).

---

## 3. New `quick` Command – One‑Line RNA‑Seq

### Purpose

Submit a **single‑sample RNA‑Seq** run with the absolute minimum of typing. The command picks sensible defaults:

| Parameter | Default |
|-----------|---------|
| Experiment type | `RNA‑Seq` |
| Organism | `human` |
| Reference genome | `hg38` (human) / `mm10` (mouse) |
| Paired‑end | `False` (single‑end) |
| GTF / reference‑FASTA | omitted |
| Mode | `local` (no Temporal) |

### Syntax

```bash
python cli.py quick --fastq <PATH_TO_FASTQ> [--organism <SPECIES>]
```

| Flag | Description | Required? |
|------|-------------|-----------|
| `--fastq` | Path to a **single‑end** FASTQ file | **Yes** |
| `--organism` | Species: `human`, `mouse`, `rat`, `zebrafish`, `yeast`, `other`. Default: `human` | No |
| `--temporal` | Submit via Temporal instead of running locally | No |
| `--no-cache` | Disable the local content‑addressed cache | No |

### Examples

```bash
# Minimal – just the FASTQ file (human, hg38)
python cli.py quick --fastq data.fastq

# Choose mouse (mm10 reference)
python cli.py quick --fastq data.fastq --organism mouse
```

### What happens under the hood (local mode)

1. **Validate** that the FASTQ file exists.
2. **Derive** the reference genome from the organism (`hg38` / `mm10`).
3. **Run** the pipeline stages in‑process, one Docker agent per stage:
   `ingest → qc → ai_decider → align → count → de → insight → report_builder → report_agent`.
4. **Record** the run under `~/.ngsagent/runs/<run-id>.json` so `status` works.
5. **Print** a live progress trace and the final report location:

```
Quick run submitted: quick-3f9a2c1d (local mode — no Temporal server required)
  → ingest (sample sample-01)
  → qc (sample sample-01)
  ...
  Status: complete
  Samples processed: 1
  Report: s3://ngs-artifacts/quick-3f9a2c1d/report/index.html
```

### When to use `quick` vs. `submit`

| Situation | Recommendation |
|-----------|----------------|
| You just want a **quick smoke test** of a new FASTQ file. | `quick` – one flag only. |
| You need **DNA‑Seq (WGS/WES)**, **paired‑end**, **GTF‑based counting**, or **custom references**. | Use `submit` (see below). |
| You are running a **batch of many samples**. | Use `submit‑batch` or the `wizard`. |

---

## 4. Existing Commands (Brief Overview)

| Command | When to use | Minimal flags |
|---------|-------------|---------------|
| `status [RUN_ID]` | Check status / list runs. Omit `RUN_ID` to list all local runs. | none |
| `submit` | Full‑featured single‑sample submission. | `--fastq` / `--fastq-r1` / `--fastq-r2`, `--organism`, `--ref-genome`, `[--reference-fasta]`, `[--gtf]`, `[--panel-bed]`, `[--known-sites]`, `[--paired/--single]` |
| `submit-batch` | Submit many samples at once via a CSV sheet. | `--sample-sheet`, `--organism`, `--ref-genome`, `[--reference-fasta]`, `[--gtf]`, `[--paired/--single]` |
| `wizard` | Interactive prompt that creates an `.env` and a sample‑sheet for you. | None (prompts you step‑by‑step) |

All of these commands default to **local** mode and accept `--temporal` to switch to the Temporal workflow.

---

## 5. Full `submit` Command – When You Need More Control

The `submit` command supports advanced use‑cases. Its help (run `python cli.py submit --help`) lists every option, but the most frequently used minimal subset is:

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
- **`--paired`** (with `--fastq-r1` / `--fastq-r2`) enables paired‑end mode.
- **`--temporal`** submits to Temporal; **`--no-cache`** disables the local cache.

Use `submit` when you need **full control** (e.g., DNA‑Seq with BQSR, custom GTF‑based gene counting, or multi‑panel experiments).

---

## 6. Batch Operations (`submit‑batch` + `wizard`)

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

## 7. Configuration & Environment

| Variable | Default | Where it’s used |
|----------|---------|-----------------|
| `NGS_MODE` | `local` | Execution mode (`local` or `temporal`) |
| `NGS_HOME` | `~/.ngsagent` | Root for the local run store + cache |
| `NGS_RUNS_DIR` | `~/.ngsagent/runs` | Local run records read by `status` |
| `NGS_NO_CACHE` | unset | Set to `1` to disable the local cache |
| `TEMPORAL_HOST` | `localhost:7233` | Temporal server (only in Temporal mode) |
| `S3_ENDPOINT` / `ARTIFACT_BUCKET` | `http://localhost:9000` / `ngs-artifacts` | MinIO artifact storage used by the agent containers |
| `REDIS_URL` / `CACHE_BUCKET` | `redis://localhost:6379` / `ngs-cache` | Optional shared cache (omitted → local FS cache) |
| `ANTHROPIC_API_KEY` | unset | Optional LLM key (agents fall back to heuristics) |

You can override any variable environment‑wide, e.g.:

```bash
export NGS_MODE=temporal
export TEMPORAL_HOST="my-temporal-instance.example.com:7233"
python cli.py quick --fastq data.fastq
```

If you frequently use a remote Temporal service, add the export to your shell profile (`~/.bashrc`, `~/.zshrc`) or create a persistent `.env` file in the project root.

---

## 8. Troubleshooting & FAQ

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `RuntimeError: Agent qc failed: ...` | A stage's Docker container exited non‑zero. | Read the stage's stderr; usually a missing mount, reference, or input path. |
| `docker: command not found` / agent image missing | Docker not installed, or images not built. | Install Docker, then `bash scripts/build-agents.sh`. |
| `Connection refused` to `localhost:9000` | MinIO not running (local mode still needs it for artifacts). | `docker compose -f docker-compose.lite.yml up -d`. |
| `RuntimeError: Failed client connect: Connection refused` (Temporal mode) | Temporal server not running or `TEMPORAL_HOST` wrong. | Start `docker compose up -d` + `python worker.py`, or drop `--temporal` to use local mode. |
| `click.BadParameter: --fastq path does not exist: ...` | FASTQ file path typo or missing file. | Verify the path, create the file, or use an absolute path. |
| `Unrecognized argument: --organism` | Typo or unsupported species. | Use one of: `human`, `mouse`, `rat`, `zebrafish`, `yeast`, `other`. |
| `ModuleNotFoundError: No module named 'click'` | Packages not installed. | Run `pip install -r requirements.txt`. |
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

## 9. Changelog – What Changed

| Change | Reason |
|--------|--------|
| **Local (Temporal‑free) execution is now the default** | The pipeline runs in‑process — no Temporal server, Postgres, Redis, or worker. Docker + MinIO are the only remaining services. |
| **`status` now reads local run records** | Run state lives in `~/.ngsagent/runs`, so status works without Temporal. Omit `RUN_ID` to list runs. |
| **Local content‑addressed cache** | Identical re‑runs skip re‑execution using a filesystem cache (`~/.ngsagent/cache`); `--no-cache` disables it. |
| **Added `quick` command** (single‑line RNA‑Seq) | Removes the need to remember 8‑10 flags for a routine RNA‑Seq run. |
| **Made `--gtf` optional in `submit`** | Researchers who don’t need counting can skip the GTF file entirely. |
| **`wizard` now writes `.env` + sample‑sheet automatically** | One‑step generation of the configuration needed for batch runs. |

---

## 🎯 Quick Start Summary (one‑liner)

```bash
# 1️⃣ Install (once)
cd NGS-Agent && pip install -r requirements.txt

# 2️⃣ Start the only service local mode needs (MinIO) + build agent images
docker compose -f docker-compose.lite.yml up -d && bash scripts/build-agents.sh

# 3️⃣ Submit a quick RNA‑Seq run — runs locally, no Temporal, no worker
python cli.py quick --fastq /path/to/your_data.fastq

# 4️⃣ Check the run
python cli.py status
```

That’s it! You now have a frictionless pathway from FASTQ file to a completed pipeline report in a single command — with no Temporal infrastructure to stand up first. Happy sequencing!
