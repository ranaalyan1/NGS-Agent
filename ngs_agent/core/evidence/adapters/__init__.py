"""Concrete evidence adapters.

Each adapter is independently importable and independently testable; nothing in
this package imports another adapter.
"""

from __future__ import annotations

from ngs_agent.core.evidence.adapters.clinvar import ADAPTER_VERSION as CLINVAR_ADAPTER_VERSION
from ngs_agent.core.evidence.adapters.clinvar import ClinVarAdapter

__all__ = ["CLINVAR_ADAPTER_VERSION", "ClinVarAdapter"]
