"""Content-addressed evidence cache.

Caching evidence is not an optimization here; it is a reproducibility
mechanism. A classification must be reproducible against *the data that was
actually retrieved*, and public databases change daily. The cache stores the
full serialized :class:`EvidenceRecord` — including its retrieval timestamp,
source version, and response digest — so a re-run can either reuse the exact
bytes (``--offline``/air-gapped) or detect that the source has moved.

Layout::

    <cache_root>/
        v1/<key>.json          one serialized EvidenceRecord
        index.jsonl            append-only (key, evidence_id, retrieved_at, source_version)

Keys are ``sha256(adapter_name|adapter_version|variant_identity|data_type)``,
so an adapter upgrade never silently reuses a stale record produced by the
previous version of its parsing code.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ngs_agent.core.errors import EvidenceValidationError
from ngs_agent.core.evidence.models import EvidenceRecord
from ngs_agent.core.hashing import canonical_json_sha256
from ngs_agent.core.log import get_logger, structured_event

CACHE_SCHEMA = "v1"


class EvidenceCache:
    """Disk-backed cache of evidence records. Never a source of truth."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_age: timedelta | None = None,
        enabled: bool = True,
    ) -> None:
        self.root = Path(root)
        self.max_age = max_age
        self.enabled = enabled
        self._dir = self.root / CACHE_SCHEMA
        self._index = self.root / "index.jsonl"
        if self.enabled:
            self._dir.mkdir(parents=True, exist_ok=True)

    # -- keys ---------------------------------------------------------------

    @staticmethod
    def key_for(
        *, adapter_name: str, adapter_version: str, variant_identity: str, data_type: str
    ) -> str:
        return canonical_json_sha256(
            {
                "adapter_name": adapter_name,
                "adapter_version": adapter_version,
                "variant_identity": variant_identity,
                "data_type": data_type,
            }
        )

    def _path(self, key: str) -> Path:
        # Two-level fan-out keeps any single directory small on large runs.
        return self._dir / key[:2] / f"{key}.json"

    # -- read/write ---------------------------------------------------------

    def get(
        self,
        *,
        adapter_name: str,
        adapter_version: str,
        variant_identity: str,
        data_type: str,
        now: Any,
    ) -> EvidenceRecord | None:
        """Return a cached record, or ``None`` if absent, expired, or corrupt.

        A corrupt cache entry is logged and treated as a miss. It is never
        surfaced as evidence: a half-written JSON file that parses to the wrong
        variant would be far worse than a cache miss.
        """
        if not self.enabled:
            return None
        key = self.key_for(
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            variant_identity=variant_identity,
            data_type=data_type,
        )
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = EvidenceRecord.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValidationError, EvidenceValidationError) as exc:
            structured_event(
                get_logger(),
                30,  # logging.WARNING
                "evidence_cache_corrupt_entry_ignored",
                cache_key=key,
                path=str(path),
                error=str(exc),
            )
            return None

        if record.queried_variant_identity != variant_identity:
            structured_event(
                get_logger(),
                40,  # logging.ERROR
                "evidence_cache_key_collision_discarded",
                cache_key=key,
                expected=variant_identity,
                found=record.queried_variant_identity,
            )
            return None

        if self.max_age is not None and (now - record.retrieved_at) > self.max_age:
            structured_event(
                get_logger(),
                20,  # logging.INFO
                "evidence_cache_expired",
                cache_key=key,
                retrieved_at=record.retrieved_at.isoformat(),
            )
            return None
        return record

    def put(self, record: EvidenceRecord, *, adapter_name: str, adapter_version: str) -> None:
        """Store a record. Failures to write are logged, never raised.

        A cache that cannot be written must not break a review; it only costs a
        re-retrieval.
        """
        if not self.enabled:
            return
        key = self.key_for(
            adapter_name=adapter_name,
            adapter_version=adapter_version,
            variant_identity=record.queried_variant_identity,
            data_type=record.data_type.value,
        )
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
            with self._index.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "key": key,
                            "evidence_id": record.evidence_id,
                            "source": record.source.name,
                            "source_version": record.source.version,
                            "data_type": record.data_type.value,
                            "status": record.status.value,
                            "retrieved_at": record.retrieved_at.isoformat(),
                            "variant_identity": record.queried_variant_identity,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )
        except OSError as exc:
            structured_event(
                get_logger(), 30, "evidence_cache_write_failed", cache_key=key, error=str(exc)
            )

    def clear(self) -> int:
        """Remove every cached entry. Returns the number of files deleted."""
        if not self.root.exists():
            return 0
        removed = 0
        for path in self._dir.rglob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        if self._index.exists():
            try:
                self._index.unlink()
            except OSError:
                pass
        return removed
