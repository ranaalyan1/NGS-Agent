"""Run provenance manifest.

Records every artifact a run produces as one JSON line in ``manifest.jsonl``,
with a full SHA-256 of each file's contents. Hashing is *streaming* (1 MiB
chunks) so multi-gigabyte BAMs are hashed with constant memory — checksums
are never truncated or dropped for large files, because a checksum that is
sometimes ``None`` is worse than no checksum at all.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MiB keeps memory flat for arbitrarily large files


def sha256_file(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Stream-compute the SHA-256 of ``path``.

    The file is read in ``chunk_size`` blocks so memory use is constant
    regardless of file size; there is deliberately no size cutoff that could
    degrade the digest to ``None``.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass
class ArtifactRecord:
    path: str
    size_bytes: int
    sha256: str
    role: str = "artifact"
    tool: str = ""
    command: list[str] = field(default_factory=list)
    recorded_at: str = ""

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


class RunManifest:
    """Accumulates artifact records for one run and writes ``manifest.jsonl``."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.records: list[ArtifactRecord] = []
        self._seen_paths: set[str] = set()

    def record(
        self,
        path: Path | str,
        role: str = "artifact",
        tool: str = "",
        command: list[str] | None = None,
    ) -> ArtifactRecord | None:
        """Hash and record one file. Returns ``None`` when it does not exist."""
        resolved = Path(path)
        if not resolved.is_file():
            logger.debug("Manifest skip (not a file): %s", resolved)
            return None
        key = str(resolved.resolve())
        if key in self._seen_paths:
            return next(record for record in self.records if record.path == key)
        record = ArtifactRecord(
            path=key,
            size_bytes=resolved.stat().st_size,
            sha256=sha256_file(resolved),
            role=role,
            tool=tool,
            command=list(command or []),
            recorded_at=datetime.now(tz=UTC).isoformat(),
        )
        self._seen_paths.add(key)
        self.records.append(record)
        return record

    def record_outcome(
        self, step_name: str, artifacts: dict[str, Any], tool: str = ""
    ) -> list[ArtifactRecord]:
        """Record every file path found in a step outcome's artifact payload."""
        recorded: list[ArtifactRecord] = []
        for path in _collect_paths(artifacts):
            record = self.record(path, role=f"step:{step_name}", tool=tool)
            if record is not None:
                recorded.append(record)
        return recorded

    def write_jsonl(self, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(record.to_jsonl() + "\n")
        return destination

    def write_summary(self, destination: Path) -> Path:
        payload = {
            "run_id": self.run_id,
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "artifact_count": len(self.records),
            "total_bytes": sum(record.size_bytes for record in self.records),
            "artifacts": [asdict(record) for record in self.records],
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return destination


def _collect_paths(value: Any) -> list[Path]:
    """Recursively collect existing-file paths from a JSON-ish payload."""
    found: list[Path] = []
    if isinstance(value, str):
        candidate = Path(value)
        if value.startswith(("/", "./", "~")) or candidate.suffix in {
            ".bam",
            ".bai",
            ".sam",
            ".fastq",
            ".gz",
            ".html",
            ".json",
            ".txt",
            ".tsv",
            ".csv",
            ".vcf",
            ".pdf",
            ".png",
            ".zip",
        }:
            if candidate.exists() and candidate.is_file():
                found.append(candidate)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_collect_paths(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_collect_paths(item))
    return found
