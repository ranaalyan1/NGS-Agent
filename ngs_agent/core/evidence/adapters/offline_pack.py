"""Offline evidence-pack adapter.

An evidence pack is a directory or JSON file of pre-retrieved, schema-validated
:class:`~ngs_agent.core.evidence.models.EvidenceRecord` objects. It exists for
three deployment realities:

1. **Air-gapped operation.** A hospital network with no egress to NCBI can ship
   a pack built inside a connected enclave, and the review still runs.
2. **Deterministic tests.** Golden cases must not depend on what ClinVar
   returns today.
3. **Replay.** Reconstructing a past decision requires the exact evidence that
   was available then (see :class:`SnapshotEvidenceAdapter`).

A pack is a *derived* artefact, never a primary source. Each record inside it
keeps its original ``source``, ``retrieved_at``, and retrieval digest, so the
pack cannot launder provenance: reading a ClinVar record from disk still says
"this came from ClinVar at this time, with this response hash".

Security note: a pack is untrusted input. Records are validated against the
evidence schema (``extra="forbid"``) and their ``evidence_id`` digest is
recomputed and compared, so a tampered record is rejected rather than believed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ngs_agent.core.errors import EvidenceValidationError
from ngs_agent.core.evidence.base import AdapterDeclaration, BaseEvidenceAdapter
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    EvidenceStatus,
    RetrievalDetail,
    VerificationStatus,
)
from ngs_agent.core.hashing import sha256_file
from ngs_agent.core.normalization import NormalizedVariant

ADAPTER_VERSION = "offline-pack-adapter-1.0.0"


class EvidencePackError(EvidenceValidationError):
    """A pack file is malformed, tampered with, or internally inconsistent."""


class OfflineEvidencePackAdapter(BaseEvidenceAdapter):
    """Serve evidence records from a local pack file or directory."""

    def __init__(
        self,
        path: Path | str,
        *,
        pack_name: str = "offline_evidence_pack",
        clock: Any = None,
        verify_digests: bool = True,
    ) -> None:
        super().__init__(clock=clock)
        self.path = Path(path)
        self._pack_name = pack_name
        self._verify_digests = verify_digests
        self._records: list[EvidenceRecord] | None = None
        self._pack_version = "unversioned"
        self._pack_sha256: str | None = None

    @property
    def declaration(self) -> AdapterDeclaration:
        self._ensure_loaded()
        return AdapterDeclaration(
            name=self._pack_name,
            version=self._pack_version,
            adapter_version=ADAPTER_VERSION,
            data_types=tuple(sorted({record.data_type for record in self._records or []}, key=lambda item: item.value))
            or (EvidenceDataType.CLINICAL_SIGNIFICANCE,),
            endpoint=str(self.path),
            license="declared by the pack producer",
            requires_network=False,
            hosted_by="local",
            notes=(
                "Pre-retrieved evidence records served from local storage. Original source, "
                "retrieval timestamp, and response digests are preserved."
            ),
        )

    # -- loading ------------------------------------------------------------

    def _ensure_loaded(self) -> list[EvidenceRecord]:
        if self._records is None:
            self._records = self._load()
        return self._records

    def _load(self) -> list[EvidenceRecord]:
        if self.path.is_dir():
            files = sorted(self.path.glob("*.json"))
            if not files:
                raise EvidencePackError(f"evidence pack directory contains no .json files: {self.path}")
            records: list[EvidenceRecord] = []
            for file_path in files:
                records.extend(self._load_file(file_path))
            self._pack_version = f"dir:{len(files)}-files"
            return records
        if not self.path.is_file():
            raise EvidencePackError(f"evidence pack not found: {self.path}")
        return self._load_file(self.path)

    def _load_file(self, file_path: Path) -> list[EvidenceRecord]:
        self._pack_sha256 = sha256_file(file_path)
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EvidencePackError(f"{file_path}: {exc}") from exc

        self._pack_version = str(payload.get("pack_version", "unversioned"))
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise EvidencePackError(f"{file_path}: expected a 'records' list")

        records: list[EvidenceRecord] = []
        for index, raw in enumerate(raw_records):
            if not isinstance(raw, dict):
                raise EvidencePackError(f"{file_path}: record {index} is not an object")
            declared_id = raw.get("evidence_id")
            try:
                if self._verify_digests and declared_id:
                    # Recompute the content digest from scratch. Validating
                    # ``raw`` as-is copies the declared id into the record and
                    # then compares it with itself, which detects nothing.
                    recomputed = EvidenceRecord.model_validate({**raw, "evidence_id": ""})
                    if recomputed.evidence_id != declared_id:
                        raise EvidencePackError(
                            f"{file_path}: record {index} declares evidence_id {declared_id!r} "
                            f"but its content hashes to {recomputed.evidence_id!r}. The pack has "
                            "been modified after the digest was computed, or was hand-edited. "
                            "Refusing to serve it."
                        )
                    record = recomputed
                else:
                    record = EvidenceRecord.model_validate(raw)
            except EvidencePackError:
                raise
            except (ValidationError, EvidenceValidationError) as exc:
                raise EvidencePackError(f"{file_path}: record {index} failed schema validation: {exc}") from exc
            records.append(record)
        return records

    # -- retrieval ----------------------------------------------------------

    def _retrieve(
        self, variant: NormalizedVariant, *, gene: str | None
    ) -> Sequence[EvidenceRecord]:
        from ngs_agent.core.evidence.models import utc_now

        records = self._ensure_loaded()
        retrieved_at = self._clock() if self._clock is not None else utc_now()
        matched = [
            record
            for record in records
            if record.queried_variant_identity == variant.identity
            and record.genome_build is variant.genome_build
        ]
        if not matched:
            return [
                self._record_unavailable(
                    variant=variant,
                    gene=gene,
                    data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
                    retrieved_at=retrieved_at,
                    retrieval=RetrievalDetail(
                        transport="file",
                        urls=(str(self.path),),
                        response_sha256=self._pack_sha256,
                    ),
                    reason=(
                        f"Evidence pack {self._pack_name} ({self._pack_version}) contains no record "
                        f"for {variant.identity}. A pack is a snapshot of specific variants, not a "
                        "database; absence here means only that the pack does not cover this variant."
                    ),
                )
            ]
        return matched


class SnapshotEvidenceAdapter(BaseEvidenceAdapter):
    """Replay adapter: serves the exact records captured in an audit record.

    This is what makes :command:`ngsagent replay` meaningful. Re-running the
    engine against today's ClinVar answers a different question ("what would we
    conclude now?") than re-running it against the recorded snapshot ("why did
    we conclude what we concluded then?"). Both are useful; only the second one
    is an audit.
    """

    def __init__(
        self,
        records: Sequence[EvidenceRecord],
        *,
        snapshot_id: str,
        original_source_versions: dict[str, str] | None = None,
        clock: Any = None,
    ) -> None:
        super().__init__(clock=clock)
        self._records = list(records)
        self._snapshot_id = snapshot_id
        self._original_source_versions = dict(original_source_versions or {})

    @property
    def declaration(self) -> AdapterDeclaration:
        return AdapterDeclaration(
            name="audit_snapshot",
            version=self._snapshot_id,
            adapter_version=ADAPTER_VERSION,
            data_types=tuple(sorted({record.data_type for record in self._records}, key=lambda item: item.value))
            or (EvidenceDataType.CLINICAL_SIGNIFICANCE,),
            requires_network=False,
            hosted_by="local",
            notes=(
                "Replays evidence captured in an audit record. Original source versions: "
                f"{self._original_source_versions or 'unknown'}."
            ),
        )

    def _retrieve(
        self, variant: NormalizedVariant, *, gene: str | None
    ) -> Sequence[EvidenceRecord]:
        from ngs_agent.core.evidence.models import utc_now

        retrieved_at = self._clock() if self._clock is not None else utc_now()
        matched = [record for record in self._records if record.queried_variant_identity == variant.identity]
        if not matched:
            return [
                EvidenceRecord(
                    source=self.declaration.as_source(),
                    data_type=EvidenceDataType.CLINICAL_SIGNIFICANCE,
                    status=EvidenceStatus.UNAVAILABLE,
                    retrieved_at=retrieved_at,
                    genome_build=variant.genome_build,
                    queried_variant_identity=variant.identity,
                    gene=gene,
                    applicability=ApplicabilityStatus.INDETERMINATE,
                    verification=VerificationStatus.NOT_VERIFIED,
                    limitations=(
                        f"Snapshot {self._snapshot_id} contains no record for {variant.identity}; "
                        "the audit record and the queried variant do not correspond.",
                    ),
                    retrieval=RetrievalDetail(transport="snapshot"),
                    provenance={"snapshot_id": self._snapshot_id},
                )
            ]
        return matched
