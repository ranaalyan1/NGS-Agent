# ADR-09 — Audit trail, human sign-off, and replay

**Status.** Accepted and implemented in `ngs_agent/core/audit.py` and
`ngs_agent/core/review.py`. Audit schema `1.0.0`. CLI commands: `audit`,
`replay`, `sign-off`.

## Context

An accountable result must survive the process that produced it. Three questions
have to be answerable months later, by someone who was not there:

1. **What was decided, from what, by which rules?** (the record)
2. **Would the same inputs produce the same decision today?** (replay)
3. **Which human accepted responsibility for it?** (sign-off)

The legacy code answered none of them: no history, no reproducibility check, no
reviewer identity ([audit A1](../audit/01-current-state-audit.md),
[A5](../audit/01-current-state-audit.md)).

## Decision

### An append-only JSONL log of self-contained records

`AuditLog(directory)` writes one JSON object per line. `AuditRecord` carries 31
fields — enough to reconstruct the decision without any other file:

| Group | Fields |
|---|---|
| Identity | `schema_version`, `audit_id`, `action`, `created_at`, `run_id`, `parent_audit_id` |
| Variant | `variant_id`, `variant_identity`, `genome_build`, `gene`, `transcript`, `gene_resolution` |
| Inputs | `input_hashes`, `configuration_hash` |
| Versions | `engine_version`, `normalization_version`, `rule_set`, `rule_set_version`, `database_versions`, `adapter_declarations` |
| Snapshots | `evidence_snapshot`, `result_snapshot` |
| Review | `reviewer`, `reviewer_role`, `reviewer_action`, `decision`, `override_reason`, `classification_before`, `classification_after` |
| Model | `model_metadata` |
| Replay | `replay_of`, `replay_reproduced`, `replay_divergence` |

JSONL was chosen over a database deliberately: it is append-only by construction,
readable with `cat`, diffable, and requires no service — which matters for
air-gapped deployments ([I11](../audit/02-safety-invariants.md#i11-nothing-leaves-the-deployment-boundary-unless-an-operator-allows-it)).
`hash_file()` digests the whole log, so tampering or truncation is detectable
(`test_audit_lists_records_and_hashes_the_log`,
`test_a_tampered_evidence_snapshot_diverges`).

`evidence_snapshot` stores the records themselves, not references to them. A
reference into a database that has since been updated would make replay
meaningless; the snapshot is what makes an audit record self-contained.

### Audit ids are deterministic

```python
def new_audit_id(*, action: str, variant_id: str, created_at: datetime) -> str
```

Derived from the action, the variant and the timestamp — so the same event hashes
to the same id, and an id cannot be chosen to collide or to impersonate
(`test_audit_id_is_deterministic_for_the_same_event`,
`test_audit_id_differs_by_action_and_variant`).

### Lineage is three queries, because one is not enough

```python
chain(audit_id)        # ancestry: what led to this record
descendants(audit_id)  # what this record led to
lineage(audit_id)      # ancestors + self + descendants
```

`chain` alone is insufficient: a sign-off or a replay is a *descendant* of a
review, not an ancestor, so walking only upward from a review finds nothing. Each
record links via `parent_audit_id`, giving a tree rather than a flat list — a
review, its sign-off, and three replays of it form one lineage.

This was a real implementation trap: the first version of the lineage command used
`chain` and silently reported sign-offs as unrelated.

### Replay re-derives from the record alone

```python
class ReplayResult(BaseModel):
    audit_id, variant_id, reproduced
    original_label,     replayed_label
    original_decision_state, replayed_decision_state
    divergence: dict[str, Any]
    evidence_record_count, rule_set, notes
    replayed_result: dict[str, Any]
    replay_audit_id: str | None
```

`replay_decision` reconstructs the variant from `evidence_snapshot`, re-runs the
engine using the **recorded** `rule_set` and `rule_set_version` — not whatever is
current — and compares. Using the recorded rule set is essential: replaying a 2015
decision under the SVI rule set would "diverge" for a reason that has nothing to do
with determinism.

Comparison is field-by-field after `_strip_non_deterministic` removes values that
legitimately differ between runs (`generated_at`, `audit_id`, `audit_path`,
`run_id`). Everything else must match. Divergence produces a path-keyed diff and
the CLI exits `4`, so a broken reproducibility guarantee fails loudly in a
pipeline rather than being logged
(`test_divergence_is_reported_field_by_field`,
`test_non_deterministic_fields_are_not_treated_as_divergence`,
`test_an_audit_record_can_be_replayed_from_its_json_alone`,
`test_every_recorded_decision_replays`).

A replay is itself an auditable event: it appends a record with
`action="replay"`, `parent_audit_id` pointing at the review, and
`replay_reproduced` recording the outcome. Replaying is not a read-only
operation, because *who checked, and when, and what they found* is part of the
history.

### Sign-off requires a named human, and disagreement requires a reason

```python
class ReviewDecision(BaseModel):          # extra="forbid", frozen=True
    reviewer: str = Field(min_length=1)
    action: ReviewerAction                # approve | reject | request_review
    decision: str | None = None           # the reviewer's tier, if any
    reviewer_role: str | None = None
    override_reason: str | None = None    # mandatory when decision differs
    notes: str | None = None              # mandatory on rejection
    signed_at: datetime | None = None
```

Validated at construction:

* A blank reviewer raises — *"A sign-off requires a reviewer identity."*
* A `decision` must be one of the five ACMG/AMP tiers.
* Rejection requires written notes — *"an unexplained rejection is not an
  auditable event."*
* A blank `override_reason` is refused.

`sign_off(result, decision)` returns a **new** contract; the original is never
mutated (`frozen`, per [ADR-08](08-result-contract.md)). The audit record stores
`classification_before` and `classification_after`, so an override preserves both.
A `HUMAN OVERRIDE` limitation is appended, and the engine's label is never
rewritten
(`test_override_is_recorded_but_never_rewrites_the_engine`).

**Role enforcement is opt-in, and that is a considered trade.**
`enforce_role=False` by default, so a deployment can adopt the workflow before
wiring up its directory of qualified reviewers. The role is *recorded* either way,
so an unqualified sign-off is visible in the audit trail rather than silently
accepted. A deployment handling real reports should set `enforce_role=True` and
supply `permitted_roles`.

`require_signed(result)` is the gate for any downstream consumer that must not act
on unsigned output.

`request_review` cannot carry an override — asking for a second opinion is not a
decision (`test_request_review_cannot_carry_an_override`).

### Configuration is hashed, minus secrets

`configuration_hash(configuration)` digests the effective configuration so a
replay can detect that settings changed, while `test_configuration_hash_excludes_secrets`
proves API keys never enter the audit log. An audit trail that leaked credentials
would be a liability rather than a control.

## Rejected alternatives

**A SQL database for the audit log.** Rejected for the default path: it adds a
service dependency to an air-gapped deployment and makes "read the history"
require a client. JSONL plus a file hash gives tamper-evidence with no
infrastructure. A database sink can be added behind the same `AuditLog` interface.

**Replay against the current rule set.** Rejected: it would report divergence
whenever guidance is updated, destroying the signal.

**Mutable records with an in-place "corrected" field.** Rejected: an editable
record is not an audit trail. Corrections are new records linked by
`parent_audit_id`.

**Anonymous or role-only sign-off.** Rejected: accountability requires an
identity. A role without a name cannot be questioned later.

**Allowing the engine to auto-approve low-risk results.** Rejected outright —
[I5](../audit/02-safety-invariants.md#i5-every-result-requires-human-review-nothing-is-autonomous).
`require_human_review` is always true and is not computed from the evidence.

**Trusting replay to compare raw dicts.** Rejected: `generated_at` and audit paths
differ legitimately, so a naive comparison reports divergence on every run and the
check gets disabled. Hence the explicit non-deterministic-field allowlist.

## Consequences

**Gains.** Any recorded decision can be re-derived from its audit record alone,
with no network and no database. Sign-off is attributable and cannot be anonymous,
unexplained, or silently overriding. Tampering is detectable. Lineage answers
"what happened to this variant" across reviews, sign-offs and replays.

**Costs.**

* **Storage grows with snapshots.** Every review stores its full evidence set.
  Accepted: self-containment is what makes replay possible, and the alternative —
  dereferencing live sources — cannot be reproduced years later.
* **Role enforcement is off by default**, so an unqualified reviewer *can* sign.
  Mitigated by always recording the role, and documented above as a deployment
  decision. This is the weakest point of the current design and should be
  revisited before any production use.
* **Replay is only as good as the snapshot.** If a record was written before a
  field existed, replay reports the gap rather than guessing.

## Verification

52 tests in `tests/core/test_audit_replay.py`.

`test_audit_file_is_jsonl`, `test_audit_json_is_machine_readable`,
`test_audit_record_carries_the_evidence_snapshot`,
`test_audit_id_is_deterministic_for_the_same_event`,
`test_audit_id_differs_by_action_and_variant`,
`test_audit_lists_records_and_hashes_the_log`,
`test_audit_of_an_empty_directory_reports_zero`,
`test_audit_linkage_is_present_when_a_log_was_used`,
`test_an_audit_record_can_be_replayed_from_its_json_alone`,
`test_every_recorded_decision_replays`,
`test_replay_reproduces_a_recorded_decision`,
`test_replay_reproduces_the_golden_labels`,
`test_a_tampered_evidence_snapshot_diverges`,
`test_divergence_is_reported_field_by_field`,
`test_non_deterministic_fields_are_not_treated_as_divergence`,
`test_a_fresh_result_is_unsigned`, `test_approval_produces_a_signed_contract`,
`test_approval_without_a_reviewer_is_refused`,
`test_reviewer_identity_is_mandatory`,
`test_an_override_requires_a_reason`,
`test_override_is_recorded_but_never_rewrites_the_engine`,
`test_request_review_cannot_carry_an_override`,
`test_configuration_hash_excludes_secrets`.

Invariants [I5](../audit/02-safety-invariants.md#i5-every-result-requires-human-review-nothing-is-autonomous),
[I6](../audit/02-safety-invariants.md#i6-a-human-override-is-recorded-it-never-rewrites-the-engine),
[I10](../audit/02-safety-invariants.md#i10-the-signed-path-is-deterministic-and-replayable).
