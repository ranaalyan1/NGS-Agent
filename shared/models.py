from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class AgentResult:
    status: str
    payload: Dict[str, Any]
    reasoning: str
    halt: bool = False
    halt_reason: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any] | None) -> "AgentResult":
        data = data or {}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        return cls(
            status=str(data.get("status", "ok")),
            payload=payload,
            reasoning=str(data.get("reasoning", "")),
            halt=bool(data.get("halt", False)),
            halt_reason=str(data.get("halt_reason", "")),
        )
