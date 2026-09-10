from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def write_text(self, relative_path: str, content: str) -> Path:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_json(self, relative_path: str, payload: dict[str, Any]) -> Path:
        return self.write_text(relative_path, json.dumps(payload, indent=2, sort_keys=True))

    def exists(self, relative_path: str) -> bool:
        return (self.root / relative_path).exists()
