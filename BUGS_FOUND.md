# NGS-Agent — Bug Hunt Report

Date: 2026-09-10 · Branch: `arena/01a08bed-ngs-agent` · Base: `a33e448`

Method: installed the project in a clean venv (`pip install -e ".[dev,llm,swarm]"`),
ran every documented command (`watch`, `analyze`, `debate`, `config`, `doctor`,
`plan`, the TUI, the swarm `cli.py`, and each container agent locally with the
documented `AGENT_INPUTS`/`ROUTING_CONTEXT` contract), built the wheel, and ran
the CI lint/test commands. The unit suite passes (93 passed, 1 skipped) — every
bug below is **outside the current test coverage**.

Severity: 🔴 blocker · 🟠 major · 🟡 minor

---

## A. Swarm pipeline (`python cli.py submit` / `python worker.py`)

The headline end-to-end pipeline cannot complete for a user following the
README. The supplied integration test (`tests/test_pipeline.py`) hides most of
these because it manually re-mounts files that the real activity layer is
supposed to mount.

### 🔴 A1. Inter-agent file hand-off is broken after the first hop
`workflows/activities.py` (`_replace_local_file_paths`) rewrites host FASTQ
paths to `/mnt/inputs/N_<name>` container paths for the **ingest** container.
The ingest agent echoes those container-private paths back
(`payload.raw_reads_r1/r2`) but never uploads the FASTQs to S3. When the **QC**
activity runs, the rewriter tests the returned paths against the **worker
host** filesystem (`Path(obj).exists()`), they don't exist, so no volume is
mounted into the QC container. QC then dies with
`FASTQ file does not exist at resolved path`.

Reproduced with the real function:
```
HOP1 mounts: [('/tmp/...R1', '/mnt/inputs/0_...R1'), ...]
HOP2 (qc) mounts: []   -> QC container cannot see the reads
```
The same flaw affects every downstream container that expects a local file
that isn't an `s3://` URI.

### 🔴 A2. Reference data and GTF/BED files are never mounted into containers
Only regular files discovered under `inputs` are bind-mounted; `routing_ctx`
is passed through untouched, and agents prefer the `routing_ctx` (host) paths:

- HISAT2 index **basename** (`--ref-genome data/ref/grch38_idx`) is not a
  regular file and its 8 `.ht2` siblings are never mounted → `hisat2` cannot
  load the index (`agents/align/main.py`).
- `count` reads `routing_ctx["gtf"]` (host path) first; the rewritten copy in
  `inputs` is ignored → featureCounts cannot find the GTF
  (`agents/count/main.py`).
- BWA needs `.bwt/.pac/.ann/.amb/.sa/.fai` siblings; GATK needs `.dict/.fai` —
  none are mounted; `reference_fasta` host paths are passed verbatim
  (`agents/bwa_agent/main.py`, `agents/gatk_agent/main.py`).
- `panel_bed` in `routing_ctx` is a host path → never visible in container,
  so per-region coverage is silently skipped (or used un-mounted).

### 🔴 A3. The conditional Trim step can never start
`NGSSampleWorkflow` builds the trim request from the **QC** payload:
`{"payload": {**qc.payload, "trim_params": ...}}`, but the QC payload contains
no `raw_reads*` keys (see `agents/qc/main.py` return). The trim agent raises:
```
RuntimeError: trim agent requires FASTQ input
```
Reproduced:
```bash
AGENT_INPUTS='{"payload":{"verdict":"trim_required","fastqc_data":"x","trim_params":{"LEADING":5}}}' \
  python agents/trim/main.py   # -> RuntimeError: trim agent requires FASTQ input
```
The same broken payload is built on the alignment-retry re-trim path.

### 🔴 A4. Heuristic trim decision never fires without an LLM (documented fallback is dead)
`agents/ai_decider/main.py` counts `^FAIL\t` / `^WARN\t` lines, which is the
**summary.txt** format; but it is fed `fastqc_data`, whose module lines look
like `>>Per base sequence quality\tfail` (lowercase, `>>` prefix). With no
`ANTHROPIC_API_KEY` the heuristic therefore always returns `trim = False`,
even for catastrophic FastQC reports — contradicting the README's
"deterministic heuristic fallbacks when no API key is set".
Reproduced: a `fastqc_data` with one `fail` and one `warn` module →
`trim = False`. (The QC agent's own heuristic correctly uses `summary.txt`.)

### 🔴 A5. WGS/WES always fails at GATK — wrong payload shape
The BWA agent returns the BAM at `payload.artifacts.bam_path`, but the GATK
agent reads `payload.bam_path`:
```
AGENT_INPUTS='{"payload":{"artifacts":{"bam_path":"s3://b/x.bam"}}}' \
  python agents/gatk_agent/main.py
# -> RuntimeError: BAM input not found for GATK
```

### 🔴 A6. Annotation agent crashes on the shipped image
`agents/annotation_agent/Dockerfile` installs no snpEff jar, so
`_annotate_with_snpeff` returns the input path unchanged — the bgzipped
`variants.vcf.gz` produced by GATK. `_parse_vcf` then opens gzip bytes with a
text `open(...)`:
```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0x8b in position 1
```

### 🔴 A7. Coverage gate halts every DNA run using a fabricated depth
With no coverage CSV (the normal case for WGS, and WES whenever the panel BED
isn't mounted — see A2), the coverage agent **assumes 25×**, which is below
its 30× threshold, and halts the run:
```json
{"status":"warn","payload":{"mean_depth":25.0,"passed":false},
 "halt":true,"halt_reason":"Coverage below threshold"}
```
"Data unavailable" must not be reported as "coverage failed".

### 🔴 A8. Differential-expression stage is broken three ways
1. A single `submit` (and the advertised `quick` command) never sets
   `sample_sheet`; the per-sample DE activity raises
   `sample_sheet is required for DE analysis`.
2. The parent batch workflow calls `de_activity` with
   `{"counts": [...], "samples": ...}`, but `agents/de_agent/main.py` requires
   `payload.count_matrix` (and `sample_sheet`) → `count_matrix is required for
   DE analysis`, i.e. the cross-condition DE step — the point of a batch —
   always errors.
3. `agents/de_agent/de_analysis.R` reads featureCounts output with
   `read_csv()` (comma); featureCounts writes a **tab-separated** file with
   `#`-comment lines and `Geneid/Chr/Start/End/Strand/Length` metadata
   columns, and names count columns after the BAM filenames (`input.bam`),
   never after `sample_id` → intersect with the sample sheet is empty and the
   script stops with "Count matrix and sample sheet do not overlap enough".
4. Neither R Dockerfile installs the Bioconductor packages it `library()`s —
   DESeq2, matrixStats, ComplexHeatmap, circlize, EnhancedVolcano
   (`agents/de_agent/Dockerfile`) and clusterProfiler, org.Hs.eg.db
   (`agents/insight_agent/Dockerfile`). They are not in Ubuntu's apt repos,
   so the containers fail at runtime.

### 🔴 A9. Insight/GO step always fails as wired
`agents/insight_agent/main.py` requires a `go_input` file, but neither
`cli.py submit/submit_batch` nor the workflow ever populates `go_input`:
```
RuntimeError: GO enrichment input is required for insight analysis
```

### 🟠 A10. Single-end batch submissions are silently mis-routed
The `wizard`/`submit-batch` CSV uses a column named **`fastq`**; the ingest
agent reads **`fastq_path`** (`agents/ingest/main.py`). Paired-end columns
(`fastq_r1/r2`) match, so only single-end batch runs go into mock mode and
then crash in QC. The single `submit`/`quick` commands use the correct
`fastq_path`, so the two paths disagree.

### 🟠 A11. `quick` one-liner cannot run (and the User Guide mis-describes it)
`python cli.py quick --fastq …` sets `reference_genome` to the bare string
`hg38` (not an index path, never mounted — see A2) and `gtf=None`; the count
agent then raises `GTF path is required for featureCounts`. CLI_USER_GUIDE.md
claims "GTF omitted → counting step is skipped", but nothing in the workflow
skips counting.

### 🟠 A12. Content-addressed cache can never hit across runs
`CacheManager.compute_hash` hashes `routing_ctx`, which contains a fresh
`run-<uuid>` on every submission, so identical re-runs always miss — the
README claims "identical re-runs return from cache instantly without
re-executing any container". Separately, `cache.get()` doesn't catch Redis
`ConnectionError` (only the S3 fallback is guarded), so a stopped Redis makes
every activity fail.

### 🟡 A13. Swarm `wizard` prints an invalid next command
It prompts "genome preset" with `hg38/mm10/mixed` and then echoes
`python cli.py submit-batch --organism hg38 …`, but `submit-batch` only
accepts `human/mouse/rat/zebrafish/yeast/other/mixed`. The same `hg38` value
is written into the sample sheet's `species` column.

### 🟡 A14. `cli.py status` ignores `TEMPORAL_HOST`
`submit` honors `TEMPORAL_HOST`, but `status` hard-codes `localhost:7233`.

---

## B. pip CLI (`ngsagent`)

### 🔴 B1. Failed LLM calls still produce a confident, exit-0 debate report
`debate_variant` (`ngs_agent/debate.py`) catches **all** backend exceptions
and inserts `"[LLM call failed: …]"` as each persona's reasoning; the stances
default to "Uncertain", and the run ends with
"All personas agree: remains VUS", a recommendation, and (with `--html`) an
exported report — exit code 0. With a bad/expired key every persona fails yet
the output is indistinguishable from a real debate:
```bash
GEMINI_API_KEY=bad ngsagent debate demo_data/sample.vcf --html /tmp/d.html
# 6 failed calls, exit 0, "consensus" + HTML exported
```
The CLI's `except RuntimeError` handler in `debate` is dead code as a result.
A failed backend should abort (non-zero exit) rather than fabricate consensus.

### 🟠 B2. ACMG engine inflates classifications by counting duplicate criteria
Codes emitted by all three personas are concatenated without de-duplication,
so one repeated criterion satisfies multi-criterion rules:
```python
compute_acmg_classification(['PM2','PM2','PM2'])  # -> "Likely Pathogenic"
compute_acmg_classification(['PS4','PS4'])        # -> "Pathogenic"
```
ACMG counting must use the *set* of criteria; one PM2 is one PM2 regardless
of how many personas mention it.

### 🟡 B3. VUS stance rendered as "Vus"
`_extract_stance` returns the literal string `"Vus"` in two branches
(`ngs_agent/debate.py`), which is printed in the terminal and HTML badge. It
should be `"VUS"`. `tests/test_debate.py` currently asserts the typo
(`in ("Vus", "Uncertain")`).

### 🟡 B4. `config set` silently accepts unknown keys and coerces values
`ngsagent config set model gpt-4o` prints "Set model = gpt-4o" but `model` is
not a real key — it's dead config. A typo like `llm anthropci` is happily
saved. The recognized-key allowlist also omits the wizard's own keys
(`openrouter_model`, `groq_model`, `deepseek_model`, `openai_compat_*`), and
unknown values containing digits are coerced to int/float, so e.g. a numeric
model tag is stored as a number in YAML.

### 🟡 B5. `watch --signatures <file.yaml>` silently loads nothing
`load_signatures` only globs `*.yaml` *inside* a directory; pointing it at a
single signature file prints "Loaded 0 failure signatures" with no error.

### 🟡 B6. Wheel omits `demo_data/`
The README tells `pip install ngs-agent` users to run
`ngsagent analyze demo_data/sample.vcf` immediately, but the wheel ships only
the `ngs_agent/` package (`pyproject.toml` hatch wheel `packages`); verified
the built wheel contains zero `demo_data` files (they're in the sdist only).

### 🟡 B7. Other pip-CLI nits
- Generic QC metric values render without units (92.5 vs 92.5%).
- `doctor` checks Gemini/Anthropic/OpenAI keys but not OpenRouter/Groq/
  DeepSeek even though the wizard offers them, and reports an active backend
  as OK when its key is missing; exit code is always 0.
- HTML reports do not HTML-escape VCF-derived fields (a `<` in REF/ALT/CLNSIG
  breaks markup / allows injected markup in a shared report).
- `debate_variant` contains an unreachable/confusing
  `if isinstance(backend, NoBackend): backend.complete("")` (the CLI already
  guards `NoBackend`).

---

## C. Developer experience / CI

### 🟠 C1. CI lint is red on `main`
`ruff check ngs_agent/ agents/` (the exact Test workflow step) reports
**140 errors** with current ruff (13 unused imports, 18 blind `except`,
8 `subprocess.run` without `check`, etc.), and the separate Pylint workflow
runs `pylint $(git ls-files '*.py')` with no config. `mypy ngs_agent/` passes.

### 🟡 C2. Orphaned second implementation `src/ngs_agent/` has a runtime NameError
The tree is neither packaged (`packages = ["ngs_agent"]`), nor tested, nor
documented. `src/ngs_agent/agent/orchestrator.py:86` calls `os.access(...)`
without importing `os` → `NameError` on the very first non-dry run.

### 🟡 C3. QC agent computes a read-length hint it never uses
`agents/qc/main.py:195` builds `read_len_hint` ("recommended_trim_bp MUST be
≤ read length") but the Claude prompt f-string never includes it (ruff F841).

### 🟡 C4. Dependency drift
`requirements.txt` pins `temporalio==1.6.0` while `[swarm]` requires
`temporalio>=1.8.0`; `jinja2` (needed by the root `report_builder.py`) is in
requirements.txt but missing from the pyproject extras; the CLI guide
instructs `pip install -r requirements.txt`, which also omits `pandas`,
`matplotlib`, and `jinja2` used by agent code run on the host.

---

## Suggested fix order
1. A1/A2 — make activities stage ALL inputs through S3 (or a shared volume),
   mount reference/index file families, and stop trusting host paths inside
   containers.
2. A3/A4 — pass the ingest payload (or S3 FASTQ URIs) into trim; parse
   `>>Module\t(warn|fail)` lines (or hand the QC summary to the decider).
3. A5–A9 — correct payload contracts (`artifacts.bam_path`, gzip-aware VCF
   reading, coverage "unknown ≠ fail", DE input contract + featureCounts TSV
   parsing + Bioconductor installs, go_input wiring).
4. B1/B2 — fail loud on LLM errors; de-duplicate ACMG codes.
5. Packaging/config/CI cleanups (B4–B6, C1, C4).
