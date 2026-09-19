# ADR-08 — The versioned result contract and abstention

**Status.** Accepted and implemented in `ngs_agent/core/contract.py`. Contract
schema `1.0.0`.

## Context

The contract is the single output type of the signed classification path. Every
interface — CLI, MCP, HTTP API, HTML report, audit log — serializes the same
object. If two surfaces could emit two shapes, the guarantees in
[item 02](../audit/02-safety-invariants.md) would hold only for whichever surface
happened to implement them.

A consumer also needs to know, without heuristics: which schema this is, whether
the engine abstained, what evidence existed and what did not, and whether a human
has signed it.

## Decision

### One frozen model, closed to extension

`VariantReviewResult` and every nested block are declared
`model_config = ConfigDict(extra="forbid", frozen=True)`.

* **`frozen`** — a result cannot be mutated after construction. This is what makes
  "the explanation layer cannot change the classification" a property of the type
  rather than of the explanation layer's discipline
  ([ADR-04](04-llm-boundary.md)).
* **`extra="forbid"`** — an unknown field is an error, not a silently dropped
  value. A consumer that adds `confidence` cannot: the field is rejected, which is
  how [I4](../audit/02-safety-invariants.md#i4-no-confidence-score-probability-or-numeric-certainty-anywhere)
  stays true as the codebase grows.

One field is typed so it cannot be turned off:

```python
research_use_only: Literal[True]
```

There is no value of this field that says "for diagnostic use". Removing the
disclaimer requires changing the type and the schema version.

### Top-level shape

```
schema_version, result_id, research_use_only, disclaimer
variant            VariantIdentity      id, chromosome, position, reference, alternate,
                                        genome_build, variant_type, accession, spdi,
                                        hgvs_g, transcript, gene, on_primary_contig
gene               GeneBlock            resolved symbol + how it was resolved
normalization      NormalizationBlock   left_aligned, left_shifted, multiallelic_split,
                                        complete, warnings …
classification     ClassificationBlock  see below
evidence           tuple[EvidenceRecord, ...]        usable records
evidence_gaps      tuple[EvidenceRecord, ...]        records explaining what is missing
evidence_coverage  tuple[EvidenceCoverage, ...]      usable | present_but_unusable | gap | not_queried
applied_criteria / rejected_criteria /
indeterminate_criteria / not_evaluated_criteria      all four buckets, never merged
external_classifications, conflicts
missing_evidence, limitations
provenance         ProvenanceBlock
review             ReviewBlock
explanation        ExplanationBlock
```

Three of these deserve emphasis:

* **`evidence_gaps` are records, not strings.** A gap carries the same source,
  status and limitations machinery as a real observation, so "ClinVar was queried
  and returned nothing for this allele" is representable and distinguishable from
  "no ClinVar adapter is configured".
* **All four criterion buckets are emitted.** Criteria that were rejected or
  indeterminate are as informative as those applied — arguably more so, since they
  explain why a variant stayed VUS. The legacy engine emitted only the codes it
  counted.
* **`limitations` includes criteria that were *not* applied.** This was a real
  defect found during implementation: `_outcome_limitations` originally iterated
  only `applied`, so caveats attached to indeterminate and rejected criteria never
  reached the contract
  ([audit B5](../audit/01-current-state-audit.md)). The output was well formed,
  every test passed, and it was still dishonest by omission. Fixed by passing
  `unresolved=[*indeterminate, *rejected]`.

### Classification block: the tier plus why

```python
label: str                       # machine value, e.g. "uncertain_significance"
display_label: str               # "VUS"
abstained: bool
decision_state: Literal["classified", "insufficient_evidence", "conflict",
                        "abstained", "manual_review_required"]
decision_basis: str
requires_human_review: bool
rule_set: str; rule_set_version: str; rule_set_citation: str
engine_version: str
concordance: str                 # agreement with external classifications
strength_counts: dict[str, dict[str, int]]
fired_rules: tuple[dict[str, str], ...]
```

`label` and `display_label` are separate because a machine value must be stable
while a display string may change. `fired_rules` carries the rule ids and
citations from [ADR-07](07-deterministic-engine.md), so the contract itself
explains which guideline clause produced the tier.

### Seven invariants, enforced at construction

```python
@model_validator(mode="after")
def _enforce_contract(self) -> VariantReviewResult:
```

1. **Schema version must match this build.** A stale version is refused:
   *"Refusing to hand a consumer a contract it cannot interpret."*
2. **Every applied criterion cites evidence** —
   *"The rule 'no evidence record, no ACMG criterion' is enforced at the contract
   level; this result cannot be emitted."* ([I1](../audit/02-safety-invariants.md#i1-no-evidence-record-no-acmg-criterion))
3. **`approved`/`rejected` requires a signature** — both a reviewer identity and a
   `signed_at` timestamp.
4. **A timestamp without a terminal status is meaningless** — `signed_at` set on a
   `pending` review is rejected.
5. **A reviewer decision differing from the engine label is an override** and
   requires an explicit `override_reason`.
6. **`abstained=True` is inconsistent with `decision_state="classified"`.**
7. **`decision_state` in {`insufficient_evidence`, `conflict`, `abstained`}
   requires `abstained=True`.**

Together 6 and 7 make abstention a two-way binding: the flag and the state cannot
disagree in either direction. A consumer may branch on whichever it finds more
natural and get the same answer.

### Abstention is a first-class outcome

`decision_state` distinguishes five situations that all deserve different human
responses:

| State | Meaning | `abstained` |
|---|---|---|
| `classified` | Rules fired on usable evidence | `False` |
| `insufficient_evidence` | Nothing usable; more data might change this | `True` |
| `conflict` | Sources or criteria contradict; needs adjudication | `True` |
| `abstained` | Policy trigger (e.g. incomplete normalization) | `True` |
| `manual_review_required` | Engine declines to conclude | `True` |

Collapsing these into "VUS" — as the legacy engine did
([audit A6](../audit/01-current-state-audit.md)) — discards the distinction between
"get more data" and "resolve a disagreement".

The CLI mirrors this in its exit code: `3` when any variant abstained, so a
pipeline can branch without parsing prose. Full set: `0` success, `2` usage error,
`3` abstained, `4` replay diverged.

### Provenance block: everything needed to reproduce

```
engine_version, normalization_version, contract_schema_version,
rule_set, rule_set_version, generated_at,
input_hashes        per-input sha256 (the VCF, the reference …)
database_versions   per-source versions actually consulted
adapters            declarations: name, version, adapter_version, hosted_by …
configuration_hash  excludes secrets
run_id, audit_id, audit_path
model_metadata      provider, model, prompt_hash — only if a model was used
environment
```

`input_hashes` plus `database_versions` plus rule-set version is what makes a
result reproducible without re-running anything: a reader knows the exact inputs,
the exact data vintage, and the exact rules.

`model_metadata` is `None` unless a model was consulted, and it records a
`prompt_hash` — so the narrative can be tied to the facts it was given
([ADR-04](04-llm-boundary.md)).

### Serialization must be byte-exact

Machine-readable output goes through `click.echo`, never a rich `Console`. Rich
word-wraps at terminal width by inserting newlines *inside string values*,
producing JSON that renders correctly for a human and fails to parse for a
machine — and the corruption varies with terminal width
([audit B4](../audit/01-current-state-audit.md)). Banners, panels and warnings go
to stderr; stdout carries only the contract.

## Rejected alternatives

**Per-interface DTOs.** Rejected: guarantees would have to be re-implemented per
surface, and divergence would be invisible until a consumer broke.

**`extra="allow"` for forward compatibility.** Rejected: it permits exactly the
 smuggling of a `confidence` field that
 [I4](../audit/02-safety-invariants.md#i4-no-confidence-score-probability-or-numeric-certainty-anywhere)
 forbids. Forward compatibility is handled by bumping `schema_version`, and
 check 1 refuses a contract this build cannot interpret.

**A single `vus_reason` string instead of five decision states.** Rejected: prose
 is not machine-readable, and the distinction between "insufficient" and
 "conflict" drives different human action.

**Numeric confidence alongside the tier.** Rejected outright — see
 [audit A4](../audit/01-current-state-audit.md).

**Letting the contract default `research_use_only` to `False`.** Rejected; the
 field is `Literal[True]`.

## Consequences

**Gains.** One shape everywhere. Unsafe states are unconstructable rather than
merely discouraged. Abstention is machine-readable at both the field and the
process-exit level. Provenance is complete enough to reproduce without re-running.

**Costs.**

* **The contract is verbose.** A VUS with no evidence still carries four empty
  criterion buckets, coverage entries, gaps and limitations. That verbosity *is*
  the auditability; a compact form would hide the absence of data.
* **Any field change is a schema-version change**, which consumers must handle.
  Accepted: silent shape changes are worse.
* **`frozen` makes in-place correction impossible.** A corrected result is a new
  result linked in the audit chain ([ADR-09](09-audit-replay-signoff.md)), not an
  edit. This is deliberate — an editable record is not an audit trail.

## Verification

37 tests in `tests/core/test_contract.py`, plus 54 in `test_cli_core.py` for the
serialized surface.

`test_no_confidence_or_probability_field_exists`,
`test_no_confidence_score_anywhere_in_the_contract`,
`test_outcome_carries_no_confidence_score`,
`test_no_command_produces_a_confidence_score`,
`test_abstained_with_classified_state_is_refused`,
`test_abstention_states_require_the_abstained_flag`,
`test_applied_criterion_without_evidence_is_unconstructable`,
`test_a_stale_schema_version_is_refused`,
`test_an_override_requires_a_reason`,
`test_override_is_recorded_but_never_rewrites_the_engine`,
`test_stdout_is_byte_exact_json`,
`test_two_runs_produce_the_same_contract`,
`test_exit_code_signals_abstention`,
`test_input_hash_is_recorded`,
`test_database_versions_are_recorded_for_the_run`,
`test_configuration_hash_excludes_secrets`.

Invariants [I4](../audit/02-safety-invariants.md#i4-no-confidence-score-probability-or-numeric-certainty-anywhere),
[I8](../audit/02-safety-invariants.md#i8-abstention-is-explicit-and-machines-can-branch-on-it),
[I10](../audit/02-safety-invariants.md#i10-the-signed-path-is-deterministic-and-replayable).
