"""Local run-state store for the Temporal-free execution path.

Replaces Temporal's workflow history / ``describe`` with a simple JSON record
per run under ``~/.ngsagent/runs`` (override with ``NGS_HOME`` or
``NGS_RUNS_DIR``). The CLI's ``status`` command reads these records so run
tracking works identically with or without a Temporal server.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from swarm.runner import ngs_home

RUN_STATUSES = (
    "submitted",
    "running",
    "complete",
    "halted",
    "failed_at_alignment",
    "failed",
)


def _runs_dir() -> Path:
    override = os.environ.get("NGS_RUNS_DIR")
    if override:
        return Path(override)
    return ngs_home() / "runs"


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class RunStore:
    """Create, update, and read local JSON run records atomically."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = base_dir or _runs_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        return self.base_dir / f"{run_id}.json"

    def create(self, run_id: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        record: Dict[str, Any] = {
            "run_id": run_id,
            "status": "submitted",
            "mode": "local",
            "created_at": _now(),
            "updated_at": _now(),
            "meta": meta or {},
        }
        self._write(record)
        return record

    def update(self, run_id: str, **fields: Any) -> Dict[str, Any]:
        record = self.get(run_id) or self.create(run_id)
        record.update(fields)
        record["updated_at"] = _now()
        self._write(record)
        return record

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(run_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def list_runs(self) -> list[Dict[str, Any]]:
        records = []
        for path in sorted(self.base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                records.append(data)
        return records

    def _write(self, record: Dict[str, Any]) -> None:
        path = self._path(str(record["run_id"]))
        tmp = path.with_name(f"{path.name}.tmp")
        tmp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
        # Touch so `list_runs` ordering reflects the latest update.
        now = time.time()
        os.utime(path, (now, now))
