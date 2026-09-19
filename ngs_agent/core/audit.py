"""Audit trail, sign-off, and replay.

An audit record is a complete, self-contained account of one decision: the
input hashes, the engine and rule-set versions, the database versions, the
*full evidence snapshot*, and the *full result contract*. It is written to an
append-only JSON Lines log and never rewritten.

Why the evidence is embedded rather than referenced
---------------------------------------------------

A replay that re-fetches from ClinVar answers "what would we conclude with
today's data?", which is a legitimate but different question from "why did we
conclude what we concluded then?". Databases are updated continuously and
classifications change; an audit that depends on re-fetching is not an audit.
Embedding the snapshot costs storage and buys the only property that matters
here: the record can be replayed years later, offline, and must produce the
same answer.

Replay semantics
----------------

:func:`replay_decision` reconstructs the variant identity and evidence from the
audit record, re-runs the deterministic engine with the recorded rule set and
configuration, and diffs the reproduced contract against the stored one
(excluding timestamps and run-scoped identifiers). A divergence raises
:class:`~ngs_agent.core.errors.ReplayDivergenceError`, because it means the
signed classification path is not deterministic — which is the single property
the whole design rests on.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ngs_agent.core.acmg.derivation import DerivationConfig
from ngs_agent.core.acmg.engine import AcmgEngine
from ngs_agent.core.contract import VariantReviewResult
from ngs_agent.core.errors import AuditError, ContractError, ReplayDivergenceError
from ngs_agent.core.evidence.models import utc_now
from ngs_agent.core.genome import GenomeBuild
from ngs_agent.core.hashing import canonical_json_sha256, ga4gh_digest
from ngs_agent.core.ledger import EvidenceLedger, MemoryLedger, import_ledger_snapshot
from ngs_agent.core.normalization import NormalizationReport, NormalizedVariant, VariantType
from ngs_agent.core.review import ReviewDecision, sign_off
from ngs_agent.core.version import AUDIT_SCHEMA_VERSION, ENGINE_VERSION

AUDIT_LOG_FILENAME = "audit.jsonl"

#: Contract fields excluded when diffing a replay. Timestamps and run-scoped
#: identifiers differ by construction; everything else must not.
_NON_DETERMINISTIC_FIELDS = frozenset(
    {"result_id", "generated_at", "run_id", "audit_id", "audit_path", "signed_at", "retrieved_at"}
)


AuditActionType = Literal["review", "sign_off", "override", "explain", "replay"]


class AuditRecord(BaseModel):
    """One append-only entry in the audit log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = AUDIT_SCHEMA_VERSION
    audit_id: str
    action: AuditActionType
    created_at: datetime
    run_id: str | None = None
    #: Audit record this one derives from (sign-off -> review, replay -> review).
    parent_audit_id: str | None = None

    variant_id: str
    variant_identity: str
    genome_build: str
    gene: str | None = None
    transcript: str | None = None
    #: How the gene symbol was resolved and whether it could be validated.
    #: Stored so a replay can reconstruct the gene block faithfully; without it
    #: a replay diverges on ``$.gene`` and correctly reports that it cannot
    #: reproduce the recorded result.
    gene_resolution: dict[str, Any] | None = None

    #: SHA-256 of each input file that contributed to the decision.
    input_hashes: dict[str, str] = Field(default_factory=dict)
    configuration_hash: str | None = None
    engine_version: str = ENGINE_VERSION
    normalization_version: str = ""
    rule_set: str = ""
    rule_set_version: str = ""
    database_versions: dict[str, str] = Field(default_factory=dict)
    adapter_declarations: tuple[dict[str, Any], ...] = ()

    #: Full serialized evidence records used for this decision.
    evidence_snapshot: tuple[dict[str, Any], ...] = ()
    #: Full serialized result contract.
    result_snapshot: dict[str, Any] = Field(default_factory=dict)

    #: Sign-off specifics.
    reviewer: str | None = None
    reviewer_role: str | None = None
    #: What the reviewer did with the result: approve / reject / request_review.
    reviewer_action: str | None = None
    #: The reviewer's ACMG/AMP tier, which may differ from the engine label.
    decision: str | None = None
    override_reason: str | None = None
    classification_before: str | None = None
    classification_after: str | None = None

    #: LLM metadata, present only when a model touched the run.
    model_metadata: dict[str, Any] | None = None

    #: Replay specifics.
    replay_of: str | None = None
    replay_reproduced: bool | None = None
    replay_divergence: dict[str, Any] | None = None

    def to_json_line(self) -> str:
        return self.model_dump_json()


class AuditLog:
    """Append-only JSON Lines audit log."""

    def __init__(self, directory: Path | str, *, filename: str = AUDIT_LOG_FILENAME) -> None:
        self.directory = Path(directory)
        self.path = self.directory / filename
        self.directory.mkdir(parents=True, exist_ok=True)

    def append(self, record: AuditRecord) -> AuditRecord:
        """Append one record. Never rewrites existing lines."""
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(record.to_json_line() + "\n")
        return record

    def read_all(self) -> list[AuditRecord]:
        if not self.path.is_file():
            return []
        records: list[AuditRecord] = []
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(AuditRecord.model_validate_json(line))
                except (ValidationError, ContractError) as exc:
                    raise AuditError(
                        f"{self.path}:{line_number}: audit entry failed schema validation: {exc}"
                    ) from exc
        return records

    def find(self, audit_id: str) -> AuditRecord:
        for record in reversed(self.read_all()):
            if record.audit_id == audit_id:
                return record
        available = [item.audit_id for item in self.read_all()]
        raise AuditError(
            f"No audit record with id {audit_id!r} in {self.path}. "
            f"Available (most recent last): {available[-20:]}"
        )

    def latest_for_variant(self, variant_id: str) -> AuditRecord | None:
        for record in reversed(self.read_all()):
            if record.variant_id == variant_id and record.action == "review":
                return record
        return None

    def chain(self, audit_id: str) -> list[AuditRecord]:
        """The full ancestry of a record, oldest first."""
        by_id = {record.audit_id: record for record in self.read_all()}
        chain: list[AuditRecord] = []
        current = by_id.get(audit_id)
        seen: set[str] = set()
        while current is not None and current.audit_id not in seen:
            seen.add(current.audit_id)
            chain.append(current)
            current = by_id.get(current.parent_audit_id) if current.parent_audit_id else None
        return list(reversed(chain))

    def descendants(self, audit_id: str) -> list[AuditRecord]:
        """Records that cite ``audit_id`` as their parent, in log order.

        An append-only log can only grow, so the descendants of a review are
        its replays and sign-offs: the things that happened *to* the decision
        after it was made. Without this view a reviewer can see how a result
        was produced but not what anyone did about it.
        """
        return [record for record in self.read_all() if record.parent_audit_id == audit_id]

    def lineage(self, audit_id: str) -> list[AuditRecord]:
        """Ancestors, the record itself, and every descendant, in log order.

        This is the complete accountable history of one decision, and the view a
        reviewer or an auditor actually wants.
        """
        wanted = {record.audit_id for record in self.chain(audit_id)}
        frontier = [audit_id]
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for record in self.descendants(current):
                wanted.add(record.audit_id)
                frontier.append(record.audit_id)
        return [record for record in self.read_all() if record.audit_id in wanted]

    def hash_file(self) -> str:
        """SHA-256 of the whole log, for sealing/verification."""
        from ngs_agent.core.hashing import sha256_file

        return sha256_file(self.path) if self.path.is_file() else ""


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def new_audit_id(*, action: str, variant_id: str, created_at: datetime) -> str:
    """Deterministic-but-unique audit id.

    Derived from the action, variant, and timestamp, so it is stable enough to
    be readable and unique enough not to collide within a run. A run id may be
    mixed in by the caller through ``created_at`` precision.
    """
    payload = f"{action}|{variant_id}|{created_at.isoformat()}"
    return f"aud.{action}.{ga4gh_digest(payload)}"


def build_review_audit_record(
    *,
    result: VariantReviewResult,
    audit_id: str | None = None,
    variant: NormalizedVariant,
    gene: str | None,
    ledger: EvidenceLedger,
    gene_resolution: dict[str, Any] | None = None,
    input_hashes: dict[str, str],
    configuration_hash: str,
    adapter_declarations: Sequence[dict[str, Any]],
    normalization_version: str,
    run_id: str | None = None,
    created_at: datetime | None = None,
    model_metadata: dict[str, Any] | None = None,
) -> AuditRecord:
    """Create the audit record for a completed review."""
    stamp = created_at or utc_now()
    return AuditRecord(
        audit_id=audit_id
        or new_audit_id(action="review", variant_id=variant.variant_id, created_at=stamp),
        action="review",
        created_at=stamp,
        run_id=run_id,
        variant_id=variant.variant_id,
        variant_identity=variant.identity,
        genome_build=variant.genome_build.value,
        gene=gene,
        transcript=result.variant.transcript,
        gene_resolution=gene_resolution,
        input_hashes=dict(input_hashes),
        configuration_hash=configuration_hash,
        engine_version=ENGINE_VERSION,
        normalization_version=normalization_version,
        rule_set=result.classification.rule_set,
        rule_set_version=result.classification.rule_set_version,
        database_versions=dict(result.provenance.database_versions),
        adapter_declarations=tuple(adapter_declarations),
        evidence_snapshot=tuple(
            json.loads(record.model_dump_json()) for record in ledger.for_variant(variant)
        ),
        result_snapshot=json.loads(result.model_dump_json()),
        model_metadata=model_metadata,
    )


def build_signoff_audit_record(
    *,
    parent: AuditRecord,
    review_result: VariantReviewResult,
    decision: ReviewDecision,
    created_at: datetime | None = None,
) -> AuditRecord:
    """Create the audit record for a human sign-off or override."""
    stamp = created_at or utc_now()
    before = str((parent.result_snapshot.get("classification") or {}).get("label", ""))
    after = decision.decision or before
    is_override = after != before
    action: AuditActionType = "override" if is_override else "sign_off"
    override_reason = decision.override_reason if is_override else None
    if is_override and not override_reason:
        raise AuditError(
            "An override (reviewer tier differs from the engine label) requires an explicit "
            "override_reason. Silent overrides are not recorded."
        )
    return AuditRecord(
        audit_id=new_audit_id(action=action, variant_id=parent.variant_id, created_at=stamp),
        action=action,
        created_at=stamp,
        run_id=parent.run_id,
        parent_audit_id=parent.audit_id,
        variant_id=parent.variant_id,
        variant_identity=parent.variant_identity,
        genome_build=parent.genome_build,
        gene=parent.gene,
        transcript=parent.transcript,
        gene_resolution=parent.gene_resolution,
        input_hashes=parent.input_hashes,
        configuration_hash=parent.configuration_hash,
        engine_version=parent.engine_version,
        normalization_version=parent.normalization_version,
        rule_set=parent.rule_set,
        rule_set_version=parent.rule_set_version,
        database_versions=parent.database_versions,
        adapter_declarations=parent.adapter_declarations,
        evidence_snapshot=parent.evidence_snapshot,
        result_snapshot=json.loads(review_result.model_dump_json()),
        reviewer=decision.reviewer,
        reviewer_role=decision.reviewer_role,
        reviewer_action=decision.action,
        decision=after,
        override_reason=override_reason,
        classification_before=before,
        classification_after=after,
    )


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


class ReplayResult(BaseModel):
    """Outcome of replaying an audit record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: str
    variant_id: str
    reproduced: bool
    original_label: str
    replayed_label: str
    original_decision_state: str
    replayed_decision_state: str
    divergence: dict[str, Any] = Field(default_factory=dict)
    evidence_record_count: int = 0
    rule_set: str = ""
    notes: tuple[str, ...] = ()
    replayed_result: dict[str, Any] = Field(default_factory=dict)
    replay_audit_id: str | None = None


def variant_from_audit(record: AuditRecord) -> NormalizedVariant:
    """Reconstruct the normalized variant stored in an audit record.

    Reconstruction is from the stored contract, not by re-normalizing the
    original VCF: the point of a replay is to reproduce the recorded decision,
    and the recorded decision was made about *this* identity.
    """
    snapshot = record.result_snapshot
    variant_block = snapshot.get("variant") or {}
    normalization_block = snapshot.get("normalization") or {}
    if not variant_block:
        raise AuditError(f"audit record {record.audit_id} has no variant block")
    try:
        report = NormalizationReport.model_validate(
            {
                key: value
                for key, value in normalization_block.items()
                if key in NormalizationReport.model_fields
            }
        )
    except ValidationError as exc:
        raise AuditError(
            f"audit record {record.audit_id} has an unreadable normalization block: {exc}") from exc
    return NormalizedVariant(
        genome_build=GenomeBuild(variant_block["genome_build"]),
        chromosome=variant_block["chromosome"],
        position=int(variant_block["position"]),
        reference=variant_block["reference"],
        alternate=variant_block["alternate"],
        variant_type=VariantType(variant_block["variant_type"]),
        accession=variant_block.get("accession"),
        spdi=variant_block.get("spdi"),
        hgvs_g=variant_block.get("hgvs_g"),
        identity=normalization_block.get("identity_string") or record.variant_identity,
        variant_id=variant_block.get("id") or record.variant_id,
        normalization=report,
    )


def _strip_non_deterministic(payload: Any) -> Any:
    """Recursively drop timestamp/identifier fields so a diff is meaningful."""
    if isinstance(payload, dict):
        return {
            key: _strip_non_deterministic(value)
            for key, value in payload.items()
            if key not in _NON_DETERMINISTIC_FIELDS
        }
    if isinstance(payload, list):
        return [_strip_non_deterministic(item) for item in payload]
    return payload


def _diff(original: Any, replayed: Any, path: str = "$") -> dict[str, Any]:
    differences: dict[str, Any] = {}
    if type(original) is not type(replayed):
        differences[path] = {"original": repr(original), "replayed": repr(replayed)}
        return differences
    if isinstance(original, dict):
        for key in sorted(set(original) | set(replayed)):
            differences.update(
                _diff(original.get(key), replayed.get(key), path=f"{path}.{key}")
            )
        return differences
    if isinstance(original, list):
        if len(original) != len(replayed):
            differences[f"{path}.length"] = {
                "original": len(original),
                "replayed": len(replayed),
            }
        for index, (left, right) in enumerate(zip(original, replayed, strict=False)):
            differences.update(_diff(left, right, path=f"{path}[{index}]"))
        return differences
    if original != replayed:
        differences[path] = {"original": original, "replayed": replayed}
    return differences


def replay_decision(
    record: AuditRecord,
    *,
    audit_log: AuditLog | None = None,
    derivation_config: DerivationConfig | None = None,
    created_at: datetime | None = None,
) -> ReplayResult:
    """Reproduce a recorded decision from its evidence snapshot.

    The engine is rebuilt with the *recorded* rule set, so a replay is a test
    of determinism rather than of today's configuration.

    Raises :class:`ReplayDivergenceError` if the reproduced classification
    differs from the recorded one. A divergence means the signed path is not
    deterministic and must be treated as a defect, not as a curiosity.
    """
    variant = variant_from_audit(record)
    evidence = import_ledger_snapshot(record.evidence_snapshot)
    ledger = MemoryLedger()
    ledger.append(evidence)

    engine = AcmgEngine(rule_set=record.rule_set, config=derivation_config or DerivationConfig())
    outcome = engine.evaluate(
        variant=variant,
        gene=record.gene,
        usable_evidence=ledger.usable_for_variant(variant),
        all_evidence=ledger.for_variant(variant),
        normalization_complete=variant.normalization.complete,
    )

    original_classification = record.result_snapshot.get("classification") or {}
    original_label = str(original_classification.get("label", ""))
    original_state = str(original_classification.get("decision_state", ""))

    replayed_contract = VariantReviewResult.build(
        result_id=f"replay-{record.audit_id}",
        variant=variant,
        gene=record.gene,
        gene_block=_gene_block_from_audit(record),
        outcome=outcome,
        evidence=tuple(ledger.informative_for_variant(variant)),
        evidence_gaps=tuple(ledger.gaps_for_variant(variant)),
        provenance=_provenance_from_audit(record),
    )
    replayed_payload = json.loads(replayed_contract.model_dump_json())

    divergence = _diff(
        _strip_non_deterministic(record.result_snapshot),
        _strip_non_deterministic(replayed_payload),
    )
    reproduced = not divergence and original_label == outcome.label

    notes: list[str] = []
    if record.evidence_snapshot and not evidence:
        notes.append(
            "The audit record contains an evidence snapshot but no record could be "
            "deserialized; the replay ran on an empty ledger."
        )
    if not record.evidence_snapshot:
        notes.append(
            "The audit record contains no evidence snapshot, so the replay could only "
            "reproduce the rule application, not the evidence retrieval."
        )
    if divergence:
        notes.append(
            "Replay diverged from the recorded result. This indicates the signed classification "
            "path is not deterministic and must be investigated before the result is relied on."
        )

    result = ReplayResult(
        audit_id=record.audit_id,
        variant_id=variant.variant_id,
        reproduced=reproduced,
        original_label=original_label,
        replayed_label=outcome.label,
        original_decision_state=original_state,
        replayed_decision_state=outcome.decision_state,
        divergence=divergence,
        evidence_record_count=len(evidence),
        rule_set=record.rule_set,
        notes=tuple(notes),
        replayed_result=replayed_payload,
    )

    if not reproduced:
        if audit_log is not None:
            stamp = created_at or utc_now()
            audit_log.append(
                AuditRecord(
                    audit_id=new_audit_id(
                        action="replay", variant_id=variant.variant_id, created_at=stamp
                    ),
                    action="replay",
                    created_at=stamp,
                    run_id=record.run_id,
                    parent_audit_id=record.audit_id,
                    variant_id=variant.variant_id,
                    variant_identity=variant.identity,
                    genome_build=variant.genome_build.value,
                    gene=record.gene,
                    rule_set=record.rule_set,
                    rule_set_version=record.rule_set_version,
                    replay_of=record.audit_id,
                    replay_reproduced=False,
                    replay_divergence=divergence,
                    result_snapshot=replayed_payload,
                    evidence_snapshot=record.evidence_snapshot,
                )
            )
        raise ReplayDivergenceError(
            f"Replay of audit record {record.audit_id} did not reproduce the recorded result. "
            f"Original: {original_label}/{original_state}; replayed: "
            f"{outcome.label}/{outcome.decision_state}. Divergence: "
            f"{json.dumps(divergence, sort_keys=True)[:2000]}"
        )

    if audit_log is not None:
        stamp = created_at or utc_now()
        replay_audit_id = new_audit_id(
            action="replay", variant_id=variant.variant_id, created_at=stamp
        )
        audit_log.append(
            AuditRecord(
                audit_id=replay_audit_id,
                action="replay",
                created_at=stamp,
                run_id=record.run_id,
                parent_audit_id=record.audit_id,
                variant_id=variant.variant_id,
                variant_identity=variant.identity,
                genome_build=variant.genome_build.value,
                gene=record.gene,
                rule_set=record.rule_set,
                rule_set_version=record.rule_set_version,
                replay_of=record.audit_id,
                replay_reproduced=True,
                replay_divergence={},
                evidence_snapshot=record.evidence_snapshot,
            )
        )
        result = result.model_copy(update={"replay_audit_id": replay_audit_id})

    return result


def _gene_block_from_audit(record: AuditRecord) -> Any:
    """Reconstruct the gene block recorded with the original decision."""
    from ngs_agent.core.contract import GeneBlock

    if not record.gene_resolution:
        return GeneBlock(symbol=record.gene)
    allowed = {
        key: value
        for key, value in record.gene_resolution.items()
        if key in GeneBlock.model_fields
    }
    try:
        return GeneBlock.model_validate(allowed)
    except ValidationError:
        # A gene block we cannot read must not silently become a different one.
        return GeneBlock(symbol=record.gene, note="stored gene_resolution could not be parsed")


def _provenance_from_audit(record: AuditRecord) -> Any:
    from ngs_agent.core.contract import ProvenanceBlock

    snapshot = record.result_snapshot.get("provenance") or {}
    allowed = {
        key: value
        for key, value in snapshot.items()
        if key in ProvenanceBlock.model_fields and key not in _NON_DETERMINISTIC_FIELDS
    }
    allowed.setdefault("engine_version", record.engine_version or ENGINE_VERSION)
    allowed.setdefault("normalization_version", record.normalization_version)
    allowed.setdefault(
        "contract_schema_version", record.result_snapshot.get("schema_version", "1.0.0"))
    allowed.setdefault("rule_set", record.rule_set)
    allowed.setdefault("rule_set_version", record.rule_set_version)
    allowed.setdefault("generated_at", record.created_at.isoformat())
    allowed["audit_id"] = record.audit_id
    return ProvenanceBlock.model_validate(allowed)


def record_sign_off(
    *,
    audit_log: AuditLog,
    result: VariantReviewResult,
    decision: ReviewDecision,
    created_at: datetime | None = None,
) -> tuple[VariantReviewResult, AuditRecord]:
    """Apply a human decision, returning the signed contract and its audit record."""
    parent = audit_log.latest_for_variant(result.variant.id)
    if parent is None:
        raise AuditError(
            f"No prior review audit record for variant {result.variant.id}. A sign-off must "
            "attach to a review; NGS-Agent does not create free-standing approvals."
        )
    signed = sign_off(result, decision)
    record = build_signoff_audit_record(
        parent=parent,
        review_result=signed,
        decision=decision,
        created_at=created_at,
    )
    audit_log.append(record)
    return signed, record


def iter_audit_records(payloads: Iterable[dict[str, Any]]) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    for index, payload in enumerate(payloads):
        try:
            records.append(AuditRecord.model_validate(payload))
        except (ValidationError, ContractError) as exc:
            raise AuditError(f"audit payload {index} failed validation: {exc}") from exc
    return records


def configuration_hash(configuration: dict[str, Any]) -> str:
    """Stable hash of the engine configuration in force for a run."""
    return canonical_json_sha256(configuration)
