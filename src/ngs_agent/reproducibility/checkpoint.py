from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ngs_agent.agent.models import ResumeState


class CheckpointStore:
    def __init__(self, checkpoint_root: Path) -> None:
        self.checkpoint_root = checkpoint_root
        self.checkpoint_root.mkdir(parents=True, exist_ok=True)

    def state_for(self, run_id: str) -> ResumeState:
        checkpoint_dir = self.checkpoint_root / run_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        return ResumeState(checkpoint_dir=checkpoint_dir, checkpoint_file=checkpoint_dir / "checkpoint.json")

    def load(self, run_id: str) -> dict[str, Any]:
        state = self.state_for(run_id)
        if not state.checkpoint_file.exists():
            return {}
        return json.loads(state.checkpoint_file.read_text(encoding="utf-8"))

    def save(self, run_id: str, payload: dict[str, Any]) -> Path:
        state = self.state_for(run_id)
        state.checkpoint_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return state.checkpoint_file
