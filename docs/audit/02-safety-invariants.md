# 02 — Safety Invariants

**Purpose.** Every promise this project makes, stated as an invariant, with the
mechanism that enforces it and the test that proves it. If a change breaks one of
these, the change is wrong — not the test.

All test names below exist in `tests/core/` or `tests/test_debate.py` and were
verified against the collected suite (659 tests, 422 of them in `tests/core/`).

An invariant enforced only by a code comment is not enforced. Where the
guarantee is *structural* — the offending state cannot be constructed at all —
that is noted, because it is strictly stronger than a test.

---

## I1. No evidence record, no ACMG criterion

> Every applied criterion cites at least one structured evidence record.

**Why.** This is the whole product. A criterion without a record cannot be
checked, dated, attributed, or re-derived, so the classification cannot be
audited — and an unauditable classification is not usable by a laboratory.

**Mechanism.** Structural, not conventional. `AppliedCriterion` requires a
non-empty `evidence_ids`; the model rejects an empty tuple at construction. There
is no code path that appends a criterion from a string, a model reply, or a
heuristic, because no such entry point exists.

**Tests.** `test_applied_criterion_without_evidence_is_unconstructable`,
`test_applied_criteria_keep_their_evidence_ids`,
`test_a_criterion_in_any_other_state_may_lack_evidence` (a criterion that was
*rejected* or *indeterminate* legitimately has no evidence — the requirement
applies to what was acted on),
`test_every_review_carries_evidence_records_and_gaps`.

See [ADR-03](../adr/03-evidence-first-acmg.md).

---

## I2. Missing evidence is never converted into negative evidence

> "We could not retrieve it" and "we retrieved it and it was absent" are
> different facts with different consequences.

**Why.** The most dangerous error in variant review is treating an unqueried
database as a reassuring one. PM2 requires rarity *observed in a population
database*; a retrieval failure must not satisfy it. Conversely, BA1 requires an
observed frequency above 5% — silence must not be read as "not common".

**Mechanism.** `EvidenceStatus` has five values — `present`, `unavailable`,
`retrieval_failed`, `invalid`, `not_configured` — and only `present` records may
carry an `observed_value`, an applicability of `applies`, or a verification of
`verified`. Model invariants reject the combinations: a record with
`status != present` that carries an observed value fails validation. Derivation
consumes only records where `usable_as_evidence` is true, and everything else
becomes an explicit gap that reaches the contract as a limitation.

**Tests.** `test_a_pack_that_encodes_missing_data_as_a_value_is_refused`,
`test_a_timestamp_without_a_status_is_refused`,
`test_missing_record_describes_its_own_gap`,
`test_a_gap_is_never_deleted_by_validation`,
`test_no_evidence_at_all_abstains`, `test_unusable_evidence_only_abstains`,
`test_pm2_is_indeterminate_when_observation_quality_is_unknown`,
`test_missing_evidence_is_explained_in_prose`,
`test_gap_records_are_present_for_the_unrecorded_allele`.

See [ADR-06](../adr/06-evidence-ledger.md).

---

## I3. A model may explain; it may never create evidence or classify

> LLM output is confined to a narrative block. It cannot write to the ledger,
> assign a criterion, change a tier, or lift an abstention.

**Why.** Models fabricate plausible citations, invert negations, and disagree
with themselves across calls. They are useful for turning a structured decision
into readable prose. They are not a source of truth, and the architecture must
not depend on their restraint.

**Mechanism.** Layered, because no single layer is sufficient:

1. **Ordering.** Classification completes before any model is called. The
   explanation path receives a finished, frozen contract and returns prose.
2. **Structural.** `PersonaOpinion` has no `stance` and no `acmg_criteria`
   field; `DebateResult` has no `classification`. There is nowhere to put a tier,
   so no consumer can read one by accident.
3. **Redaction.** Tier vocabulary, ACMG codes (including invented ones) and
   strength modifiers are stripped from model text, and every removal is reported
   rather than silent.
4. **Detection.** `audit_explanation` compares codes mentioned in prose against
   codes actually applied, flagging `unknown_criterion:` and
   `criterion_not_applied:` separately.

**Tests.** `test_a_model_cannot_add_a_criterion`,
`test_a_model_cannot_change_the_label`,
`test_a_model_cannot_change_an_abstention`,
`test_explanation_cannot_change_the_classification`,
`test_a_classifying_model_is_redacted_and_reported`,
`test_the_redaction_is_visible_not_silent`,
`test_duplicate_redactions_are_collapsed`,
`test_a_well_behaved_model_produces_no_violations`,
`test_an_uncited_evidence_id_is_flagged`, `test_a_real_evidence_id_is_not_flagged`,
`test_boundary_violations_are_recorded_on_the_block`,
`test_explanation_lives_in_its_own_block`,
`test_the_report_escapes_model_output`,
`test_a_flagged_narrative_carries_a_stronger_disclaimer`.

The legacy `_extract_stance` — which read a tier out of prose with a regex — is
gone, and `tests/test_debate.py` asserts its absence structurally.

See [ADR-04](../adr/04-llm-boundary.md).

---

## I4. No confidence score, probability, or numeric certainty anywhere

**Why.** The legacy engine returned `confidence=0.95` for Pathogenic and `0.75`
for VUS as hardcoded constants ([audit A4](01-current-state-audit.md)). Such a
number reads as a calibrated probability and is not one. A tier plus its
supporting evidence plus its gaps is honest; a decimal attached to a tier is not.

**Mechanism.** No such field exists in the contract, the evidence model, the
engine outcome, or the audit record. The contract is `extra="forbid"` and frozen,
so a field cannot be smuggled in by a caller or a future contributor without
changing the schema and its version.

**Tests.** `test_no_confidence_or_probability_field_exists`,
`test_no_confidence_score_anywhere_in_the_contract`,
`test_no_command_produces_a_confidence_score`,
`test_outcome_carries_no_confidence_score`.

---

## I5. Every result requires human review; nothing is autonomous

**Why.** NGS-Agent is research-use-only infrastructure. It has not undergone
clinical validation and is not a medical device. The engine's job is to make a
reviewer's decision *better informed and fully traceable*, never to replace it.

**Mechanism.** `requires_human_review` is not computed — it is always true. A
fresh result is `pending` and unsigned. Sign-off requires a named reviewer;
approval and rejection are distinct actions; rejection requires notes.

**Tests.** `test_every_result_requires_human_review`,
`test_every_outcome_requires_human_review`, `test_a_fresh_result_is_unsigned`,
`test_default_review_is_pending_and_unsigned`,
`test_approval_produces_a_signed_contract`,
`test_approval_without_a_reviewer_is_refused`,
`test_reviewer_identity_is_mandatory`,
`test_disclaimer_points_at_the_signed_path`.

See [ADR-09](../adr/09-audit-replay-signoff.md).

---

## I6. A human override is recorded; it never rewrites the engine

**Why.** A reviewer must be able to disagree with the engine — that is the point
of review. But the disagreement must remain visible. If an override silently
replaced the derived label, the audit trail would record a conclusion the engine
never reached, and no later reader could tell.

**Mechanism.** An override requires a reason, appends a `HUMAN OVERRIDE`
limitation, and preserves the engine's original label alongside the reviewer's
decision. A request for review cannot carry an override.

**Tests.** `test_an_override_requires_a_reason`,
`test_override_requires_a_reason`, `test_an_override_with_a_reason_succeeds`,
`test_override_is_recorded_but_never_rewrites_the_engine`,
`test_request_review_cannot_carry_an_override`.

---

## I7. Contradiction is surfaced, never silently resolved

**Why.** "The evidence conflicts" and "there is not enough evidence" both end at
VUS, but they demand different human action: one requires adjudicating
disagreeing sources, the other requires obtaining more data. Collapsing them — as
the legacy engine did ([audit A6](01-current-state-audit.md)) — destroys the
information a reviewer needs.

**Mechanism.** Conflicts are first-class records with a kind, a severity, the
competing values and their evidence ids. `decision_state` distinguishes
`classified`, `insufficient_evidence`, `conflict`, `abstained` and
`manual_review_required`. A blocking conflict forces abstention.

**Tests.** `test_ba1_with_pathogenic_evidence_is_a_blocking_conflict`,
`test_two_authoritative_sources_disagreeing_is_a_conflict`,
`test_contradicting_external_and_criteria_is_a_blocking_conflict`,
`test_source_reported_conflict_is_not_usable`,
`test_abstention_states_require_the_abstained_flag`,
`test_abstained_with_classified_state_is_refused`.

---

## I8. Abstention is explicit, and machines can branch on it

**Why.** A system that always answers will eventually answer wrongly with
confidence. Refusing must be a first-class, machine-readable outcome — not a VUS
with an apologetic string attached.

**Mechanism.** `abstained` is a boolean on the classification, constrained so
that it is true exactly when `decision_state` is one of
`insufficient_evidence`, `conflict` or `abstained`. The CLI exits `3` when any
variant abstained, so a pipeline can branch without parsing prose. Exit codes:
`0` success, `2` usage, `3` abstained, `4` replay diverged.

**Tests.** `test_no_evidence_at_all_abstains`,
`test_source_none_abstains_on_everything`, `test_incomplete_normalization_abstains`,
`test_non_primary_contig_abstains`, `test_exit_code_signals_abstention`,
`test_the_abstained_variant_explains_why`,
`test_human_readable_rendering_names_the_indeterminate_criteria`,
`test_consequence_alone_is_never_pvs1`,
`test_null_in_lof_gene_without_transcript_context_is_indeterminate`.

See [ADR-08](../adr/08-result-contract.md).

---

## I9. Identity is explicit, and a locus is not an allele

**Why.** Two alleles at one position can carry opposite classifications. At
`chr17:43082434` (GRCh38), `G>A` is BRCA1 c.4327C>T p.Arg1443Ter — Pathogenic,
expert-panel reviewed — while `G>C` at the *same* position and the *same* dbSNP id
`rs41293455` is c.4327C>G p.Arg1443Gly — Benign. Any matching by position, or by
rsid, conflates them.

**Mechanism.** Variants are keyed by `build|chromosome|position|reference|alternate`
and matched by canonical SPDI, with coordinate span as a fallback. A record found
at the locus but not the allele is reported as `unavailable`, never adopted.
Multiallelic records are split before normalization; indels are left-aligned and
flag when left-alignment could not be verified. No build is ever guessed: absent
a declared build the pipeline raises ([audit B3](01-current-state-audit.md)).

**Tests.** `test_a_different_position_is_never_a_match`,
`test_evidence_about_a_different_variant_is_a_different_record`,
`test_gene_filter_never_relabels_a_variant`,
`test_gene_is_resolved_and_its_resolution_recorded`,
`test_gene_resolution_provenance_is_kept`,
`test_a_gene_we_could_not_validate_says_so`,
`test_gene_of_record_applies_when_the_vcf_is_unannotated`,
`test_gene_of_record_does_not_override_an_annotated_gene`.

The locus-vs-allele cases are golden tests using real public-domain ClinVar
records, including the multi-allele loci above.

See [ADR-05](../adr/05-variant-identity.md).

---

## I10. The signed path is deterministic and replayable

**Why.** Reproducibility is the minimum standard for an accountable result. If
the same input can produce two different classifications, nothing downstream can
be trusted.

**Mechanism.** The engine is pure: no clock, no I/O, no global state. Timestamps
and identifiers come from an injected clock and from content hashing, so a test
can freeze them. Replay re-derives the decision from the stored contract using
the *recorded* rule set and version — not whatever is current — and any
divergence raises `ReplayDivergenceError` with a field-by-field report.

**Tests.** `test_two_runs_produce_the_same_contract`,
`test_stdout_is_byte_exact_json`,
`test_audit_id_is_deterministic_for_the_same_event`,
`test_an_audit_record_can_be_replayed_from_its_json_alone`,
`test_every_recorded_decision_replays`,
`test_replay_reproduces_a_recorded_decision`,
`test_replay_reproduces_the_golden_labels`,
`test_a_tampered_evidence_snapshot_diverges`,
`test_divergence_is_reported_field_by_field`,
`test_non_deterministic_fields_are_not_treated_as_divergence`,
`test_input_hash_is_recorded`,
`test_database_versions_are_recorded_for_the_run`.

Because determinism is the claim, the divergence test reports the exact JSON path
of the first difference rather than dumping two large dicts — a flake there must
be diagnosable, not merely visible.

See [ADR-09](../adr/09-audit-replay-signoff.md).

---

## I11. Nothing leaves the deployment boundary unless an operator allows it

**Why.** Raw reads and patient-derived variants are PHI-adjacent. Default
behaviour must be safe for an air-gapped laboratory, and any egress must be a
deliberate, announced act.

**Mechanism.** The default evidence source is `recorded`: bundled fixtures, no
network. `--source live` prints an egress warning naming the endpoint
(`eutils.ncbi.nlm.nih.gov`) before any request. Adapters declare `hosted_by` and
`requires_network`, and a live adapter without a source version is refused. No
raw FASTQ is uploaded anywhere by this path.

**Tests.** `test_default_configuration_is_offline`,
`test_air_gapped_deployment_can_still_run`,
`test_live_source_warns_about_egress`,
`test_live_adapter_declares_network_and_the_eutils_endpoint`,
`test_live_adapter_without_a_source_version_is_refused`,
`test_network_adapter_builds_when_explicitly_allowed`,
`test_hosted_by_records_where_data_was_produced`,
`test_configuration_hash_excludes_secrets`,
`test_recorded_adapter_does_not_declare_a_live_endpoint`.

---

## I12. A failure is never presented as a success

**Why.** The legacy `consult` command exited 0 and wrote an HTML report when every
model call had failed ([audit A5](01-current-state-audit.md)). An empty report
reads as "reviewed, nothing found".

**Mechanism.** Missing backends raise rather than returning empty results. When
every model call fails the command exits 1 and refuses to write a report; a
partial failure still writes one but says so on stdout. Stale schema versions and
tampered packs are refused outright.

**Tests.** `test_a_missing_backend_is_an_error_not_an_empty_result`,
`test_a_missing_pack_file_is_an_error_not_an_empty_result`,
`test_a_missing_input_file_is_a_usage_error`,
`test_a_stale_schema_version_is_refused`, `test_a_tampered_pack_is_refused`,
`test_exit_is_nonzero_and_no_report_is_written`,
`test_a_partial_failure_still_reports_and_says_it_is_partial`,
`test_missing_audit_dir_is_called_out`.

---

## I13. Research-use-only status is stated, not implied

**Why.** Overstating readiness is itself a safety defect. A tool that looks
clinical will be used clinically.

**Mechanism.** `research_use_only: true` and a disclaimer are fields on every
contract, rendered in every report and on the CLI's stderr. They are not
optional and cannot be turned off by a flag.

**Tests.** `test_disclaimer_points_at_the_signed_path`,
`test_the_report_shows_redactions`, plus the contract tests asserting the fields
are present and immutable.

---

## The eight questions every pull request must answer

These are the invariants operationalised as review procedure. A PR that cannot
answer them is not ready, regardless of whether its tests pass.

1. **Does it change the signed classification path?** If yes, determinism (I10)
   and the contract version (I4, [ADR-08](../adr/08-result-contract.md)) are in
   scope, and replay of previously recorded decisions must still succeed.
2. **Does it add an evidence source?** Then it must declare `hosted_by`,
   `requires_network`, source version and adapter version (I11), and it must be
   testable with no network at all.
3. **Is provenance preserved end to end?** Source, version, retrieval time,
   build, transcript, variant identity, observed value, applicability,
   verification, limitations (I1, I2).
4. **Is the result replayable from the audit record alone?** (I10)
5. **What happens when evidence is missing?** The answer must never be "it is
   treated as absent" (I2).
6. **Could a model fabricate anything here?** (I3)
7. **Could this be mistaken for a clinical decision?** (I5, I13)
8. **Which test proves each of the above?** A named test, or the PR adds one.
