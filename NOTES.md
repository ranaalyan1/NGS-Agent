# NOTES

What was built, what was deliberately not built, and what needs a human's
decision. Written for the next person to pick this up.

## 2026-09-26 — launch-gap round (five review items)

1. **VCF roadmap stated publicly** (`ROADMAP.md`, linked from the README scope
   section and from the VCF verdict itself). No VCF judging yet — still an
   honest unknown — but the scope and ordering are now written down.
2. **Snakemake / Cromwell-WDL recognition** (`core/sniff.py`, `core/assess.py`):
   new `snakemake_log` / `cromwell_log` kinds (WDL source included) that return
   specific "recognised, diagnosis planned" verdicts instead of generic
   unknowns. Full signature diagnosis for these runners is roadmap item 2–3.
3. **MultiQC input** (`core/parse/multiqc.py`, `core/rules/multiqc_rules.py`):
   reads data JSON, general-stats tables, and report HTML; judges each sample
   with the same QC rule IDs and thresholds (imported from `qc_rules`, never
   copied) plus two cohort rules. Run folders containing summaries get them as
   QC evidence, including a duplication fallback for `AUD-DUP-04`.
4. **Install friction** (`Dockerfile`, `environment-box.yml`, pipx/conda/Docker
   recipes in the README, `.github/workflows/docker.yml` publishing
   `ghcr.io/ranaalyan1/ngs-agent` on tags).
5. **Adoption proof** (README case-study section + `CASE_STUDY.md`): the
   contig-mismatch run, with real receipts pasted from actual CLI output.

Deliberate calls: MultiQC findings reuse the six QC rule IDs (same judgments,
new evidence — the receipts audit needed no change); `RULESET_VERSION` is
unchanged because no threshold or existing wording moved; the one existing
test touched (`test_vcf_exits_two_without_guessing`) was made
wrap-insensitive rather than weakened — it asserted on a line-break accident.
Docker image build is CI-tested (no Docker in this sandbox).

## What exists

| Station | Where | Tests |
|---|---|---|
| 1 — The Sniffer | `core/sniff.py`, `fixtures/` | `tests/test_sniff.py` (36) |
| 2 — FastQC parser + QC rules | `core/parse/fastqc.py`, `core/rules/qc_rules.py` | `tests/test_fastqc_parse.py`, `tests/test_qc_rules.py` (48) |
| 3 — Answer writer + report | `core/answer.py`, `core/report.py` | `tests/test_answer_report.py` (21) |
| 4 — The Box | `doors/gui/` | `tests/test_gui.py` (20) |
| 5 — Log diagnoser | `core/parse/nextflow_log.py`, `core/diagnose.py`, `core/signatures/*.yaml` | `tests/test_logdiag.py` (19) |
| 6 — Folder audit | `core/parse/folder.py`, `core/rules/audit_rules.py` | `tests/test_audit.py` (44) |
| 7 — CLI door | `doors/cli.py` | `tests/test_cli.py` (22) |
| 8 — Hardening | `README.md`, `tests/test_zz_receipts_audit.py`, `tests/test_docs.py` | (35) |

`core/assess.py` is the single door-facing entry point: `path -> Verdict`.
Both doors call it and nothing else.

## Refused, on the Forbidden List

These were not built, and should not be built without a scope change:

* **Pipeline execution** — no Nextflow/Snakemake/Docker/Slurm launching.
  NGS-Agent reads the outputs of runs; it never starts one.
* **Swarm / autonomous / self-directed execution** — nothing here decides what
  to do next on its own. The pipeline is fixed: sniff, parse, rules, answer.
* **VCF variant prioritisation** — a VCF gets an honest "I can't interpret this
  yet" (`unknown_verdict`), never a guess.
* **Debate personas / multi-LLM discussion** — v1 has no language model at
  all. Every sentence is a template plus a number.
* **MCP server** — no tool-calling protocol surface.
* **Accounts, auth, cloud upload, user databases** — no network code exists in
  `core/`; the tests grep for it.

## Not built, because no station asked for it

Recorded here instead of being quietly added:

1. **`ngs --out DIR`** to write the HTML report from the CLI. The Box has
   "Download report"; the CLI has `--json`. The HTML writer is one call away
   (`core.report.write_report`) if wanted.
2. **CRAM support.** The sniffer detects `BAM\x01`; a CRAM would come back
   `unknown`. The audit's truncation check reads the BGZF EOF marker, which is
   BAM-specific.
3. **Beyond 2,000 files / 4 MB text files.** `parse_folder` caps both
   (`MAX_FILES`, `MAX_TEXT_BYTES`) and silently skips the rest. A real WGS run
   folder with thousands of sharded BAMs is not handled yet.
4. **More charts in the report.** Only the per-base quality curve is drawn
   (inline SVG). Adapter and GC curves are numbers in "Details".
5. **Watch mode** (following a growing log). The log diagnoser reads a finished
   file.
6. **Cohort-aware thresholds.** `AUD-DUP-04` compares samples inside one folder
   only. It says nothing when a folder holds a single sample, which is honest
   but incomplete.
7. **Localisation.** English only.

## Decisions a human should review

* **`ngs` console script was reassigned** from the legacy `ngs_agent.cli:main`
  to `doors.cli:main`. Existing installs that used `ngs` for the old CLI will
  now get the new one; `ngsagent` still points at the legacy CLI. Say the word
  and it goes back.
* **The legacy tree is untouched**: `ngs_agent/`, `agents/`, `workflows/`,
  `shared/`, `tools/`, `cli.py`, `worker.py`. Some of it (swarm execution,
  multi-perspective debate) is on the Forbidden List. It should probably be
  removed before v1.0 ships, or it will read as part of the product.
* **`README.md` was rewritten** for the new product. The old README is in git
  history.
* **Signature `reference` URLs** point at tool homepages and documentation
  roots only. If you want deep links into specific manual sections, they need
  checking one by one — a wrong reference is worse than a short one.
* **Thresholds** are consolidated at the top of `core/rules/qc_rules.py` and
  `core/rules/audit_rules.py`. They encode opinions (duplication 20/50/70%,
  freemix 3/5%, alignment 75/50%) that a biologist should sign off on.

## How to regenerate things

```bash
python scripts/make_fixtures.py     # fixtures/ (deterministic, byte-identical)
python scripts/make_screenshot.py   # docs/images/quickstart.png from real CLI output
.venv/bin/python -m pytest          # ~500 tests
```

Fixtures are generated, not hand-written, so a reviewer can read exactly what
was planted where in `scripts/make_fixtures.py`.
