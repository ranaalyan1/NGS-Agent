# NGS-Agent — Audit & Design Set

This directory is the design record for the evidence-backed variant-review
engine in `ngs_agent/core/`. It exists so a reviewer, auditor, or new engineer
can answer *"why is it built this way, and what proves it works?"* without
reading ten thousand lines of Python.

**Positioning.** Other projects generate evidence — AlphaGenome, AlphaFold,
BioNeMo, Parabricks, ClinVar, gnomAD, ClinGen. NGS-Agent does not compete with
them and does not rebuild them. It retrieves their output into a versioned
ledger and makes that evidence *accountable*: every criterion cites a record,
every record names its source and version, every result is replayable, and a
human signs off before anything is acted on.

> They generate evidence. We make evidence accountable.

**Status: research use only.** NGS-Agent has not undergone clinical validation
and is not cleared or approved as a medical device. Nothing in this repository
is a diagnostic output.

---

## The ten items

| # | Document | What it settles |
|---|----------|-----------------|
| 01 | [audit/01-current-state-audit.md](audit/01-current-state-audit.md) | What the codebase actually did before this work, and which behaviours were unsafe |
| 02 | [audit/02-safety-invariants.md](audit/02-safety-invariants.md) | The non-negotiable invariants, each mapped to the test that enforces it |
| 03 | [adr/03-evidence-first-acmg.md](adr/03-evidence-first-acmg.md) | No evidence record, no ACMG criterion |
| 04 | [adr/04-llm-boundary.md](adr/04-llm-boundary.md) | Models may explain; they may never create evidence or classify |
| 05 | [adr/05-variant-identity.md](adr/05-variant-identity.md) | Normalization, builds, multiallelic splitting, locus ≠ allele |
| 06 | [adr/06-evidence-ledger.md](adr/06-evidence-ledger.md) | The typed ledger, adapters, and why missing ≠ negative |
| 07 | [adr/07-deterministic-engine.md](adr/07-deterministic-engine.md) | Rule sets, strength modifiers, conflicts, PVS1 applicability |
| 08 | [adr/08-result-contract.md](adr/08-result-contract.md) | The versioned JSON contract and abstention semantics |
| 09 | [adr/09-audit-replay-signoff.md](adr/09-audit-replay-signoff.md) | Audit chain, human sign-off, override, and replay |
| 10 | [design/10-benchmark-and-roadmap.md](design/10-benchmark-and-roadmap.md) | Benchmark harness design (not yet runnable) and what comes next |

Items 03–09 are architecture decision records: each states the context, the
decision, what was rejected, and the consequences — including the ones that
cost us something.

## Reading order

* **Auditing the safety story** → 02, then 03 and 04. Item 02 is the shortest
  path from "what do you promise?" to "which test proves it?".
* **Integrating the engine** → 08 (the contract), then 05 (variant identity)
  and 06 (evidence).
* **Understanding a classification** → 07, then 09 for how it was recorded.
* **Reviewing what changed and why** → 01.

## Ground truth at the time of writing

These numbers are stated once, here, so the individual documents do not drift.
Regenerate them with the commands shown.

| Fact | Value | Command |
|------|-------|---------|
| Tests, total | 659 (1 skipped unless `RUN_NGS_FUNCTIONAL=1`) | `pytest tests/` |
| Tests, `tests/core/` | 422 | `pytest tests/core` |
| Engine source | 10,362 lines in `ngs_agent/core/` | `find ngs_agent/core -name '*.py' \| xargs wc -l` |
| Engine tests | 4,747 lines in `tests/core/` | `find tests/core -name '*.py' \| xargs wc -l` |
| Contract schema | `1.0.0` | `ngs_agent.core.version.CONTRACT_SCHEMA_VERSION` |
| Audit schema | `1.0.0` | `AUDIT_SCHEMA_VERSION` |
| Evidence schema | `1.0.0` | `EVIDENCE_SCHEMA_VERSION` |
| Engine version | `core-1.0.0` | `ENGINE_VERSION` |
| Normalization version | `norm-1.0.0` | `NORMALIZATION_VERSION` |
| Gene mechanism table | `gene-mechanism-0.1.0-curated-seed` | `GENE_MECHANISM_TABLE_VERSION` |
| ACMG criterion specs | 28 (16 pathogenic codes, 12 benign codes) | `ngs_agent.core.acmg.criteria.CRITERIA` |
| Rule sets | `acmg-amp-2015` v1.0.0 (default), `acmg-amp-2015+clingen-svi-2020` v1.0.0 | `ngs_agent.core.acmg.rule_sets.REGISTRY` |
| Evidence data types | 14 | `ngs_agent.core.evidence.models.EvidenceDataType` |
| Review commands | `normalize`, `review`, `replay`, `sign-off`, `audit` | `ngs_agent.core.cli.REVIEW_COMMANDS` |
| Lint | `ruff check ngs_agent/ tests/ agents/` passes with no waiver on the core tree | CI |

## Two commands that demonstrate the whole thesis

```console
$ ngsagent review demo_data/review_demo.vcf --genome-build GRCh38 --audit-dir /tmp/audit
# 3 alleles from 2 VCF records: Pathogenic, Benign, and one that abstains.
# Exit code 3 — "this run abstained" — so a pipeline can branch on it.

$ ngsagent audit /tmp/audit --audit-dir /tmp/audit        # list records, hash the log
$ ngsagent replay <audit_id> --audit-dir /tmp/audit       # re-derive from the record alone
$ ngsagent sign-off <audit_id> --audit-dir /tmp/audit \
      --reviewer "Dr A. Reviewer" --role clinical_laboratory_scientist --action approve
```

`replay` and `sign-off` both take an **audit id** (not a result id), listed by
`ngsagent audit`. `replay` re-runs the deterministic engine against the evidence
snapshot stored in that record and diffs the reproduced contract against the
stored one; `--strict` is the default, so a failure to reproduce exits non-zero
(`4`). `sign-off` requires `--reviewer`, and `--notes` when rejecting or
`--reason` when the reviewer's tier differs from the engine's.

The third allele abstains because a synonymous consequence with no splice
predictor configured is *not* evidence of benignity. That refusal is the
product working as designed, and it is the single most important thing to
understand about this codebase.

Two honest caveats about the other two alleles. First, both reach their label by
**adopting** a ≥3-star ClinVar expert-panel classification
(`decision_basis="authoritative_external_classification"`), with no criteria
derived and no combination rule fired — the demo exercises identity resolution,
the ledger, abstention and the audit chain, not the full derivation path. Second,
only ClinVar and a bundled gene-mechanism seed table are wired, so 23 of 28
criteria are `not_evaluated` on these variants. Both facts are visible in the
contract, and both are why [item 10](design/10-benchmark-and-roadmap.md) reports
no accuracy figure: benchmarking this configuration against ClinVar would measure
a lookup. See that item's "circularity trap".
