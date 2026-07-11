# NGS-Agent

Agentic bioinformatics CLI for wet-lab NGS teams. Monitor pipeline logs in real time, parse and interpret VCF and QC outputs, and run three-perspective LLM debates on Variants of Uncertain Significance — all from a single `pip install`.

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![PyPI](https://img.shields.io/badge/pypi-ngs--agent-orange)](https://pypi.org/project/ngs-agent/)

---

## Installation

```bash
pip install ngs-agent
```

Core install pulls only `click`, `rich`, and `PyYAML`. No Docker, no Conda environment, no Temporal server.

To use the `debate` command with an LLM:

```bash
pip install "ngs-agent[llm]"
```

To run the full Temporal-orchestrated swarm pipeline (RNA-Seq, WGS, WES end-to-end):

```bash
pip install "ngs-agent[swarm]"
```

---

## Usage

```bash
ngsagent watch pipeline.log
ngsagent watch --tail pipeline.log
ngsagent analyze variants.vcf
ngsagent analyze variants.vcf --qc multiqc_summary.txt
ngsagent debate variants.vcf
ngsagent debate variants.vcf --gene BRCA2
ngsagent config wizard
```

Try it immediately with the bundled demo files:

```bash
ngsagent watch demo_data/sample.log
ngsagent analyze demo_data/sample.vcf
```

---

## Commands

### watch

Scans a pipeline log against five built-in failure signatures. Pass `--tail` to follow a log as it grows.

```bash
ngsagent watch <logfile> [--tail] [--signatures <dir>]
```

Each match prints the matched line, a plain-English explanation of the failure mode, and a concrete suggested fix. Signature severity levels are `critical` and `warning`. No LLM is involved.

Built-in signatures:

| Name | Severity | Fires when |
|---|---|---|
| Adapter Contamination | critical | Adapter sequence detected as overrepresented in reads |
| Low Alignment Rate | critical | Overall mapping rate below 80% |
| Low Mean Coverage | critical | Mean sequencing depth below 20x |
| High PCR Duplication | warning | Duplication rate above 30% |
| Poor Insert Size | warning | Median insert size below 150 bp |

You can supply your own YAML signatures directory with `--signatures`. The schema is the same as the built-in files under `ngs_agent/signatures/`.

---

### analyze

Parses a VCF file and renders a colour-coded variant report in the terminal. Accepts an optional QC summary text file (MultiQC output or any plaintext file containing metrics).

```bash
ngsagent analyze <vcffile> [--qc <qcfile>]
```

VCF parsing reads `GENE`, `CSQ`, `CLNSIG`, and `AF` from the INFO field, and `DP` and `AD` from the sample column to compute read depth and variant allele fraction. Variants are classified automatically:

`Pathogenic` — ClinVar `CLNSIG` contains "pathogenic" without "conflicting"  
`VUS` — ClinVar `CLNSIG` contains "uncertain", "vus", or "unknown significance"  
`Other` — everything else (benign, synonymous, unannotated)

QC parsing extracts mapping rate, mean coverage, duplication rate, and Q30 fraction using regex against the file text and grades each metric pass / warn / fail.

---

### debate

Submits every VUS in a VCF to three independent LLM personas simultaneously. Each persona evaluates the variant from a different disciplinary angle, then the tool builds a consensus and recommendation.

```bash
ngsagent debate <vcffile> [--gene <GENE_SYMBOL>]
```

The three personas:

`Population Geneticist` — evaluates allele frequency, gnomAD population context, and stratification  
`Clinical Geneticist` — evaluates ClinVar classification, ACMG criteria, and phenotype fit  
`Functional Geneticist` — evaluates predicted consequence, splice site impact, and protein-level effect

Consensus logic: if all three agree the variant is pathogenic, it's escalated for clinical follow-up. If all three call it benign, it's flagged for deprioritisation. Mixed opinions surface the disagreement verbatim so the reviewing scientist sees exactly where uncertainty lies.

Requires an LLM backend. Configure one with `ngsagent config wizard`.

---

### config

Manages `~/.ngsagent/config.yaml`.

```bash
ngsagent config wizard
ngsagent config show
ngsagent config set llm anthropic
ngsagent config set anthropic_model claude-opus-4-7
ngsagent config set llm ollama
ngsagent config set ollama_model llama3.2
ngsagent config set ollama_host http://localhost:11434
```

---

## LLM Setup

### Anthropic

```bash
pip install "ngs-agent[llm]"
export ANTHROPIC_API_KEY=sk-ant-...
ngsagent config set llm anthropic
```

Default model is `claude-sonnet-4-20250514`. Override with `ngsagent config set anthropic_model <model>`.

### Ollama (local, no API key)

```bash
pip install "ngs-agent[llm]"
ollama pull llama3.2
ngsagent config set llm ollama
```

Ollama talks to `http://localhost:11434` by default. Override the host and model via `config set`.

`watch` and `analyze` always work with no LLM configured. Only `debate` requires one.

---

## Swarm Pipeline (full RNA-Seq / WGS / WES)

NGS-Agent also ships a Temporal-orchestrated Docker swarm that runs complete genomics pipelines end to end. Each bioinformatics tool runs in its own container as an autonomous agent. Claude is embedded at decision points — QC verdict, trim parameter selection, alignment failure diagnosis, and biological interpretation — with deterministic heuristic fallbacks when no API key is set.

**Requirements:** Docker Engine, Python 3.11+, Linux or macOS (WSL2 on Windows)

**Setup:**

```bash
cp .env.example .env
pip install "ngs-agent[swarm]"
docker compose up -d
bash scripts/build-agents.sh
python worker.py
```

**Submit a paired-end RNA-Seq run:**

```bash
python cli.py submit \
  --experiment RNA-Seq \
  --organism human \
  --ref-genome data/ref/grch38_idx \
  --gtf data/ref/genes.gtf \
  --fastq-r1 data/fastq/R1.fastq.gz \
  --fastq-r2 data/fastq/R2.fastq.gz \
  --paired
```

**Check run status:**

```bash
python cli.py status <run-id>
```

**RNA-Seq pipeline stages:**

Ingest (read count + paired/single detection) → QC (real FastQC + Claude verdict) → AI Decider (Trimmomatic parameters from Claude) → Trim (conditional) → Align (HISAT2 + samtools, with AI-guided re-trim retry on low mapping rate) → Count (featureCounts) → Differential Expression (DESeq2, PCA, MA plot, volcano, heatmap) → GO Enrichment (clusterProfiler + Claude biological narrative) → Report Builder (self-contained HTML) → Report Agent (OpenRouter narrative summary)

**WGS / WES pipeline stages:**

Ingest → QC → AI Decider → Trim → BWA-MEM2 (with per-region coverage from panel BED) → GATK (MarkDuplicatesSpark → BQSR → HaplotypeCaller) → Annotation (snpEff, variant CSV) → Coverage Gate (halts run if mean depth below threshold) → Report Builder → Report Agent

All file artifacts are uploaded to MinIO at `s3://ngs-artifacts/<run_id>/<agent>/`. Results are content-addressed using blake2b hashes of the inputs, so identical re-runs return from cache instantly without re-executing any container.

---

## Project Layout

```
ngs_agent/              pip-installable CLI (watch, analyze, debate, config)
  backends/             LLM provider abstraction: Anthropic, Ollama, NoBackend
  signatures/           YAML failure signatures loaded by the watch command
agents/                 Docker containers, one per pipeline step
  base/base_agent.py    Agent contract: reads AGENT_INPUTS + ROUTING_CONTEXT env vars, prints JSON to stdout
workflows/              Temporal workflow definitions and activity dispatcher
shared/                 AgentResult model, MinIO storage helper, Redis+MinIO cache
cli.py                  Swarm pipeline CLI (submit, status, wizard)
worker.py               Temporal worker process
demo_data/              sample.log and sample.vcf for testing without real data
```

---

## Development

```bash
git clone https://github.com/ranaalyan1/NGS-Agent.git
cd NGS-Agent
pip install -e ".[dev,llm]"
pytest
ruff check ngs_agent/
mypy ngs_agent/
```

---

## License

Apache 2.0. See [LICENSE](LICENSE).
