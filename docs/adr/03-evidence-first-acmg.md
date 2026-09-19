# ADR-03 — Evidence-first ACMG classification

**Status.** Accepted and implemented. Supersedes the legacy
`compute_acmg_classification(list[str])`, which was deleted rather than adapted.

## Context

ACMG/AMP 2015 is a rule system over *criteria*, and criteria are judgments about
*observations*: an allele frequency measured in a population database, a
consequence called against a transcript, a classification asserted by an expert
panel. The legacy engine accepted the criteria as bare strings and never saw the
observations ([audit A1](../audit/01-current-state-audit.md)), which meant:

* a criterion could not be dated, attributed, or re-checked;
* the same string from three models counted three times ([audit A3](../audit/01-current-state-audit.md));
* the decision could not be re-derived later, because its real inputs were never
  recorded.

The project's central rule follows directly: **no evidence record, no ACMG
criterion.**

## Decision

Classification is a pure function of a typed evidence ledger. The chain is fixed
and one-directional:

```
EvidenceRecord  →  CriterionEvaluation  →  ClassificationOutcome  →  VariantReviewResult
   (observation)      (one criterion)         (rule combination)        (contract)
```

A criterion is *derived*, never asserted. Each of the 28 criterion specs in
`ngs_agent/core/acmg/criteria.py` declares what data type it needs and how to
read it; `ngs_agent/core/acmg/derivation.py` evaluates a spec against the records
actually present and produces a `CriterionEvaluation`:

```python
class CriterionEvaluation(BaseModel):        # derivation.py:110
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    state: str                                # applied | rejected | indeterminate | not_evaluated
    direction: Direction
    strength: Strength | None = None          # None unless state == applied
    guideline_strength: Strength              # before any rule-set modifier
    modifier: str | None = None               # set when a rule set changed it
    evidence_ids: tuple[str, ...] = ()
    reason: str
    citation: str
    rule_set: str
    limitations: tuple[str, ...] = ()
```

Three of those fields carry most of the design:

* **`state`** — a criterion that could not be evaluated is recorded as
  `indeterminate` or `not_evaluated` with a reason. It is not dropped, and it is
  not treated as satisfied.
* **`guideline_strength` + `modifier`** — the strength actually applied is kept
  separate from the strength the base guideline assigns, so a rule set that
  downgrades PM2 to supporting is visible in the output rather than buried in
  code. See [ADR-07](07-deterministic-engine.md).
* **`limitations`** — per-criterion caveats propagate to the contract, including
  from criteria that were *not* applied.

### Enforcement is at the contract boundary

The invariant is enforced where the result is constructed, so no caller — CLI,
MCP, API, or a future interface — can emit a violation:

```python
# contract.py:283
for criterion in self.applied_criteria:
    if not criterion.evidence_ids:
        raise ContractError(
            f"Applied criterion {criterion.code} carries no evidence_id. The rule "
            "'no evidence record, no ACMG criterion' is enforced at the contract level; "
            "this result cannot be emitted."
        )
```

Only *applied* criteria are required to cite evidence. A rejected or indeterminate
criterion legitimately has none — it was not acted on — and its reason and
limitations are what reach the reviewer.

Every numeric threshold lives in `DerivationConfig` and is versioned through the
rule set, because a constant hardcoded inside a function is an undocumented
clinical decision.

## Rejected alternatives

**Keep `list[str]` and attach evidence afterwards.** Rejected: the type would
still permit an unevidenced criterion, so the guarantee would rest on discipline
rather than construction, and every future call site would be a chance to break it.

**Let the engine accept model-proposed criteria and validate them.** Rejected:
validation can catch a malformed criterion but cannot manufacture the observation
behind it. This is the specific failure mode of [audit A2](../audit/01-current-state-audit.md),
and any entry point that accepts a code reopens it. See [ADR-04](04-llm-boundary.md).

**Allow heuristics for criteria with no adapter yet.** Rejected: it makes "no
source configured" indistinguishable from "source says yes". Criteria without an
adapter are `not_evaluated` with an explicit reason, and the result usually
abstains. That is the honest outcome, and abstention is cheap
([ADR-08](08-result-contract.md)).

## Consequences

**Gains.** Every criterion is attributable to a dated, versioned observation.
Decisions are re-derivable from stored records. Duplicate criteria cannot inflate
a classification, because `count_strengths()` counts a set of codes. A reviewer
can inspect exactly what was known and what was not.

**Costs, accepted deliberately.**

* **Coverage is limited by adapters.** With only ClinVar and the bundled gene
  mechanism table wired, many criteria are `not_evaluated` and many variants
  abstain. On the shipped demo VCF, one of three alleles abstains. This is
  correct behaviour, but it means the tool is less *decisive* than the legacy
  engine, which always answered. Item [10](../design/10-benchmark-and-roadmap.md)
  sequences the adapters that reduce abstention.
* **More code per criterion.** Each spec needs a data type, an applicability
  check and tests, rather than a string in a table.
* **No graceful degradation.** If an adapter breaks, the criterion becomes
  indeterminate and the result may abstain. We accept reduced throughput over
  plausible-looking output.

## Verification

`test_applied_criterion_without_evidence_is_unconstructable`,
`test_applied_criteria_keep_their_evidence_ids`,
`test_a_criterion_in_any_other_state_may_lack_evidence`,
`test_every_review_carries_evidence_records_and_gaps`,
`test_no_evidence_at_all_abstains`, `test_unusable_evidence_only_abstains`,
`test_missing_evidence_is_explained_in_prose`,
`test_null_in_lof_gene_without_transcript_context_is_indeterminate`,
`test_consequence_alone_is_never_pvs1`.

Invariant [I1](../audit/02-safety-invariants.md#i1-no-evidence-record-no-acmg-criterion).
