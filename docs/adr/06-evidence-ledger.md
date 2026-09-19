# ADR-06 — The evidence ledger and adapters

**Status.** Accepted and implemented in `ngs_agent/core/evidence/`. Evidence
schema `1.0.0`. Adapters shipped: ClinVar (recorded and live), gene/disease
mechanism (bundled table), offline pack.

## Context

[ADR-03](03-evidence-first-acmg.md) makes classification a function of evidence
records. That only holds if the records themselves are trustworthy: typed,
validated, dated, attributed, and honest about absence. Public databases change
daily, so a result that does not record *which version* of a database it consulted
cannot be reproduced or audited later.

The legacy path had no such layer — criteria arrived as strings
([audit A1](../audit/01-current-state-audit.md)).

## Decision

### One record type, nineteen fields, four of them about trust

`EvidenceRecord` is a frozen pydantic model:

| Field | Purpose |
|---|---|
| `schema_version`, `evidence_id` | Versioned identity; ids are namespaced `ev.v1.…` |
| `source` | `EvidenceSource`: name, version, adapter_version, hosted_by, license |
| `data_type` | One of 14 typed classes (below) |
| `status` | `present` \| `unavailable` \| `retrieval_failed` \| `invalid` \| `not_configured` |
| `retrieved_at` | When *we* fetched it, not when the source last updated |
| `genome_build` | Build the coordinates belong to |
| `queried_variant_identity` | What we asked for |
| `observed_variant_identity` | What the source actually described — may differ |
| `transcript`, `accession`, `gene` | Context the observation is only meaningful within |
| `observed_value` | The observation itself, structured |
| `applicability` | `applies` \| `does_not_apply` \| `indeterminate` |
| `verification` | `verified` \| `identity_unverified` \| `source_reported_conflict` \| `not_verified` |
| `strength_hint` | Optional; the engine decides strength, never the source |
| `limitations` | Per-record caveats that propagate to the contract |
| `retrieval` | `RetrievalDetail`: transport, endpoint, response digest |
| `provenance` | Free-form but hashed with the record |

The separation of `queried_variant_identity` from `observed_variant_identity` is
what makes [ADR-05](05-variant-identity.md)'s locus-vs-allele rule enforceable: if
a source returns a record about a *different* allele, the two fields disagree and
the record is not usable for ours.

### Fourteen typed data classes, not a generic bag

`clinical_significance`, `allele_frequency`, `molecular_consequence`,
`gene_disease_mechanism`, `nmd_escape_prediction`, `splicing_prediction`,
`missense_prediction`, `functional_assay`, `de_novo`, `segregation`, `case_control`,
`protein_structure`, `mutational_hotspot`, `sequence_constraint`.

Typing matters because each criterion needs a specific shape: PM2 needs an
*exact* frequency with its ancestry scope, PVS1 needs consequence plus mechanism
plus NMD context. An untyped blob would push parsing into the engine, where a
misparse becomes a misclassification.

Frequencies are exact integer ratios (`ExactRatio`, numerator/denominator) rendered
to decimal strings — never binary floats, which cannot represent most allele
frequencies exactly and would make threshold comparisons (`BA1 > 5%`,
`PM2 < 1e-4`) platform-dependent.

### Invariants make the dangerous states unconstructable

Enforced by model validation, not convention:

* `status != present` ⇒ no `observed_value`, no `strength_hint`, `applicability`
  not `applies`, `verification` not `verified`.
* `applicability == applies` ⇒ `verification == verified`.

So "a retrieval failure that somehow carries a value" cannot exist. This is the
structural core of **missing evidence is never negative evidence**: an
`unavailable` record has nothing in it that a derivation could misread as an
observation.

### Validation may downgrade, never upgrade, and never deletes a gap

`validate_evidence` returns a `ValidationReport` of `ValidationFinding`s with a
`ValidationSeverity`. `_downgrade` can demote a record (e.g. to `invalid`) when it
fails checks; nothing in the layer can promote a record to `present` or fabricate a
value. Gaps are preserved: `test_a_gap_is_never_deleted_by_validation`, and
`summarize_gaps` turns absent records into human-readable statements that reach the
contract as limitations.

### Adapters declare themselves

```python
@dataclass(frozen=True)
class AdapterDeclaration:
    name: str
    version: str                 # the SOURCE's version (e.g. a ClinVar release)
    adapter_version: str         # OUR parsing code's version
    data_types: tuple[EvidenceDataType, ...]
    endpoint: str | None = None
    license: str | None = None
    requires_network: bool = True
    hosted_by: str = "vendor"
    notes: str = ""
```

Two version numbers, because they change for different reasons: a ClinVar release
changes the data, an adapter release changes how we read it. Both are recorded in
the contract, so a divergence can be attributed to the right one.

`requires_network` and `hosted_by` make the egress posture auditable
(`test_live_adapter_declares_network_and_the_eutils_endpoint`,
`test_recorded_adapter_does_not_declare_a_live_endpoint`,
`test_hosted_by_records_where_data_was_produced`). A live adapter with no source
version is refused outright (`test_live_adapter_without_a_source_version_is_refused`) —
an undated source cannot support a reproducible claim.

Adapters are independently testable: each takes a transport, so tests inject
recorded responses with no network.

### Caching is a reproducibility mechanism, not an optimization

From `cache.py`:

```
<cache_root>/v1/<key>.json     one serialized EvidenceRecord
<cache_root>/index.jsonl       append-only (key, evidence_id, retrieved_at, source_version)
key = sha256(adapter_name | adapter_version | variant_identity | data_type)
```

The cache stores the *full* record including retrieval timestamp, source version
and response digest, so a re-run either reuses the exact bytes (air-gapped) or
detects that the source moved. `adapter_version` is in the key, so upgrading our
parsing code can never silently reuse a record produced by the previous version.

The cache is never a source of truth, and it is off by default (`cache_dir=None`).

### Recorded fixtures are the default source

`--source recorded` uses bundled, versioned fixtures
(`ngs_agent/core/data/recordings/clinvar/` with a manifest naming the ClinVar
release and recording time). This makes the engine runnable and testable with no
network — the air-gapped case is the default case, not a special mode.
`--source live` warns about egress to `eutils.ncbi.nlm.nih.gov` before any request;
`--source none` abstains on everything.

The offline pack adapter refuses packs that encode missing data as a value
(`test_a_pack_that_encodes_missing_data_as_a_value_is_refused`), that are tampered
with, or whose schema version is stale — a pack is untrusted input, not a
privilege escalation.

## Rejected alternatives

**A generic `dict` evidence bag.** Rejected: no invariants, so `status=unavailable`
with a value becomes representable, which is exactly the confusion I2 forbids.

**Retrying a failed retrieval until it succeeds, then recording success.**
Rejected: `retrieval_failed` is a fact about this run and must remain one. Retries
happen inside the transport; the record states what was ultimately true.

**Treating "not in ClinVar" as evidence of benignity.** Rejected — absence from a
database is absence of assertion, not an assertion of absence.

**Letting sources set criterion strength.** Rejected: `strength_hint` is advisory
and the rule set decides. Otherwise a source's editorial policy would silently
become our classification policy, and rule-set changes
([ADR-07](07-deterministic-engine.md)) would not be reproducible.

**Writing directly to the ledger from the explanation layer.** Rejected; see
[ADR-04](04-llm-boundary.md).

## Consequences

**Gains.** Every criterion is traceable to a dated, versioned, typed observation.
Reproducibility is possible without re-querying live databases. Air-gapped
operation is the default. Adapters can be added without touching the engine.

**Costs.**

* **Only two real sources are wired** (ClinVar, gene mechanism). Consequently
  12 of 14 data types have no adapter, many criteria are `not_evaluated`, and
  abstention is common. This is the honest state and the main limitation of the
  current slice. gnomAD is next; see
  [item 10](../design/10-benchmark-and-roadmap.md).
* **The bundled gene mechanism table is a curated seed**, versioned
  `gene-mechanism-0.1.0-curated-seed`, and its declaration says so
  (`license="Apache-2.0 (bundled curation seed); replace with ClinGen G2P data in
  production"`). It must be replaced with authoritative ClinGen G2P data before
  any production use. Shipping a seed table is a deliberate trade: PVS1 needs a
  mechanism, and abstaining on every LoF variant would make the slice
  undemonstrable.
* **Recorded fixtures go stale.** They are versioned and dated precisely so
  staleness is visible rather than silent.

## Verification

`test_a_pack_that_encodes_missing_data_as_a_value_is_refused`,
`test_a_timestamp_without_a_status_is_refused`,
`test_a_gap_is_never_deleted_by_validation`, `test_missing_record_describes_its_own_gap`,
`test_no_evidence_at_all_abstains`, `test_unusable_evidence_only_abstains`,
`test_source_reported_conflict_is_not_usable`,
`test_live_adapter_without_a_source_version_is_refused`,
`test_a_stale_schema_version_is_refused`, `test_a_tampered_pack_is_refused`,
`test_default_configuration_is_offline`, `test_air_gapped_deployment_can_still_run`,
`test_live_source_warns_about_egress`, `test_source_none_abstains_on_everything`,
`test_database_versions_are_recorded_for_the_run`.

83 tests across `tests/core/test_evidence_models.py` (31) and
`tests/core/test_evidence_adapters.py` (52).

Invariant [I2](../audit/02-safety-invariants.md#i2-missing-evidence-is-never-converted-into-negative-evidence).
