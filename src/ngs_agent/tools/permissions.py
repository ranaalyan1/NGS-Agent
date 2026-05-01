from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class SafetyLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    EXPENSIVE = "expensive"
    DESTRUCTIVE = "destructive"


@dataclass
class PermissionPolicy:
    require_confirmation_for: set[SafetyLevel] = field(
        default_factory=lambda: {SafetyLevel.EXPENSIVE, SafetyLevel.DESTRUCTIVE}
    )
    allow_destructive_without_confirmation: bool = False

    def requires_confirmation(self, level: SafetyLevel) -> bool:
        if level == SafetyLevel.DESTRUCTIVE and self.allow_destructive_without_confirmation:
            return False
        return level in self.require_confirmation_for
