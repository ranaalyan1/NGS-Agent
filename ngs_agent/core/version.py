"""Version stamps for every artefact the core engine emits.

These values are written into the JSON contract *and* into the audit record, so
a result produced six months from now can be reproduced against the exact rule
set and schema that produced it. Bumping a value here is a breaking change to
the signed classification path and must be called out in the pull request.
"""

from __future__ import annotations

#: Schema of :class:`ngs_agent.core.contract.VariantReviewResult`.
CONTRACT_SCHEMA_VERSION = "1.0.0"

#: Schema of :class:`ngs_agent.core.evidence.models.EvidenceRecord`.
EVIDENCE_SCHEMA_VERSION = "1.0.0"

#: Schema of :class:`ngs_agent.core.audit.AuditRecord`.
AUDIT_SCHEMA_VERSION = "1.0.0"

#: Version of the deterministic classification engine.
#:
#: This is deliberately *not* the package version: the engine can change while
#: the CLI does not, and reproducibility depends on the engine identity.
ENGINE_VERSION = "core-1.0.0"

#: Version of the variant normalization/identity algorithm.
NORMALIZATION_VERSION = "norm-1.0.0"

#: Registered ACMG rule sets and their own versions.
#:
#: A rule set is a citable, frozen document. Changing the rules means adding a
#: new key, never editing an existing one in place.
RULE_SET_VERSIONS: dict[str, str] = {
    "acmg-amp-2015": "1.0.0",
    "acmg-amp-2015+clingen-svi-2020": "1.0.0",
}

DEFAULT_RULE_SET = "acmg-amp-2015"

#: Version of the curated gene/disease mechanism table shipped with the engine.
GENE_MECHANISM_TABLE_VERSION = "gene-mechanism-0.1.0-curated-seed"
