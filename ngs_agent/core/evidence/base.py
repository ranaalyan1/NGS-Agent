"""The evidence-adapter contract.

Every evidence source in NGS-Agent — ClinVar today, gnomAD/ClinGen/VEP/SpliceAI
tomorrow, a customer's internal VCEP curation database eventually — is reached
through this one protocol. That uniformity is what makes the accountability
layer work: an adapter cannot be a black box, because the protocol requires it
to declare its identity, its failure modes, and the shape of what it returns.

Rules every adapter must follow (enforced where possible, documented where not):

1. Declare ``source_name``, ``source_version``, and ``adapter_version``.
2. Return :class:`~ngs_agent.core.evidence.models.EvidenceRecord` objects —
   never free-form text, never a classification.
3. Record the retrieval time and, for HTTP sources, the response digest.
4. Record failures. ``status=retrieval_failed`` and ``status=unavailable`` are
   different answers and must not be conflated.
5. Never convert missing data into negative evidence. If the source did not
   answer, the record carries no ``observed_value``.
6. Be independently testable: the constructor takes a transport so a test can
   substitute a recorded response without touching the network.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    EvidenceSource,
    EvidenceStatus,
    RetrievalDetail,
    VerificationStatus,
)
from ngs_agent.core.normalization import NormalizedVariant


class AdapterCapabilityError(RuntimeError):
    """Raised when an adapter is asked for a data type it cannot produce."""


@dataclass(frozen=True)
class AdapterDeclaration:
    """Static self-description published by every adapter.

    Surfaced in the JSON contract's ``provenance.adapters`` block so a reader
    can see which sources were *available* to a run, not just which ones
    returned something.
    """

    name: str
    version: str
    adapter_version: str
    data_types: tuple[EvidenceDataType, ...]
    endpoint: str | None = None
    license: str | None = None
    requires_network: bool = True
    hosted_by: str = "vendor"
    notes: str = ""

    def as_source(self) -> EvidenceSource:
        return EvidenceSource(
            name=self.name,
            version=self.version,
            adapter_version=self.adapter_version,
            endpoint=self.endpoint,
            license=self.license,
            hosted_by=self.hosted_by,  # type: ignore[arg-type]
        )


@runtime_checkable
class EvidenceAdapter(Protocol):
    """The interface the ledger, registry, and pipeline program against."""

    @property
    def declaration(self) -> AdapterDeclaration:
        """Static metadata about the source and this adapter's version."""

    def fetch(
        self,
        variant: NormalizedVariant,
        *,
        gene: str | None = None,
    ) -> Sequence[EvidenceRecord]:
        """Retrieve every record this adapter can produce for ``variant``.

        Implementations must return at least one record even when nothing was
        found: a zero-length result is indistinguishable from "the adapter was
        never called", which is exactly the ambiguity the ledger exists to
        remove.
        """


class BaseEvidenceAdapter(abc.ABC):
    """Convenience base implementing the record-construction boilerplate.

    Subclasses implement :meth:`_retrieve` and receive ready-made helpers for
    the three states that matter: present, unavailable, and failed. Centralizing
    them here is deliberate — an adapter author should not have to remember the
    invariant that a failed retrieval carries no ``observed_value``, because
    getting that wrong is a patient-safety bug.
    """

    def __init__(self, *, clock: Any = None) -> None:
        self._clock = clock

    @property
    @abc.abstractmethod
    def declaration(self) -> AdapterDeclaration:
        """Static self-description. See :class:`AdapterDeclaration`."""

    @abc.abstractmethod
    def _retrieve(
        self, variant: NormalizedVariant, *, gene: str | None
    ) -> Sequence[EvidenceRecord]:
        """Do the actual retrieval and build records via the ``_record_*`` helpers."""

    def fetch(
        self, variant: NormalizedVariant, *, gene: str | None = None
    ) -> Sequence[EvidenceRecord]:
        from ngs_agent.core.evidence.models import utc_now  # local import: patchable clock

        now = self._clock() if self._clock is not None else utc_now()
        try:
            records = self._retrieve(variant, gene=gene)
        except Exception as exc:  # noqa: BLE001 - adapter failures must never escape as crashes
            # A bug in an adapter is a retrieval failure, not a reason to abort
            # the whole review: the engine will abstain on missing evidence.
            # One failed record per declared data type keeps the "missing
            # evidence" report in the contract accurate.
            return [
                self._record_failed(
                    variant=variant,
                    gene=gene,
                    data_type=data_type,
                    retrieved_at=now,
                    error=f"{type(exc).__name__}: {exc}",
                )
                for data_type in self.declaration.data_types
            ]
        if not records:
            raise AdapterCapabilityError(
                f"{self.declaration.name} returned no records for {variant.identity}. Adapters "
                f"must "
                "return an explicit unavailable/failed record so that 'nothing found' is "
                "distinguishable from 'never asked'."
            )
        return records

    # -- record constructors ------------------------------------------------

    def _record_present(
        self,
        *,
        variant: NormalizedVariant,
        gene: str | None,
        data_type: EvidenceDataType,
        observed_value: dict[str, Any],
        retrieved_at: Any,
        verification: VerificationStatus,
        applicability: ApplicabilityStatus | None = None,
        transcript: str | None = None,
        accession: str | None = None,
        limitations: Sequence[str] = (),
        retrieval: RetrievalDetail | None = None,
        provenance: dict[str, Any] | None = None,
        observed_variant_identity: str | None = None,
    ) -> EvidenceRecord:
        if applicability is None:
            applicability = (
                ApplicabilityStatus.APPLIES
                if verification is VerificationStatus.VERIFIED
                else ApplicabilityStatus.INDETERMINATE
            )
        return EvidenceRecord(
            source=self.declaration.as_source(),
            data_type=data_type,
            status=EvidenceStatus.PRESENT,
            retrieved_at=retrieved_at,
            genome_build=variant.genome_build,
            queried_variant_identity=variant.identity,
            observed_variant_identity=observed_variant_identity or variant.identity,
            transcript=transcript,
            accession=accession,
            gene=gene,
            observed_value=observed_value,
            applicability=applicability,
            verification=verification,
            limitations=tuple(limitations),
            retrieval=retrieval or RetrievalDetail(),
            provenance=provenance or {},
        )

    def _record_unavailable(
        self,
        *,
        variant: NormalizedVariant,
        gene: str | None,
        data_type: EvidenceDataType,
        retrieved_at: Any,
        retrieval: RetrievalDetail | None = None,
        limitations: Sequence[str] = (),
        reason: str = "",
    ) -> EvidenceRecord:
        return EvidenceRecord(
            source=self.declaration.as_source(),
            data_type=data_type,
            status=EvidenceStatus.UNAVAILABLE,
            retrieved_at=retrieved_at,
            genome_build=variant.genome_build,
            queried_variant_identity=variant.identity,
            gene=gene,
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.NOT_VERIFIED,
            limitations=tuple([*limitations, reason] if reason else limitations),
            retrieval=retrieval or RetrievalDetail(transport="none"),
            provenance={"gap_reason": reason or "source reported no record for this variant"},
        )

    def _record_failed(
        self,
        *,
        variant: NormalizedVariant,
        gene: str | None,
        data_type: EvidenceDataType,
        retrieved_at: Any,
        error: str,
        retrieval: RetrievalDetail | None = None,
    ) -> EvidenceRecord:
        return EvidenceRecord(
            source=self.declaration.as_source(),
            data_type=data_type,
            status=EvidenceStatus.RETRIEVAL_FAILED,
            retrieved_at=retrieved_at,
            genome_build=variant.genome_build,
            queried_variant_identity=variant.identity,
            gene=gene,
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.NOT_VERIFIED,
            limitations=(
                "Retrieval failed. This is NOT evidence of absence: the source did not answer.",
            ),
            retrieval=retrieval or RetrievalDetail(transport="none", error=error),
            provenance={"gap_reason": f"retrieval failed: {error}"},
        )

    def _record_not_configured(
        self,
        *,
        variant: NormalizedVariant,
        gene: str | None,
        data_type: EvidenceDataType,
        retrieved_at: Any,
        reason: str,
    ) -> EvidenceRecord:
        return EvidenceRecord(
            source=self.declaration.as_source(),
            data_type=data_type,
            status=EvidenceStatus.NOT_CONFIGURED,
            retrieved_at=retrieved_at,
            genome_build=variant.genome_build,
            queried_variant_identity=variant.identity,
            gene=gene,
            applicability=ApplicabilityStatus.INDETERMINATE,
            verification=VerificationStatus.NOT_VERIFIED,
            limitations=(reason,),
            retrieval=RetrievalDetail(transport="none"),
            provenance={"gap_reason": reason},
        )
