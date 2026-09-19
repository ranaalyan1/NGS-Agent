"""Deterministic ACMG/AMP curation engine.

Public surface:

* :class:`AcmgEngine` — the engine. Pure function of (variant, evidence, rule set).
* :class:`ClassificationOutcome` — the engine's verdict, fully self-describing.
* :class:`AbstentionPolicy` — when the engine must decline to answer.
* :func:`get_rule_set` — resolve a citable, versioned rule set.

Nothing in this subpackage performs I/O, reads a clock, or imports an LLM
backend. Those are enforced by :file:`tests/core/test_boundary.py`.
"""

from __future__ import annotations

from ngs_agent.core.acmg.criteria import (
    CRITERIA,
    CriterionSpec,
    Direction,
    Strength,
    get_criterion,
)
from ngs_agent.core.acmg.derivation import (
    CriterionEvaluation,
    DerivationConfig,
    EvaluationState,
    ExternalClassification,
)
from ngs_agent.core.acmg.engine import (
    AbstentionPolicy,
    AcmgEngine,
    ClassificationOutcome,
    ConflictRecord,
    EvidenceCoverage,
    FiredRule,
    tier_group,
)
from ngs_agent.core.acmg.rule_sets import (
    ACMG_AMP_2015,
    ACMG_AMP_2015_CLINGEN_SVI,
    CombinationRule,
    RuleSet,
    get_rule_set,
)

__all__ = [
    "ACMG_AMP_2015",
    "ACMG_AMP_2015_CLINGEN_SVI",
    "AbstentionPolicy",
    "AcmgEngine",
    "CRITERIA",
    "ClassificationOutcome",
    "CombinationRule",
    "ConflictRecord",
    "CriterionEvaluation",
    "CriterionSpec",
    "DerivationConfig",
    "Direction",
    "EvaluationState",
    "EvidenceCoverage",
    "ExternalClassification",
    "FiredRule",
    "RuleSet",
    "Strength",
    "get_criterion",
    "get_rule_set",
    "tier_group",
]
