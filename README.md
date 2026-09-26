# NGS-Agent

**Drop an NGS file. Get a true answer in plain language, with receipts.**

NGS-Agent reads a FastQC report, a run folder, or a Nextflow log and tells you —
in sentences a PI can act on — what is wrong, what it means, and the one thing
to do next. Every claim it makes is printed with the receipt that proves it:
which file, which line, which rule, which version.

```bash
ngs sample_fastqc.zip
```

![ngs sample_fastqc.zip — real output](docs/images/quickstart.png)

---

## 60-second quickstart

```bash
# 1. Get the code
git clone https://github.com/ranaalyan1/NGS-Agent.git && cd NGS-Agent

# 2. Install (Python 3.11+)
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 3. Point it at a file
.venv/bin/python -m doors.cli fixtures/fastqc/sample_fastqc.zip

# 4. Or at a whole run folder — all the steps exited 0, and it is still broken
.venv/bin/python -m doors.cli fixtures/runs/contig_mismatch

# 5. Or open the Box (drop zone, no instructions needed)
.venv/bin/python -m uvicorn doors.gui.app:app --host 0.0.0.0 --port 8000
```

That is the whole interface. There is no config file, no file-type picker, and
no menu: the sniffer decides what a file is from its contents.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | passed, or warnings only |
| `1` | at least one failing finding |
| `2` | the input could not be interpreted |

---

## The Box

`POST /analyze` on the GUI is the same pipeline as the CLI. Three states only:

**EMPTY** (drop zone) → **CHECKING** (spinner) → **VERDICT** (answer, *Download
report*, *Details*).

The downloaded report is a single HTML file with inline CSS: no fonts, no
scripts, no network calls. Email it to a PI and it renders
([a real one](docs/sample_report.html)). Next to it, `ngs <path> --json`
writes the same verdict as JSON, receipts included.

---

## What it reads today

| Input | What you get |
|---|---|
| FastQC report (`.zip`) | Six QC rules (below). Bottom line: **healthy / trim and proceed / re-sequence**. |
| Run folder | Ten audit rules that compare the files against each other (below). Bottom line: **healthy / review / fix and re-run**. |
| Nextflow log | Ten failure signatures, ranked, with the line numbers that matched. Bottom line: **the one root cause**, or an honest "unknown" with the last 20 lines attached. |

### The QC rules

| Rule | Catches |
|---|---|
| `QC-QUAL-01` | Base quality falling below Q20, and where to trim |
| `QC-ADAPT-01` | Adapter sequence still in the reads (above 5%) |
| `QC-DUP-01` | Duplication, with the RNA-seq-versus-WGS context that decides whether it matters |
| `QC-GC-01` | A narrow GC spike: contamination, not a genome |
| `QC-N-01` | Cycles where the machine could not call a base |
| `QC-LEN-01` | Reads that are not all the same length |

### The folder audit is the point

These are the failures that do not announce themselves. Every step exits 0,
every file is present, and the numbers are quietly wrong:

| Rule | Catches |
|---|---|
| `AUD-STRAND-01` | Chromosomes named differently in the alignment and the gene list, with an assignment rate under 30% — the silent differential-expression killer |
| `AUD-STRAND-02` | Strandedness setting contradicted by the counts (including libraries that carry no strand information at all) |
| `AUD-CONTAM-02` | Contamination above 3% |
| `AUD-DUP-04` | One sample's duplication far above the cohort median |
| `AUD-TRUNC-01` | An alignment file that was never finished writing |
| `AUD-BUILD-01` | Two genome builds mixed in one run |
| `AUD-COUNT-01` | Normalised values (TPM) sitting where raw counts belong |
| `AUD-PAIRED-01` | Paired-end libraries counted as single-end |
| `AUD-ADAPT-03` | Adapter read-through (fragments shorter than the reads) |
| `AUD-ALIGN-01` | Alignment rate below 75% |

`fixtures/runs/contig_mismatch` is exactly the disaster case: all four steps
exited 0, and 18% of reads were assigned to genes. NGS-Agent catches it.

---

## Receipts

A claim without a receipt is an opinion. Every finding carries at least two:

```
QC-QUAL-01: rule:QC-QUAL-01 @ Per base sequence quality — ruleset 2026-09
QC-QUAL-01: file:751e79c3d819 @ sample_fastqc/fastqc_data.txt:line=28
            — Per base sequence quality mean = 17.8 at position 40-49 (threshold Q20)
```

* `rule:<ID>` — which rule said so, and under which ruleset version.
* `file:<sha256-12>` — the exact line of the exact file the number came from.
* `signature:<ID>` — for logs, which failure pattern matched, and where.

Where evidence is missing, the tool says so and the verdict is **unknown**. It
never fills the gap with a confident guess.

---

## Scope: what NGS-Agent is, and is not

NGS-Agent is an **interpreter**, not a runner. It never executes a pipeline,
never uploads anything, and never calls a language model to decide what to
tell you — every sentence in v1 comes from a template and a rule.

**v1 does not do:**

* running or orchestrating pipelines (Nextflow, Snakemake, Docker, Slurm);
* interpreting VCF variant files — you will get an honest "I can't interpret
  this yet" rather than a guess;
* variant prioritisation or ACMG classification;
* multi-model debate or persona discussion;
* an MCP server, accounts, authentication, or any cloud upload.

Those are out of scope by design, not by accident. See `NOTES.md` for what was
deliberately left out of this build.

It also cannot judge what it cannot see: a folder with no counts summary gets no
assignment-rate verdict, and says so in the output.

---

## Development

```bash
.venv/bin/python -m pytest              # the whole suite (~500 tests)
.venv/bin/python -m pytest tests/test_audit.py -v   # one station
python scripts/make_fixtures.py         # regenerate fixtures/ (byte-identical)
python scripts/make_screenshot.py       # regenerate docs/images/quickstart.png
```

The suite enforces the product's own laws: the Golden Rule (no logic in
`doors/`), the Law of Receipts (every finding created anywhere in the session is
audited), and the honest-unknown rule (traps: a VCF renamed `.txt`, a VCF with
no extension, a gzipped VCF, empty and garbage files).

### Layout

```
core/          all logic lives here
  sniff.py       what is this file? (content, never filename)
  assess.py      one entry point: path -> Verdict
  parse/         fastqc.py, folder.py, nextflow_log.py
  rules/         qc_rules.py, audit_rules.py
  signatures/    ten Nextflow failure signatures (YAML)
  answer.py      Verdict -> plain language (templates only)
  report.py      Verdict -> standalone HTML + JSON sidecar
doors/         ways in; no logic
  cli.py         ngs <path>
  gui/           FastAPI + one HTML page
fixtures/      generated test data, including planted failures
```

---

## Licence

Apache-2.0. See [LICENSE](LICENSE).
