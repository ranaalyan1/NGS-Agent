# 10 — Benchmark harness design, and what comes next

**Status: design only. Not implemented.**

> **Benchmark not yet available.**
>
> No truth set is bundled, no harness exists, and no accuracy number has been
> measured. There is no sensitivity, specificity, precision, F1 or concordance
> figure anywhere in this repository, and any such figure quoted from this project
> would be fabricated. This document specifies how to produce real ones.

`find . -iname '*benchmark*'` returns nothing. That is the honest current state,
and stating it is part of the design: a benchmark whose absence can be quietly
papered over is not worth running.

---

## Why this is last, not first

The engine currently has two evidence sources wired (ClinVar and a bundled gene
mechanism seed table). Twelve of fourteen evidence data types have no adapter
([ADR-06](../adr/06-evidence-ledger.md)), so most criteria evaluate to
`not_evaluated` and most variants abstain.

Benchmarking now would measure *adapter coverage*, not classification quality. A
low score would be uninformative about the engine, and — worse — a partially wired
engine could score well on a truth set that happened to overlap its coverage,
producing a number that looks like validation and is not.

## The circularity trap

This is the single most important design constraint, and it is specific to this
architecture.

The engine **adopts authoritative external classifications**: a ClinVar record at
≥3 stars (expert panel or practice guideline) can be adopted directly, per
[ADR-07](../adr/07-deterministic-engine.md). Meanwhile the obvious truth sets —
ClinGen VCEP specifications, ClinVar expert-panel records — *are* ≥3-star ClinVar
classifications.

Benchmark the shipped configuration against those truth sets and the engine will
score near-perfect concordance by **reading the answer key**. It would be a
faithful measurement of a lookup, presented as a measurement of reasoning.

**Therefore the harness must run in two distinct modes, reported separately and
never merged:**

| Mode | External adoption | What it measures |
|---|---|---|
| `derivation-only` | **disabled** | Whether criteria derived from primary evidence (frequency, consequence, mechanism, predictors) reproduce expert conclusions. This is the number that means anything about the engine. |
| `as-shipped` | enabled | End-to-end agreement with current practice, including adopted classifications. Useful operationally; near-meaningless as a quality measure, and must be labelled as such in any report. |

Any published figure must name its mode. A figure without a mode is not
interpretable.

A second, subtler leak: the truth set must not overlap the recorded fixtures used
as evidence. Truth and evidence are separate datasets with separate provenance,
and the harness must assert disjointness by variant identity before scoring.

**This is not hypothetical — the shipped demo is an adoption case.** Running
`ngsagent review demo_data/review_demo.vcf` produces three alleles: Pathogenic,
Benign, and one that abstains. Both classified alleles reach their label via
`decision_basis="authoritative_external_classification"`, adopted from ClinVar
expert-panel records `VCV000017675.110` and `VCV000017676.77`, with
`applied_criteria` empty and no combination rule fired. Benchmarked against
ClinVar, those two would score as perfect agreement while exercising none of the
derivation logic. The third allele — which abstains — is the only one of the three
whose outcome reflects the engine's own reasoning.

A harness that did not separate these modes would report 100% concordance on two
of three variants and measure nothing.

## Metrics

Per-tier confusion matrix first; derived metrics second. A single accuracy number
over five ordered tiers hides exactly the errors that matter.

| Metric | Definition | Why |
|---|---|---|
| Sensitivity (per tier) | Of truth = *T*, fraction reported *T* | Recall of Pathogenic is the safety-critical one |
| Specificity (per tier) | Of truth ≠ *T*, fraction not reported *T* | Guards against inflation |
| Precision (per tier) | Of reported *T*, fraction with truth = *T* | What a reviewer acting on the label can trust |
| F1 | Harmonic mean, per tier | Balances the two, per tier only |
| Concordance | Exact tier agreement | Comparable with the engine's existing per-variant `concordance` |
| Partial concordance | Same `tier_group` (positive / uncertain / negative) | Pathogenic vs Likely Pathogenic is a different error than Pathogenic vs Benign |
| **Abstention rate** | Fraction with `abstained=True` | Must be reported *alongside* accuracy, never folded into it |
| **Unsafe-overcall rate** | Truth Benign or Likely Benign, reported Pathogenic or Likely Pathogenic | Escalates care for a healthy person |
| **Unsafe-undercall rate** | Truth Pathogenic or Likely Pathogenic, reported Benign or Likely Benign | Ends surveillance for an affected person |

The last two are the metrics this project exists to control. They are reported
separately rather than merged into "discordance", because the two directions have
different clinical costs and different remedies — the same asymmetry the engine
applies internally ([ADR-07](../adr/07-deterministic-engine.md), "fail toward VUS").

### Abstention must not be allowed to flatter the result

A system that abstains on everything has zero unsafe-overcalls and undefined
precision. So:

* Accuracy metrics are computed over **classified** results, with the denominator
  stated explicitly.
* Abstention rate is reported next to them, always.
* The harness reports the pair `(unsafe-overcall rate, abstention rate)` as the
  headline, because improving the first by worsening the second is not progress.
* Abstentions are broken down by `decision_state`
  (`insufficient_evidence`, `conflict`, `abstained`, `manual_review_required`) so
  "we lack an adapter" is distinguishable from "the evidence contradicts itself".

## Truth sets

| Candidate | Use | Caveat |
|---|---|---|
| ClinGen VCEP classifications | Primary truth for `derivation-only` | Gene-specific specs; must record the VCEP and spec version |
| ClinVar expert-panel / practice-guideline (≥3 star) | Secondary truth | **Circular under `as-shipped`** — see above |
| ClinVar 1–2 star submissions | *Not* truth | Inter-submitter conflict is common; usable only as an agreement study |

Requirements for any truth set actually adopted:

* Versioned and dated — the release it was drawn from, and the retrieval date.
* Keyed by allele identity, not locus ([ADR-05](../adr/05-variant-identity.md)).
  A truth set matched by position would inherit the `rs41293455` conflation.
* License and redistribution checked before bundling.
* Stored as recorded fixtures, so a benchmark run is reproducible without network
  access and without re-querying a database that has since changed.
* Disjoint from evidence fixtures, asserted by the harness.

## Reporting rules

1. If the harness cannot run — no truth set, missing adapter, unresolved license —
   it prints **"Benchmark not yet available"** and the reason. It does not report
   partial numbers as if they were complete, and it does not extrapolate.
2. Every figure ships with: mode (`derivation-only` / `as-shipped`), truth-set
   name and version, evidence fixture versions, rule set and version, engine
   version, denominator, and abstention rate.
3. Results are emitted as the same versioned JSON contract style as everything
   else, and written to the audit log, so a benchmark is itself replayable.
4. No figure is rounded up, and small denominators are flagged rather than
   presented as rates.
5. Numbers are never quoted without their mode and denominator. A benchmark that
   can be summarized in one number will be.

## What exists today that the harness will build on

Not nothing — these are already implemented and tested:

* **Per-variant concordance.** `_concordance()` returns `concordant`,
  `partially_concordant`, `discordant` or `not_comparable`, using `tier_group` to
  distinguish same-group from opposite-group disagreement. The harness's
  concordance and partial-concordance metrics reuse this rather than
  reimplementing tier comparison.
* **Run summaries.** `summarize_run()` already emits `labels` (display labels),
  `decision_states`, `abstention_rate` (4 dp), `conflicts`,
  `requires_human_review`, `database_versions` and `run_warnings` — most of the
  denominator bookkeeping a benchmark needs.
* **Determinism and replay.** A benchmark run is reproducible and auditable
  because the engine is pure and every decision is replayable
  ([ADR-09](../adr/09-audit-replay-signoff.md)).
* **Recorded fixtures.** The adapter-recording mechanism that makes air-gapped
  operation work is the same mechanism a versioned truth set needs.

## Roadmap

Sequenced by what unblocks what. The gating rules from the project brief are
restated inline because they are the reason for the order.

| # | Work | Gate |
|---|---|---|
| 1 | **gnomAD adapter** (allele frequency, ancestry-stratified, exact ratios) | Unblocks BA1, BS1, PM2 properly. Highest value per unit effort: frequency is the most commonly decisive evidence and is well-structured. |
| 2 | **VEP or SnpEff adapter** (consequence, transcript, MANE Select) | Unblocks PVS1 context, PM4, and transcript-aware normalization; currently the largest source of `not_evaluated`. |
| 3 | **SpliceAI, REVEL, CADD adapters** (predictors) | Unblocks PP3, BP4. Must record predictor *versions* — scores are not comparable across releases. |
| 4 | **Benchmark harness**, `derivation-only` mode first | Requires 1–3, or it measures coverage. |
| 5 | **MCP server** | *Only after* the JSON contract is stable ([ADR-08](../adr/08-result-contract.md)). Must return the versioned contract and may not bypass evidence validation or human review. |
| 6 | **HTTP API** | Same contract, same gates as MCP. |
| 7 | **Review workbench TUI** | *Only after* the contract is stable; a large UI built on a moving schema is rework. |
| 8 | **Nextflow / pipeline integration** | Consumes the CLI's exit codes (`0`/`2`/`3`/`4`), which are already stable. |
| 9 | Gene-specific VCEP rule sets | After 4, so each spec can be validated against its truth set. |
| 10 | Full VRS descriptors in the contract | Identity is already VRS-compatible in substance ([ADR-05](../adr/05-variant-identity.md)); this is an emit-format change and a schema bump. |

Temporal and Docker-swarm orchestration remain **experimental and isolated**. They
are not extended until the core engine is validated by item 4 — orchestrating an
unvalidated classifier scales the problem, not the solution.

## Explicitly not claimed

* No clinical-grade or diagnostic status. Research use only
  ([I13](../audit/02-safety-invariants.md#i13-research-use-only-status-is-stated-not-implied)).
* No accuracy figure, of any kind, in any mode.
* No substitute for VCEP or laboratory review. The shipped rule sets encode
  published guidance, not gene-specific expert specifications.
* No production readiness of the bundled gene mechanism table, which is a curated
  seed (`gene-mechanism-0.1.0-curated-seed`) that must be replaced with
  authoritative ClinGen G2P data.
* Role-enforced sign-off is available but **off by default**
  ([ADR-09](../adr/09-audit-replay-signoff.md)); a production deployment must turn
  it on.

## The standard this work is judged against

Not agent count, model integrations, or visual polish. The question is whether one
variant can be processed reproducibly, explainably, evidence-linked, auditably,
safely on missing evidence, model-independently, and human-reviewably — while
being honest about its limitations.

The first vertical slice does that, and `demo_data/review_demo.vcf` demonstrates it
end to end: two alleles classified from recorded ClinVar evidence, one abstaining
because a synonymous consequence with no splice predictor configured is not
evidence of benignity. What remains is breadth of evidence, then measurement.
