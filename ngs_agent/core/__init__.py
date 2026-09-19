"""NGS-Agent core: the deterministic, evidence-backed variant-review engine.

This package is the *signed classification path*. Everything in it is:

* deterministic — no network, no clock, and no model call may change a
  classification outcome once an evidence snapshot is fixed;
* evidence-linked — no ACMG/AMP criterion may be produced without a structured
  :class:`~ngs_agent.core.evidence.models.EvidenceRecord`;
* auditable — every result is emitted as a versioned JSON contract plus an
  append-only audit record that can be replayed.

LLMs are *never* imported by this package. The optional explanation layer
(:mod:`ngs_agent.core.explanation`) sits outside the signed path and can only
attach read-only metadata to a completed contract.

Boundary rule (see ``docs/architecture/adr/ADR-0001-evidence-boundary.md``):

    No evidence record -> no ACMG criterion.
"""

from __future__ import annotations

from ngs_agent.core.version import (
    CONTRACT_SCHEMA_VERSION,
    ENGINE_VERSION,
    EVIDENCE_SCHEMA_VERSION,
    RULE_SET_VERSIONS,
)

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "ENGINE_VERSION",
    "EVIDENCE_SCHEMA_VERSION",
    "RULE_SET_VERSIONS",
]
