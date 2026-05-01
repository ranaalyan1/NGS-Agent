# NGS-Agent → ngs 

This repository contains the refactor of NGS-Agent into an enterprise-grade, native-first, agentic CLI named `ngs`.

Commit: f86ab09

**Overview**

- **Native-first** installation using Conda/Mamba (environment.yml). Docker/Apptainer are optional backends.
- A Typer + Rich CLI providing both natural-language and structured commands.
- Agentic core: Planner → Executor → Verifier → Reporter with checkpointing and artifact provenance.
- Bioinformatics tool wrappers (FastQC, Trimmomatic, HISAT2, samtools, featureCounts, MultiQC, DESeq2, GO enrichment).

**Quick Links**

- Source: `src/ngs_agent`
- Environment spec: `environment.yml`
- Installer script: `scripts/install.sh`

**Install (Native, recommended)**

Prerequisites: Conda or Mamba installed.

1. Create the environment with Mamba (recommended):

```bash
mamba env create -f environment.yml -n ngs
mamba activate ngs
```

2. (Optional) Run the one-liner installer to set up entry points and helpers:

```bash
bash scripts/install.sh
```

**Install (Docker, optional)**

Docker is supported as an execution backend but is not required. To use Docker, set backend flags in `ngs` commands or configure the backend in `ngs.toml`.

**CLI Quickstart**

Natural-language run (single-shot):

```bash
ngs "Run an RNA-Seq pipeline on samplesheet.csv and produce a differential expression report"
```

Structured run (recommended for reproducibility):

```bash
ngs run rnaseq --samples samplesheet.csv --outdir results/rnaseq --reference data/genome.fa
```

Useful commands:

- `ngs plan rnaseq --samples samplesheet.csv` — preview the plan and estimated runtime/cost.
- `ngs run rnaseq --confirm` — run with automatic confirmation.
- `ngs doctor` — validate local environment and dependencies.

**Backend Selection**

By default `ngs` uses the native backend (Conda/Mamba). To force Docker or Apptainer, use the `--backend` flag: `--backend docker` or `--backend apptainer`.

**Migration Notes (from legacy NGS-Agent)**

- Old `cli.py` entrypoints have been consolidated into the new `ngs` CLI.
- Common mappings:
  - `python cli.py run` → `ngs run ...`
  - `python cli.py analyze` → `ngs analyze ...`
  - Temporal workflows are preserved for compatibility in `workflows/` but local/native execution is now the default.

**Quick Verification**

1. Ensure the environment is active (see Install section).
2. Run a dry-run plan:

```bash
ngs plan rnaseq --samples samplesheet.csv
```

3. Execute a small test run or use `ngs doctor` to validate tools.

**Contributing & Development**

- Code lives in `src/ngs_agent`.
- Add new tool wrappers to `src/ngs_agent/tools` and register them with the ToolRegistry.
- Run Python compile checks locally:

```bash
python -m compileall src/ngs_agent
```

**Acknowledgements & Notes**

- This refactor introduces an Anthropic/Claude-enabled reporter integration (guarded by `NGS_ANTHROPIC_API_KEY`) for narrative summaries — opt-in only.
- For full migration guidance, see the QUICKSTART and docs/migration.md (coming soon).

**License**

See the repository license file.
# NGS Agent Swarm

Temporal-orchestrated RNA-Seq pipeline with containerized agents and MinIO artifacts.

## Implemented pipeline

- Ingest: validates FASTQ input and read counts
- QC: runs FastQC and uploads report artifacts
- AI Decider: sends FastQC metrics to Claude and decides if trim is needed
- Trim: runs Trimmomatic (single or paired mode)
- Align: runs HISAT2 + samtools sort/index
- Count: runs featureCounts
- DE: runs DESeq2, PCA, MA, volcano, and heatmap generation
- Insight: runs GO enrichment and grounded AI interpretation
- Report Builder: generates a self-contained HTML report
- DNA branch: runs BWA-MEM2, GATK calling, annotation, and coverage summaries

## Prerequisites

- Docker Engine/Desktop
- Python 3.11+
- Linux/macOS shell (Windows users should run under WSL2)

## Security

- `.env` is git-ignored
- Copy `.env.example` to `.env`
- Rotate any credentials if they were ever exposed in commit history

## Setup

```bash
cp .env.example .env
python -m pip install -r requirements.txt
docker compose up -d
bash scripts/build-agents.sh
```

Start worker:

```bash
python worker.py
```

Quick-start wizard:

```bash
make wizard
```

DNA branch note:

- Provide `--experiment WGS` or `--experiment WES` together with `--reference-fasta`.
- Optionally mount a prebuilt `snpEff.jar` and set `SNPEFF_JAR=/path/to/snpEff.jar` for richer annotation.

## Real data example (paired-end)

1. Download a tiny paired FASTQ test set:

```bash
mkdir -p data/fastq
curl -L -o data/fastq/test_R1.fastq.gz "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR258/008/SRR2584868/SRR2584868_1.fastq.gz"
curl -L -o data/fastq/test_R2.fastq.gz "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR258/008/SRR2584868/SRR2584868_2.fastq.gz"
```

2. Download reference + annotation and build HISAT2 index:

```bash
mkdir -p data/ref
curl -L -o data/ref/genome.fa.gz "https://ftp.ensembl.org/pub/release-112/fasta/homo_sapiens/dna/Homo_sapiens.GRCh38.dna.primary_assembly.fa.gz"
curl -L -o data/ref/genes.gtf.gz "https://ftp.ensembl.org/pub/release-112/gtf/homo_sapiens/Homo_sapiens.GRCh38.112.gtf.gz"
gunzip -f data/ref/genome.fa.gz
gunzip -f data/ref/genes.gtf.gz
hisat2-build data/ref/genome.fa data/ref/grch38_idx
```

3. Submit run:

```bash
python cli.py submit \
  --experiment RNA-Seq \
  --organism human \
  --ref-genome data/ref/grch38_idx \
  --gtf data/ref/genes.gtf \
  --fastq-r1 data/fastq/test_R1.fastq.gz \
  --fastq-r2 data/fastq/test_R2.fastq.gz \
  --paired
```

4. Check run:

```bash
python cli.py status <run-id>
```

## Artifact locations

- QC report: `s3://ngs-artifacts/<run_id>/qc/...`
- Trimmed FASTQ: `s3://ngs-artifacts/<run_id>/trim/...`
- BAM + BAI: `s3://ngs-artifacts/<run_id>/align/...`
- Count matrix/summary: `s3://ngs-artifacts/<run_id>/count/...`
- DNA BAM/VCF/annotation outputs: `s3://ngs-artifacts/<run_id>/dna/...`

## Tests

Functional test harness:

```bash
RUN_NGS_FUNCTIONAL=1 \
TEST_FASTQ_R1=/abs/path/R1.fastq.gz \
TEST_FASTQ_R2=/abs/path/R2.fastq.gz \
TEST_HISAT2_INDEX_DIR=/abs/path/index_dir \
TEST_GTF=/abs/path/genes.gtf \
pytest -q tests/test_pipeline.py
```

## Current limitation

- DNA variant-calling branch is not yet wired into the workflow and remains the next expansion path.
