"""FileTracker — ported from Zero's pattern.

Records the version (mtime + size hash) of each file read or written this
session. write_file/edit_file refuse to clobber a file that changed on disk
since it was last read by the agent, preventing accidental data loss.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileVersion:
    path: str
    mtime: float
    size: int
    sha256: str


class FileTracker:
    def __init__(self):
        self._versions: dict[str, FileVersion] = {}

    @staticmethod
    def _hash(contents: bytes) -> str:
        return hashlib.sha256(contents).hexdigest()

    def record_read(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            return
        stat = p.stat()
        contents = p.read_bytes()
        self._versions[str(p.resolve())] = FileVersion(
            path=str(p.resolve()),
            mtime=stat.st_mtime,
            size=stat.st_size,
            sha256=self._hash(contents),
        )

    def check_write(self, path: str | Path) -> tuple[bool, str | None]:
        """Return (ok, reason). ok=False if the file changed on disk since last read."""
        p = str(Path(path).resolve())
        if p not in self._versions:
            return True, None  # never read by agent — allow
        actual = Path(p)
        if not actual.exists():
            return True, None  # was deleted on disk
        stat = actual.stat()
        recorded = self._versions[p]
        if stat.st_mtime == recorded.mtime and stat.st_size == recorded.size:
            return True, None
        # File changed — verify with hash
        contents = actual.read_bytes()
        if self._hash(contents) == recorded.sha256:
            # Same content, just touched
            return True, None
        return False, (
            f"Refusing to write {path}: file changed on disk since last read by agent "
            f"(recorded mtime={recorded.mtime}, actual={stat.st_mtime}). "
            f"Re-read the file first to confirm the latest contents."
        )

    def record_write(self, path: str | Path, contents: bytes) -> None:
        p = str(Path(path).resolve())
        self._versions[p] = FileVersion(
            path=p,
            mtime=Path(p).stat().st_mtime if Path(p).exists() else 0.0,
            size=len(contents),
            sha256=self._hash(contents),
        )
