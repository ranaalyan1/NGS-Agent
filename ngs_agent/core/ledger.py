"""The evidence ledger: append-only storage for everything the engine may use.

The ledger is the boundary the mission calls "non-negotiable". Nothing reaches
the ACMG engine except through it, and nothing enters it except a validated
:class:`~ngs_agent.core.evidence.models.EvidenceRecord`. Two properties make
that enforceable rather than aspirational:

* **Append-only.** Records are never edited or deleted. A correction is a new
  record. The on-disk form is JSON Lines, so an entry can be added without
  rewriting — and without the possibility of rewriting — the file.
* **Self-describing.** Each entry stores its source version, retrieval time,
  genome build, verification status, and limitations. A reader of the ledger
  alone can tell whether a classification was justified, without running any
  code.

Storage backends: :class:`MemoryLedger` for tests and single reviews,
:class:`JsonlLedger` for durable runs. Both implement :class:`EvidenceLedger`.
Neither performs network I/O.
"""

from __future__ import annotations

import abc
import json
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from ngs_agent.core.errors import AuditError, EvidenceValidationError
from ngs_agent.core.evidence.models import EvidenceRecord, EvidenceStatus
from ngs_agent.core.normalization import NormalizedVariant

LEDGER_SCHEMA = "v1"


class EvidenceLedger(abc.ABC):
    """Interface the pipeline and the audit layer program against."""

    @abc.abstractmethod
    def append(self, records: Iterable[EvidenceRecord]) -> int:
        """Append records, returning how many were newly added."""

    @abc.abstractmethod
    def for_variant(self, variant: NormalizedVariant) -> list[EvidenceRecord]:
        """Every record in the ledger about ``variant``, in append order."""

    @abc.abstractmethod
    def all_records(self) -> list[EvidenceRecord]:
        """Every record in the ledger."""

    # -- shared behaviour ---------------------------------------------------

    def usable_for_variant(self, variant: NormalizedVariant) -> list[EvidenceRecord]:
        """Records that may actually support or refute a criterion.

        This is the only accessor the ACMG derivation layer is permitted to
        use. Filtering here (rather than inside the engine) means the engine
        cannot accidentally consume an unverified record: it never sees one.
        """
        return [record for record in self.for_variant(variant) if record.usable_as_evidence]

    def informative_for_variant(self, variant: NormalizedVariant) -> list[EvidenceRecord]:
        """Present records, including those that failed verification.

        Used for reporting conflicts and for the ``evidence`` block of the
        contract, which shows the reviewer everything retrieved — not just what
        the engine was allowed to use.
        """
        return [record for record in self.for_variant(variant) if record.informative]

    def gaps_for_variant(self, variant: NormalizedVariant) -> list[EvidenceRecord]:
        """Records that document a gap: unavailable, failed, invalid, or unconfigured."""
        return [
            record
            for record in self.for_variant(variant)
            if record.status is not EvidenceStatus.PRESENT
        ]

    def source_versions(self) -> dict[str, str]:
        """Distinct ``name -> version`` pairs seen in the ledger."""
        versions: dict[str, set[str]] = {}
        for record in self.all_records():
            versions.setdefault(record.source.name, set()).add(record.source.version)
        return {name: "|".join(sorted(values)) for name, values in sorted(versions.items())}


class MemoryLedger(EvidenceLedger):
    """An in-process ledger. Append-only by construction."""

    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []
        self._ids: set[str] = set()

    def append(self, records: Iterable[EvidenceRecord]) -> int:
        added = 0
        for record in records:
            if record.evidence_id in self._ids:
                continue
            self._ids.add(record.evidence_id)
            self._records.append(record)
            added += 1
        return added

    def for_variant(self, variant: NormalizedVariant) -> list[EvidenceRecord]:
        return [
            record
            for record in self._records
            if record.queried_variant_identity == variant.identity
        ]

    def all_records(self) -> list[EvidenceRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[EvidenceRecord]:
        return iter(self._records)


class JsonlLedger(EvidenceLedger):
    """A durable, append-only JSON Lines ledger.

    Each line is one serialized record. Writing never rewrites existing lines,
    which means a ledger file is an audit artefact in its own right: it can be
    hashed, shipped, and inspected without NGS-Agent.
    """

    def __init__(self, path: Path | str, *, create: bool = True) -> None:
        self.path = Path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: list[EvidenceRecord] | None = None

    def append(self, records: Iterable[EvidenceRecord]) -> int:
        existing = {record.evidence_id for record in self._load()}
        added = 0
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                if record.evidence_id in existing:
                    continue
                handle.write(record.model_dump_json() + "\n")
                existing.add(record.evidence_id)
                added += 1
        self._cache = None
        return added

    def _load(self) -> list[EvidenceRecord]:
        if self._cache is not None:
            return self._cache
        records: list[EvidenceRecord] = []
        if self.path.is_file():
            with self.path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(EvidenceRecord.model_validate_json(line))
                    except (ValidationError, EvidenceValidationError) as exc:
                        raise AuditError(
                            f"{self.path}:{line_number}: ledger entry failed schema validation: {exc}"
                        ) from exc
        self._cache = records
        return records

    def for_variant(self, variant: NormalizedVariant) -> list[EvidenceRecord]:
        return [
            record
            for record in self._load()
            if record.queried_variant_identity == variant.identity
        ]

    def all_records(self) -> list[EvidenceRecord]:
        return list(self._load())

    def compact(self) -> int:
        """Rewrite the ledger with duplicates removed. Returns records kept.

        Compaction creates a *new* file and renames it, so a partially written
        ledger can never replace a good one.
        """
        records = self._load()
        seen: set[str] = set()
        unique: list[EvidenceRecord] = []
        for record in records:
            if record.evidence_id in seen:
                continue
            seen.add(record.evidence_id)
            unique.append(record)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for record in unique:
                handle.write(record.model_dump_json() + "\n")
        temporary.replace(self.path)
        self._cache = unique
        return len(unique)


def export_ledger_snapshot(
    ledger: EvidenceLedger, variant: NormalizedVariant
) -> Sequence[dict[str, object]]:
    """Serialize every record about ``variant`` for embedding in an audit record.

    The snapshot is what :command:`ngsagent replay` consumes. It embeds the
    full record — not a pointer to a source that may have changed — because a
    replay that has to re-fetch is not a replay.
    """
    return [json.loads(record.model_dump_json()) for record in ledger.for_variant(variant)]


def import_ledger_snapshot(
    payload: Iterable[dict[str, object]], *, retrieved_at_default: datetime | None = None
) -> list[EvidenceRecord]:
    """Rebuild records from an audit snapshot.

    ``retrieved_at_default`` is unused for well-formed snapshots (the timestamp
    travels with each record) and exists only to make the failure mode explicit
    if a snapshot is missing it.
    """
    records: list[EvidenceRecord] = []
    for index, entry in enumerate(payload):
        try:
            records.append(EvidenceRecord.model_validate(entry))
        except (ValidationError, EvidenceValidationError) as exc:
            message = f"snapshot entry {index} failed schema validation: {exc}"
            if retrieved_at_default is not None:
                message += f" (default retrieved_at {retrieved_at_default.isoformat()} was offered but not applied)"
            raise AuditError(message) from exc
    return records
