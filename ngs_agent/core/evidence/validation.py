"""Evidence validation — the gate between retrieval and the ledger.

Adapters are written by humans and talk to services that change without notice.
Validation is a second, independent opinion: it re-checks the structural
invariants that matter for safety *after* the adapter has built its records,
so a buggy or malicious adapter cannot push unusable evidence into the engine.

Checks performed:

``build_mismatch``
    A record whose ``genome_build`` disagrees with the variant it claims to
    describe is invalidated. Mixing assemblies is the single easiest way to
    attach the wrong evidence to a variant.

``identity_mismatch``
    A present record whose ``observed_variant_identity`` differs from the
    ``queried_variant_identity`` is downgraded to
    ``identity_unverified``/``indeterminate`` rather than used.

``negative_evidence_smuggling``
    A non-present record that somehow carries an observed value or strength
    hint is invalidated. This is the machine-checkable form of the rule
    "never silently convert missing data into negative evidence".

``duplicate``
    Two records with the same ``evidence_id`` are collapsed to one, with the
    duplicate's retrieval time recorded. The same observation fetched twice is
    one observation.

``source_version_missing``
    A record without a source version cannot be reproduced and is invalidated.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, ConfigDict

from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceRecord,
    EvidenceStatus,
    VerificationStatus,
)
from ngs_agent.core.normalization import NormalizedVariant


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ValidationFinding(BaseModel):
    """One problem (or potential problem) found in a batch of evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    severity: ValidationSeverity
    message: str
    evidence_id: str | None = None
    source: str | None = None


@dataclass
class ValidationReport:
    """Findings plus the record set that survived them."""

    accepted: list[EvidenceRecord] = field(default_factory=list)
    rejected: list[tuple[EvidenceRecord, str]] = field(default_factory=list)
    findings: list[ValidationFinding] = field(default_factory=list)
    duplicate_retrievals: dict[str, list[str]] = field(default_factory=dict)

    @property
    def errors(self) -> list[ValidationFinding]:
        return [finding for finding in self.findings if finding.severity is ValidationSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationFinding]:
        return [finding for finding in self.findings if finding.severity is ValidationSeverity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_evidence(
    records: Iterable[EvidenceRecord], variant: NormalizedVariant
) -> ValidationReport:
    """Validate a batch of records retrieved for ``variant``."""
    report = ValidationReport()
    seen: dict[str, EvidenceRecord] = {}

    for record in records:
        rejection = _validate_one(record, variant, report)
        if rejection is not None:
            report.rejected.append((record, rejection))
            continue
        if record.evidence_id in seen:
            report.duplicate_retrievals.setdefault(record.evidence_id, []).append(
                record.retrieved_at.isoformat()
            )
            report.findings.append(
                ValidationFinding(
                    code="duplicate",
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"{record.source.name}: duplicate evidence_id {record.evidence_id}; "
                        "collapsed to a single observation."
                    ),
                    evidence_id=record.evidence_id,
                    source=record.source.name,
                )
            )
            continue
        seen[record.evidence_id] = record
        report.accepted.append(record)

    return report


def _validate_one(
    record: EvidenceRecord, variant: NormalizedVariant, report: ValidationReport
) -> str | None:
    """Return a rejection reason, or ``None`` if the record is acceptable.

    Where a problem can be *corrected* safely (an over-confident applicability
    claim) the record is downgraded in place rather than rejected, and a
    warning is recorded. Rejecting would hide the fact that the source did
    answer; downgrading keeps the answer while removing the trust.
    """
    source = record.source.name

    if not record.source.version or record.source.version.strip() in {"", "unknown"}:
        report.findings.append(
            ValidationFinding(
                code="source_version_missing",
                severity=ValidationSeverity.ERROR,
                message=(
                    f"{source}: evidence source version is missing or 'unknown', so this record "
                    "cannot be reproduced. Rejected."
                ),
                evidence_id=record.evidence_id,
                source=source,
            )
        )
        return "source version missing"

    if record.genome_build is not variant.genome_build:
        report.findings.append(
            ValidationFinding(
                code="build_mismatch",
                severity=ValidationSeverity.ERROR,
                message=(
                    f"{source}: record is stated for {record.genome_build.value} but the variant "
                    f"under review is {variant.genome_build.value}. Evidence from a different "
                    "assembly is never attached to a variant. Rejected."
                ),
                evidence_id=record.evidence_id,
                source=source,
            )
        )
        return "genome build mismatch"

    if record.status in {
        EvidenceStatus.UNAVAILABLE,
        EvidenceStatus.RETRIEVAL_FAILED,
        EvidenceStatus.INVALID,
        EvidenceStatus.NOT_CONFIGURED,
    }:
        if record.observed_value or record.strength_hint:
            report.findings.append(
                ValidationFinding(
                    code="negative_evidence_smuggling",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"{source}: a record with status={record.status.value} carries an observed "
                        "value or strength hint. Missing data must never be encoded as a "
                        "observation. Rejected."
                    ),
                    evidence_id=record.evidence_id,
                    source=source,
                )
            )
            return "non-informative record carries a value"
        return None

    # status == present from here on.
    if record.queried_variant_identity != variant.identity:
        report.findings.append(
            ValidationFinding(
                code="query_mismatch",
                severity=ValidationSeverity.ERROR,
                message=(
                    f"{source}: record was queried for {record.queried_variant_identity!r} but is "
                    f"being attached to {variant.identity!r}. Rejected."
                ),
                evidence_id=record.evidence_id,
                source=source,
            )
        )
        return "queried variant mismatch"

    if (
        record.observed_variant_identity
        and record.observed_variant_identity != record.queried_variant_identity
    ):
        report.findings.append(
            ValidationFinding(
                code="identity_mismatch",
                severity=ValidationSeverity.WARNING,
                message=(
                    f"{source}: the source describes {record.observed_variant_identity!r} while "
                    f"{record.queried_variant_identity!r} was requested. Trust downgraded to "
                    "identity_unverified; the engine will not use this record for a criterion."
                ),
                evidence_id=record.evidence_id,
                source=source,
            )
        )
        if (
            record.verification is VerificationStatus.VERIFIED
            or record.applicability is ApplicabilityStatus.APPLIES
        ):
            _downgrade(record, source, report)

    if (
        record.applicability is ApplicabilityStatus.APPLIES
        and record.verification is not VerificationStatus.VERIFIED
    ):
        report.findings.append(
            ValidationFinding(
                code="unverified_applicability",
                severity=ValidationSeverity.WARNING,
                message=(
                    f"{source}: applicability=applies without verification=verified. "
                    "Downgraded to indeterminate."
                ),
                evidence_id=record.evidence_id,
                source=source,
            )
        )
        _downgrade(record, source, report)

    if not record.retrieval.urls and record.retrieval.transport == "http":
        report.findings.append(
            ValidationFinding(
                code="missing_retrieval_url",
                severity=ValidationSeverity.WARNING,
                message=(
                    f"{source}: an HTTP retrieval recorded no URL, weakening the audit trail."
                ),
                evidence_id=record.evidence_id,
                source=source,
            )
        )

    return None


def _downgrade(record: EvidenceRecord, source: str, report: ValidationReport) -> None:
    """Mutate a frozen record's trust fields in place, recording the change.

    Frozen models are the right default, but validation is the one place where
    an already-constructed record must be corrected — and the correction has to
    be visible, not silent.
    """
    object.__setattr__(record, "verification", VerificationStatus.IDENTITY_UNVERIFIED)
    object.__setattr__(record, "applicability", ApplicabilityStatus.INDETERMINATE)
    existing = tuple(record.limitations)
    note = (
        "Trust downgraded by NGS-Agent evidence validation: the record's stated identity could "
        "not be confirmed against the query. It will not support an ACMG criterion."
    )
    if note not in existing:
        object.__setattr__(record, "limitations", (*existing, note))


def summarize_gaps(records: Sequence[EvidenceRecord]) -> list[str]:
    """Human-readable list of why each non-usable record is not usable.

    This is what surfaces in the contract's ``missing_evidence`` block. Every
    gap gets a sentence; none is allowed to vanish.
    """
    return [record.describe_gap() for record in records if not record.usable_as_evidence]
