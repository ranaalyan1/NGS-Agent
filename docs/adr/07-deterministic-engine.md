# ADR-07 — The deterministic ACMG/AMP engine

**Status.** Accepted and implemented in `ngs_agent/core/acmg/`
(`criteria.py`, `rule_sets.py`, `derivation.py`, `engine.py`). Engine version
`core-1.0.0`; 28 criterion specs (16 pathogenic codes, 12 benign codes); 2 rule sets.

## Context

ACMG/AMP 2015 is a published, citable rule system. That is exactly why it should
be encoded as data and evaluated deterministically rather than approximated in
code branches: a reviewer must be able to see *which rule fired*, against *which
citation*, at *which strength*, and re-derive it later.

The legacy engine evaluated ad-hoc boolean expressions over string counts and
returned a hardcoded confidence
([audit A1](../audit/01-current-state-audit.md),
[A3](../audit/01-current-state-audit.md),
[A4](../audit/01-current-state-audit.md)).

## Decision

### Rules are data, versioned and cited

```python
class CombinationRule(BaseModel):
    rule_id: str                              # e.g. "R2015-P-i-a"
    produces: ClassificationLabel
    requires: dict[Strength, int]             # minimum counts per strength
    at_most: dict[Strength, int]              # upper bounds, where the guideline has one
    direction: Direction
    citation: str = RICHARDS_2015
    text: str

class StrengthModifier(BaseModel):
    code: str
    action: Literal["modify_strength", "disable"]
    strength: Strength | None = None
    rationale: str
    citation: str = CLINGEN_SVI

class RuleSet(BaseModel):
    name: str
    version: str
    description: str
    citation: str
    combination_rules: tuple[CombinationRule, ...]
    modifiers: tuple[StrengthModifier, ...] = ()
    allow_reputable_source_criteria: bool = True
```

Every rule carries its `rule_id` and citation into the output as a `FiredRule`, so
a classification explains itself in the guideline's own terms.

Five labels, with display names kept separate from machine values:

| `label` | `display_label` | `tier_group` |
|---|---|---|
| `pathogenic` | Pathogenic | positive |
| `likely_pathogenic` | Likely Pathogenic | positive |
| `uncertain_significance` | VUS | uncertain |
| `likely_benign` | Likely Benign | negative |
| `benign` | Benign | negative |

Strengths: `stand_alone`, `very_strong`, `strong`, `moderate`, `supporting`.
Directions: `pathogenic`, `benign`.

### Two rule sets, both at v1.0.0

**`acmg-amp-2015`** (default) — Richards 2015 encoded verbatim, 18 combination
rules, no modifiers:

| Rule | Produces | Requires |
|---|---|---|
| R2015-P-i-a | pathogenic | 1 very_strong + 1 strong |
| R2015-P-i-b | pathogenic | 1 very_strong + 2 moderate |
| R2015-P-i-c | pathogenic | 1 very_strong + 1 moderate + 1 supporting |
| R2015-P-i-d | pathogenic | 1 very_strong + 2 supporting |
| R2015-P-ii | pathogenic | 2 strong |
| R2015-P-iii | pathogenic | 1 strong + 3 moderate |
| R2015-P-iv | pathogenic | 1 strong + 2 moderate + 2 supporting |
| R2015-P-v | pathogenic | 1 strong + 1 moderate + 4 supporting |
| R2015-LP-i | likely_pathogenic | 1 very_strong + 1 moderate |
| R2015-LP-ii | likely_pathogenic | 1 strong + 1 moderate (at most 2 moderate) |
| R2015-LP-iii | likely_pathogenic | 1 strong + 2 supporting |
| R2015-LP-iv | likely_pathogenic | 3 moderate |
| R2015-LP-v | likely_pathogenic | 2 moderate + 2 supporting |
| R2015-LP-vi | likely_pathogenic | 1 moderate + 4 supporting |
| R2015-B-i | benign | 1 stand_alone |
| R2015-B-ii | benign | 2 strong |
| R2015-LB-i | likely_benign | 1 strong + 2 supporting |
| R2015-LB-ii | likely_benign | 2 supporting |

**`acmg-amp-2015+clingen-svi-2020`** — the same 18 rules plus one, with three
modifiers:

* `PM2` → `modify_strength` to **supporting** (ClinGen SVI 2020: rarity alone is
  not moderate evidence).
* `PP5`, `BP6` → **disabled**. These are the "reputable source says so" criteria;
  the rule set also sets `allow_reputable_source_criteria = False`. They are
  deprecated because they launder another lab's reasoning into ours without
  evidence we can inspect — which [ADR-03](03-evidence-first-acmg.md) forbids.
* adds `SVI-LP-vii` → likely_pathogenic from **1 very_strong + 1 supporting**,
  the SVI cap that prevents PVS1 plus a single weak criterion reaching Pathogenic.

Precedence is fixed: pathogenic → likely_pathogenic → benign → likely_benign, so a
tie cannot resolve differently between runs.

The rule set name and version are recorded on every outcome
(`rule_set`, `rule_set_version`, `rule_set_citation`), and replay re-runs the
*recorded* rule set rather than the current default
([ADR-09](09-audit-replay-signoff.md)).

### Counting is over a set of codes

```python
def count_strengths(evaluations, direction) -> dict[Strength, int]:
    """Count *distinct applied criteria* per strength level.

    Counting uses a set of criterion codes. The legacy engine summed raw code
    occurrences, so three personas each mentioning PM2 produced ``pm=3`` and a
    Likely Pathogenic call from a single criterion. That inflation is
    structurally impossible here: one criterion contributes at most one count,
    at the strength the rule set assigns it.
    """
```

This closes [audit A3](../audit/01-current-state-audit.md) at the type level: the
input is `CriterionEvaluation`s, each derived from evidence, not strings.

### Safety asymmetry: fail toward VUS

From `derivation.py`:

> Safety asymmetry (deliberate): weak observation quality blocks *pathogenic*
> criteria (which risks a false-positive clinical action) but only limits
> *benign* criteria (which risks leaving a variant as VUS). Failing toward VUS is
> the intended failure mode.

The two error directions are not equally costly. A false Pathogenic can trigger
prophylactic surgery in a healthy person; a false Benign can end surveillance.
Both are serious, but the *asymmetric* response is that uncertain quality may not
support an escalation, while it may still contribute to de-escalation. Where
quality is unknown the variant stays VUS and a human decides.

### PVS1 requires positive context, and is never silently downgraded

PVS1 is the most consequential criterion, so its applicability is checked rather
than assumed. From `pvs1_strength_policy()`:

> PVS1 strength follows the decision tree of Abou Tayoun 2018. NGS-Agent applies
> PVS1 at very_strong only when transcript/exon context establishes that the
> variant is predicted to undergo nonsense-mediated decay (or is otherwise not in
> the last exon / last 50 bp of the final exon junction). Without that context
> PVS1 is recorded as indeterminate and is NOT applied at a reduced strength,
> because a downgrade requires positive evidence of NMD escape rather than the
> absence of evidence either way.

Two separate points, both easy to get wrong:

* A null consequence **alone** is never PVS1 — the gene's disease mechanism must
  be established as loss-of-function (`test_consequence_alone_is_never_pvs1`).
* Missing transcript context yields **indeterminate**, not a downgraded
  application. Absence of evidence about NMD escape is not evidence of NMD escape.

The policy is a function returning text, so it is inspectable and testable rather
than buried in derivation branches.

### Abstention is a policy object, not scattered conditionals

```python
class AbstentionPolicy(BaseModel):
    abstain_on_incomplete_normalization: bool = True
    abstain_on_blocking_conflict: bool = True
    abstain_when_no_usable_evidence: bool = True
    abstain_when_nothing_applies: bool = True
    abstain_on_unverified_supporting_evidence: bool = True
    require_human_review_always: bool = True
```

Every trigger is named and individually testable. `require_human_review_always` is
not configurable in practice — it is part of the invariant set
([I5](../audit/02-safety-invariants.md#i5-every-result-requires-human-review-nothing-is-autonomous)).

### Conflicts are records the engine refuses to resolve

```python
class ConflictRecord(BaseModel):
    """A contradiction the reviewer must resolve. Never resolved automatically."""
    kind: Literal["criteria_vs_criteria", "criteria_vs_external",
                  "within_source", "normalization", "build"]
    severity: Literal["blocking", "notable"]
    description: str
    evidence_ids: tuple[str, ...] = ()
    resolution: str = ("Requires human review; NGS-Agent does not resolve "
                       "conflicting evidence.")
```

Note the default `resolution` text: the engine states its own refusal in the
output. BA1 (allele frequency > 5%) alongside pathogenic criteria is a *blocking*
conflict — the variant cannot be both common and pathogenic — and forces
abstention. Contradiction between our derived criteria and an authoritative
external classification is likewise blocking.

### Evidence coverage is reported per data type

`EvidenceCoverage.status` is one of `usable`, `present_but_unusable`, `gap`,
`not_queried`. "We did not look" is distinguished from "we looked and found
nothing", which is what makes
[I2](../audit/02-safety-invariants.md#i2-missing-evidence-is-never-converted-into-negative-evidence)
visible to a reviewer rather than merely true internally.

### The engine is pure

No clock, no I/O, no globals, no randomness. Timestamps and ids are injected by the
pipeline. Repeated evaluation of the same inputs yields byte-identical JSON, which
is what makes replay possible
([ADR-09](09-audit-replay-signoff.md)).

## Rejected alternatives

**A point-based score (Tavtigian/ClinGen SVI: supporting=1, moderate=2, strong=4,
very_strong/stand_alone=8; P≥10, LP 6–9, VUS 0–5, LB −1…−6, B≤−7).** Not used as
the primary mechanism. It is a well-founded system, but the published combination
rules are what laboratories and VCEPs cite, and a numeric score reads like the
calibrated confidence [I4](../audit/02-safety-invariants.md#i4-no-confidence-score-probability-or-numeric-certainty-anywhere)
forbids. The SVI *recommendations* we do adopt are expressed as rule-set modifiers
and one extra rule, so they remain citable and inspectable.

**Gene-specific rule sets per VCEP.** Not implemented. The architecture supports it
(a rule set is data plus a version), but shipping specs we cannot validate against
truth sets would be worse than shipping none. Tracked in
[item 10](../design/10-benchmark-and-roadmap.md).

**Resolving conflicts automatically by preferring the higher-star source.**
Rejected: that is a policy decision with clinical consequences, and it would make
the engine's output depend on a heuristic rather than on evidence.

**Applying PVS1 at moderate strength when NMD context is missing.** Rejected — see
the policy above. Downgrading requires positive evidence of escape.

## Consequences

**Gains.** Every classification names the rules that fired, with citations and
strengths. Criteria are counted once each. Rule-set changes are versioned and
replayable. Abstention triggers are explicit and testable. Conflicts are surfaced
with their evidence. The engine cannot reach a tier without evidence.

**Costs.**

* **Fewer decisive answers.** With two evidence sources wired, most variants
  abstain or stay VUS. The engine is deliberately less assertive than the legacy
  one, which always produced a tier and a confidence.
* **Encoding the guidelines faithfully is laborious.** 18 rules, 28 criterion
  specs, per-criterion applicability checks — and each needs tests plus a citation.
* **Two rule sets to keep coherent.** A change to the 2015 set must be reviewed for
  its effect on the SVI set, which inherits its rules.
* **Rule sets are ours, not a VCEP's.** They encode published guidance, not
  gene-specific expert-panel specifications, so they cannot substitute for a VCEP
  review on any particular gene.

## Verification

79 tests in `tests/core/test_acmg_engine.py`, plus the derivation cases in
`test_vertical_slice.py`.

`test_full_pvs1_context_applies_at_very_strong`,
`test_consequence_alone_is_never_pvs1`,
`test_null_in_lof_gene_without_transcript_context_is_indeterminate`,
`test_a_strength_modifier_is_captured`,
`test_svi_adds_the_very_strong_plus_supporting_cap`,
`test_ba1_with_pathogenic_evidence_is_a_blocking_conflict`,
`test_two_authoritative_sources_disagreeing_is_a_conflict`,
`test_contradicting_external_and_criteria_is_a_blocking_conflict`,
`test_abstention_states_require_the_abstained_flag`,
`test_abstained_with_classified_state_is_refused`,
`test_no_evidence_at_all_abstains`,
`test_pm2_is_indeterminate_when_observation_quality_is_unknown`.

Invariants [I7](../audit/02-safety-invariants.md#i7-contradiction-is-surfaced-never-silently-resolved),
[I8](../audit/02-safety-invariants.md#i8-abstention-is-explicit-and-machines-can-branch-on-it).
