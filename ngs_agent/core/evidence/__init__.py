"""Evidence retrieval, validation, and the typed evidence model.

The evidence layer is the only place in NGS-Agent where the outside world is
allowed to speak. Everything downstream — the ledger, the ACMG engine, the JSON
contract, the audit record — consumes :class:`EvidenceRecord` objects and
nothing else.

Public entry points:

* :mod:`ngs_agent.core.evidence.models` — the record type and its invariants.
* :mod:`ngs_agent.core.evidence.base` — the adapter protocol every source implements.
* :mod:`ngs_agent.core.evidence.registry` — adapter discovery and configuration.
* :mod:`ngs_agent.core.evidence.adapters` — ClinVar, offline packs, replay snapshots.
"""

from __future__ import annotations

from ngs_agent.core.evidence.base import (
    AdapterCapabilityError,
    AdapterDeclaration,
    BaseEvidenceAdapter,
    EvidenceAdapter,
)
from ngs_agent.core.evidence.models import (
    ApplicabilityStatus,
    EvidenceDataType,
    EvidenceRecord,
    EvidenceSource,
    EvidenceStatus,
    RetrievalDetail,
    VerificationStatus,
    utc_now,
)

__all__ = [
    "AdapterCapabilityError",
    "AdapterDeclaration",
    "ApplicabilityStatus",
    "BaseEvidenceAdapter",
    "EvidenceAdapter",
    "EvidenceDataType",
    "EvidenceRecord",
    "EvidenceSource",
    "EvidenceStatus",
    "RetrievalDetail",
    "VerificationStatus",
    "utc_now",
]
