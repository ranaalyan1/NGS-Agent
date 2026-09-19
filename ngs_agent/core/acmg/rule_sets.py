"""ACMG rule sets: criterion-strength modifiers and combination rules.

A rule set is a **frozen, citable document**. It says two things:

1. How each criterion's strength may be modified (for example, ClinGen SVI's
   recommendation that PM2 be applied at supporting rather than moderate
   strength, or that PP5/BP6 not be used at all).
2. Which combinations of strengths produce which classification.

The combination rules below are a verbatim encoding of Table 5 of Richards et
al. 2015 (ACMG/AMP standards and guidelines, doi:10.1038/gim.2015.30). They are
declared as data with an explicit rule id and citation so that every
classification can print *which line of the guideline* produced it. That is the
difference between an explainable engine and a scoring function.

Two rule sets are registered:

``acmg-amp-2015``
    The guideline as published. PM2 at moderate strength; PP5/BP6 usable; no
    "PVS1 + supporting" pathogenic-likely combination (the guideline does not
    list one).

``acmg-amp-2015+clingen-svi-2020``
    The guideline plus the ClinGen SVI recommendations that have been widely
    adopted by VCEPs: PM2 downgraded to supporting, PP5/BP6 not used, and
    PVS1 + one supporting criterion reaching Likely Pathogenic (as encoded in
    ClinGen VCEP specifications such as HBOP/ATM).

Changing a rule set is a change to the signed classification path. Editing an
existing rule set in place is forbidden; add a new key and a new version.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.acmg.criteria import (
    ABOU_TAYOUN_2018,
    CLINGEN_SVI,
    RICHARDS_2015,
    Direction,
    Strength,
    get_criterion,
)
from ngs_agent.core.errors import RuleSetError
from ngs_agent.core.version import DEFAULT_RULE_SET, RULE_SET_VERSIONS

ClassificationLabel = Literal[
    "pathogenic", "likely_pathogenic", "uncertain_significance", "likely_benign", "benign"
]

#: Display labels. The contract emits these; internal code uses the literals.
LABEL_DISPLAY: dict[str, str] = {
    "pathogenic": "Pathogenic",
    "likely_pathogenic": "Likely Pathogenic",
    "uncertain_significance": "VUS",
    "likely_benign": "Likely Benign",
    "benign": "Benign",
}


class CombinationRule(BaseModel):
    """One line of the guideline's "rules for combining criteria" table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    produces: ClassificationLabel
    #: Minimum counts per strength level required to fire.
    requires: dict[Strength, int] = Field(default_factory=dict)
    #: Maximum counts (exclusive upper bounds) where the guideline states a range,
    #: e.g. Likely Pathogenic (ii) "1 Strong AND 1-2 Moderate".
    at_most: dict[Strength, int] = Field(default_factory=dict)
    direction: Direction
    citation: str = RICHARDS_2015
    text: str = ""

    def matches(self, counts: dict[Strength, int]) -> bool:
        for strength, minimum in self.requires.items():
            if counts.get(strength, 0) < minimum:
                return False
        for strength, maximum in self.at_most.items():
            if counts.get(strength, 0) > maximum:
                return False
        return True


class StrengthModifier(BaseModel):
    """A rule-set-level change to how one criterion is used."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    action: Literal["modify_strength", "disable"]
    strength: Strength | None = None
    rationale: str
    citation: str = CLINGEN_SVI


class RuleSet(BaseModel):
    """A frozen, versioned classification rule set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str
    description: str
    citation: str
    combination_rules: tuple[CombinationRule, ...]
    modifiers: tuple[StrengthModifier, ...] = Field(default_factory=tuple)
    #: Whether PP5/BP6 (reputable-source) criteria may be applied at all.
    allow_reputable_source_criteria: bool = True
    #: Whether BA1 alone classifies Benign even when no other evidence exists.
    #: Always true in ACMG/AMP; exposed so a deployment cannot accidentally
    #: turn it off without a version bump.
    ba1_is_standalone: bool = True

    def modifier_for(self, code: str) -> StrengthModifier | None:
        for modifier in self.modifiers:
            if modifier.code == code:
                return modifier
        return None

    def effective_strength(self, code: str) -> Strength | None:
        """The strength this rule set applies ``code`` at, or ``None`` if disabled."""
        spec = get_criterion(code)
        modifier = self.modifier_for(code)
        if modifier is None:
            return spec.default_strength
        if modifier.action == "disable":
            return None
        if modifier.strength is None:
            raise RuleSetError(f"modifier for {code} has action=modify_strength but no strength")
        if not spec.allows(modifier.strength):
            raise RuleSetError(
                f"{code} does not permit strength {modifier.strength.value} "
                f"(allowed: {[s.value for s in spec.allowed_strengths]})"
            )
        return modifier.strength

    def rules_producing(self, label: ClassificationLabel) -> tuple[CombinationRule, ...]:
        return tuple(rule for rule in self.combination_rules if rule.produces == label)


# ---------------------------------------------------------------------------
# Richards 2015 Table 5, encoded verbatim
# ---------------------------------------------------------------------------

_VS = Strength.VERY_STRONG
_S = Strength.STRONG
_M = Strength.MODERATE
_P = Strength.SUPPORTING
_SA = Strength.STAND_ALONE

_RICHARDS_PATHOGENIC: tuple[CombinationRule, ...] = (
    CombinationRule(
        rule_id="R2015-P-i-a",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_VS: 1, _S: 1},
        text="1 Very strong (PVS1) AND >=1 Strong (PS1-PS4)",
    ),
    CombinationRule(
        rule_id="R2015-P-i-b",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_VS: 1, _M: 2},
        text="1 Very strong (PVS1) AND >=2 Moderate (PM1-PM6)",
    ),
    CombinationRule(
        rule_id="R2015-P-i-c",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_VS: 1, _M: 1, _P: 1},
        text="1 Very strong (PVS1) AND 1 Moderate AND 1 Supporting",
    ),
    CombinationRule(
        rule_id="R2015-P-i-d",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_VS: 1, _P: 2},
        text="1 Very strong (PVS1) AND >=2 Supporting (PP1-PP5)",
    ),
    CombinationRule(
        rule_id="R2015-P-ii",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_S: 2},
        text=">=2 Strong (PS1-PS4)",
    ),
    CombinationRule(
        rule_id="R2015-P-iii",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_S: 1, _M: 3},
        text="1 Strong AND >=3 Moderate",
    ),
    CombinationRule(
        rule_id="R2015-P-iv",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_S: 1, _M: 2, _P: 2},
        text="1 Strong AND 2 Moderate AND >=2 Supporting",
    ),
    CombinationRule(
        rule_id="R2015-P-v",
        produces="pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_S: 1, _M: 1, _P: 4},
        text="1 Strong AND 1 Moderate AND >=4 Supporting",
    ),
)

_RICHARDS_LIKELY_PATHOGENIC: tuple[CombinationRule, ...] = (
    CombinationRule(
        rule_id="R2015-LP-i",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_VS: 1, _M: 1},
        text="1 Very strong (PVS1) AND 1 Moderate (PM1-PM6)",
    ),
    CombinationRule(
        rule_id="R2015-LP-ii",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_S: 1, _M: 1},
        at_most={_M: 2},
        text="1 Strong AND 1-2 Moderate",
    ),
    CombinationRule(
        rule_id="R2015-LP-iii",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_S: 1, _P: 2},
        text="1 Strong AND >=2 Supporting (PP1-PP5)",
    ),
    CombinationRule(
        rule_id="R2015-LP-iv",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_M: 3},
        text=">=3 Moderate",
    ),
    CombinationRule(
        rule_id="R2015-LP-v",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_M: 2, _P: 2},
        text="2 Moderate AND >=2 Supporting",
    ),
    CombinationRule(
        rule_id="R2015-LP-vi",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_M: 1, _P: 4},
        text="1 Moderate AND >=4 Supporting",
    ),
)

_RICHARDS_BENIGN: tuple[CombinationRule, ...] = (
    CombinationRule(
        rule_id="R2015-B-i",
        produces="benign",
        direction=Direction.BENIGN,
        requires={_SA: 1},
        text="1 Stand-alone (BA1)",
    ),
    CombinationRule(
        rule_id="R2015-B-ii",
        produces="benign",
        direction=Direction.BENIGN,
        requires={_S: 2},
        text=">=2 Strong (BS1-BS4)",
    ),
)

_RICHARDS_LIKELY_BENIGN: tuple[CombinationRule, ...] = (
    CombinationRule(
        rule_id="R2015-LB-i",
        produces="likely_benign",
        direction=Direction.BENIGN,
        requires={_S: 1, _P: 2},
        text="1 Strong AND >=2 Supporting",
    ),
    CombinationRule(
        rule_id="R2015-LB-ii",
        produces="likely_benign",
        direction=Direction.BENIGN,
        requires={_P: 2},
        text=">=2 Supporting (BP1-BP7)",
    ),
)

ACMG_AMP_2015 = RuleSet(
    name="acmg-amp-2015",
    version=RULE_SET_VERSIONS["acmg-amp-2015"],
    description=(
        "ACMG/AMP 2015 standards and guidelines, encoded verbatim. No ClinGen SVI "
        "modifications are applied."
    ),
    citation=RICHARDS_2015,
    combination_rules=(
        *_RICHARDS_PATHOGENIC,
        *_RICHARDS_LIKELY_PATHOGENIC,
        *_RICHARDS_BENIGN,
        *_RICHARDS_LIKELY_BENIGN,
    ),
    modifiers=(),
    allow_reputable_source_criteria=True,
)

#: ClinGen SVI-derived modifiers layered onto the 2015 guideline.
_SVI_MODIFIERS: tuple[StrengthModifier, ...] = (
    StrengthModifier(
        code="PM2",
        action="modify_strength",
        strength=Strength.SUPPORTING,
        rationale=(
            "ClinGen SVI recommends PM2 be applied only at supporting strength: rarity or "
            "absence in population databases is not robust evidence of pathogenicity, "
            "particularly for recessive conditions."
        ),
    ),
    StrengthModifier(
        code="PP5",
        action="disable",
        rationale=(
            "ClinGen SVI recommends PP5 not be used. 'Reputable source' is undefined and the "
            "criterion permits a database opinion to substitute for evidence. Where a source is "
            "authoritative (ClinGen VCEP or practice guideline), NGS-Agent records it as an "
            "external classification with an explicit decision basis instead."
        ),
    ),
    StrengthModifier(
        code="BP6",
        action="disable",
        rationale="ClinGen SVI recommends BP6 not be used; see the PP5 rationale.",
    ),
)

#: Additional combination rules endorsed by ClinGen VCEP specifications.
_SVI_LIKELY_PATHOGENIC: tuple[CombinationRule, ...] = (
    CombinationRule(
        rule_id="SVI-LP-vii",
        produces="likely_pathogenic",
        direction=Direction.PATHOGENIC,
        requires={_VS: 1, _P: 1},
        text="1 Very strong (PVS1) AND 1 Supporting (typically PM2_Supporting)",
        citation=CLINGEN_SVI,
    ),
)

ACMG_AMP_2015_CLINGEN_SVI = RuleSet(
    name="acmg-amp-2015+clingen-svi-2020",
    version=RULE_SET_VERSIONS["acmg-amp-2015+clingen-svi-2020"],
    description=(
        "ACMG/AMP 2015 plus widely adopted ClinGen SVI modifications: PM2 at supporting "
        "strength, PP5/BP6 disabled, and PVS1 + 1 supporting reaching Likely Pathogenic."
    ),
    citation=f"{RICHARDS_2015} With modifications per {CLINGEN_SVI}",
    combination_rules=(
        *_RICHARDS_PATHOGENIC,
        *_RICHARDS_LIKELY_PATHOGENIC,
        *_SVI_LIKELY_PATHOGENIC,
        *_RICHARDS_BENIGN,
        *_RICHARDS_LIKELY_BENIGN,
    ),
    modifiers=_SVI_MODIFIERS,
    allow_reputable_source_criteria=False,
)

REGISTRY: dict[str, RuleSet] = {
    ACMG_AMP_2015.name: ACMG_AMP_2015,
    ACMG_AMP_2015_CLINGEN_SVI.name: ACMG_AMP_2015_CLINGEN_SVI,
}


def get_rule_set(name: str | None = None) -> RuleSet:
    """Resolve a rule set by name. Unknown names raise; nothing defaults silently."""
    key = (name or DEFAULT_RULE_SET).strip()
    rule_set = REGISTRY.get(key)
    if rule_set is None:
        raise RuleSetError(
            f"Unknown rule set {name!r}. Registered: {', '.join(sorted(REGISTRY))}. "
            "A rule set is a citable document; NGS-Agent will not approximate one."
        )
    return rule_set


def pvs1_strength_policy() -> str:
    """Documented policy for PVS1 strength downgrade.

    Kept as a function returning text so the policy is inspectable and testable
    rather than buried in derivation branches.
    """
    return (
        f"PVS1 strength follows the decision tree of {ABOU_TAYOUN_2018}. NGS-Agent applies "
        "PVS1 at very_strong only when transcript/exon context establishes that the variant is "
        "predicted to undergo nonsense-mediated decay (or is otherwise not in the last exon / "
        "last 50 bp of the final exon junction). Without that context PVS1 is recorded as "
        "indeterminate and is NOT applied at a reduced strength, because a downgrade requires "
        "positive evidence of NMD escape rather than the absence of evidence either way."
    )
