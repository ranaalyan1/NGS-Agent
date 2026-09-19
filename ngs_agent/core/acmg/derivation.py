"""Evidence -> ACMG criterion derivation.

This module is the only place in NGS-Agent where an ACMG criterion comes into
existence, and it can only be created from an
:class:`~ngs_agent.core.evidence.models.EvidenceRecord` that has passed
validation and is marked ``usable_as_evidence``.

There is no code path here that accepts a string, a document, a model
completion, or any other unstructured input. That is the whole point: the
function signature *is* the safety control.

Four outcomes per criterion, all recorded:

``applied``
    The criterion is met, at a specific strength, citing specific evidence ids.

``rejected``
    The criterion was evaluated and is not met. The reason is recorded, because
    "we checked BA1 and the frequency was 0.2%" is information a reviewer needs
    and "BA1 absent" is not.

``indeterminate``
    Relevant evidence exists but is insufficient to decide — for example a
    frequency with no allele count, or a null consequence with no transcript
    context. Indeterminate is not rejected: it means the decision is open, and
    it counts toward abstention.

``not_evaluated``
    NGS-Agent has no configured evidence source for this criterion. Reported
    explicitly so the contract can distinguish "we did not look" from "we
    looked and it did not apply".

Safety asymmetry (deliberate): weak observation quality blocks *pathogenic*
criteria (which risks a false-positive clinical action) but only limits
*benign* criteria (which risks leaving a variant as VUS). Failing toward VUS is
the intended failure mode.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.acmg.criteria import (
    CLINGEN_SVI,
    CRITERIA,
    Direction,
    Strength,
    get_criterion,
)
from ngs_agent.core.acmg.rule_sets import CombinationRule, RuleSet
from ngs_agent.core.evidence.models import EvidenceDataType, EvidenceRecord
from ngs_agent.core.normalization import NormalizedVariant
from ngs_agent.core.quantities import ExactRatio, ratio_from_decimal_string

#: Molecular consequence terms that constitute a "null variant" for PVS1.
#: Follows the ACMG/AMP 2015 PVS1 text (nonsense, frameshift, canonical +/-1 or
#: 2 splice sites, initiation codon, single/multiexon deletion) using both
#: Sequence Ontology and ClinVar/VEP vocabulary.
NULL_CONSEQUENCE_TERMS: frozenset[str] = frozenset(
    {
        "nonsense",
        "stop_gained",
        "frameshift_variant",
        "frameshift",
        "transcript_ablation",
        "splice_donor_variant",
        "splice_acceptor_variant",
        "splice_donor_region_variant",
        "start_lost",
        "initiation_codon_variant",
        "stop_lost_and_start_gained",
    }
)

#: Consequence terms that are *not* null but are sometimes mistaken for it.
#: Recorded explicitly in rejections so a reviewer can see the reasoning.
NON_NULL_TRUNCATING_TERMS: frozenset[str] = frozenset(
    {
        "stop_lost",
        "inframe_deletion",
        "inframe_insertion",
        "in_frame_deletion",
        "in_frame_insertion",
        "protein_altering_variant",
        "splice_region_variant",
        "synonymous_variant",
        "missense_variant",
        "start_retained_variant",
        "incomplete_terminal_codon_variant",
    }
)

PATHOGENIC_CLASSIFICATION_LABELS = frozenset(
    {"pathogenic", "likely_pathogenic", "pathogenic_likely_pathogenic"}
)
BENIGN_CLASSIFICATION_LABELS = frozenset({"benign", "likely_benign", "benign_likely_benign"})


class EvaluationState(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"
    NOT_EVALUATED = "not_evaluated"


class CriterionEvaluation(BaseModel):
    """The disposition of one ACMG criterion for one variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    state: str
    direction: Direction
    #: Strength actually applied. ``None`` unless ``state == applied``.
    strength: Strength | None = None
    #: Strength the base guideline assigns, before any rule-set modifier.
    guideline_strength: Strength
    #: Set when a rule-set modifier changed the strength.
    modifier: str | None = None
    evidence_ids: tuple[str, ...] = ()
    reason: str
    citation: str
    rule_set: str
    limitations: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.state == EvaluationState.APPLIED.value


class DerivationConfig(BaseModel):
    """Numeric thresholds used by the derivation layer.

    Every threshold is explicit and versioned through the rule set, because a
    hard-coded constant inside a function is an undocumented clinical decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ACMG BA1: allele frequency greater than 5%.
    ba1_frequency_threshold: str = "0.05"
    #: PM2 rarity ceiling. NOT a ClinGen universal value — see the limitation
    #: attached to every PM2 evaluation.
    pm2_rarity_threshold: str = "0.0001"
    #: Minimum ClinVar assertion ("star") level for PP5/BP6 to be considered at
    #: all. Below this, the submission carries no criteria and is not a
    #: "reputable source" report in the sense the guideline intends.
    pp5_bp6_minimum_stars: int = 1
    #: Require allele count/number before PM2 may be applied.
    require_observation_quality_for_pm2: bool = True
    #: Require transcript/exon (NMD) context before PVS1 may be applied.
    require_transcript_context_for_pvs1: bool = True

    def ba1_threshold(self) -> ExactRatio:
        return ratio_from_decimal_string(self.ba1_frequency_threshold)

    def pm2_threshold(self) -> ExactRatio:
        return ratio_from_decimal_string(self.pm2_rarity_threshold)


class DerivationContext(BaseModel):
    """Everything the derivation layer is allowed to look at."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant: NormalizedVariant
    gene: str | None
    rule_set: RuleSet
    config: DerivationConfig = Field(default_factory=DerivationConfig)
    #: Records that passed validation and are usable as evidence.
    usable_evidence: tuple[EvidenceRecord, ...] = ()
    #: Every record retrieved, including gaps. Used for reasons only.
    all_evidence: tuple[EvidenceRecord, ...] = ()
    #: Whether normalization completed.
    normalization_complete: bool = True
    #: Set only by an operator who has deliberately relaxed
    #: ``AbstentionPolicy.abstain_on_incomplete_normalization``. When False (the
    #: default) an incompletely normalized variant blocks derivation entirely:
    #: if we cannot prove which allele we hold, we cannot prove which allele the
    #: evidence describes. When True the criteria are derived anyway and the
    #: engine still discloses the incomplete normalization as a limitation.
    #:
    #: This flag exists so the policy switch means what its name says. Without
    #: it, relaxing the abstention policy silently changed nothing except the
    #: reported ``decision_state``, because derivation refused independently.
    evaluate_criteria_on_incomplete_normalization: bool = False

    def by_type(self, data_type: EvidenceDataType) -> list[EvidenceRecord]:
        return [record for record in self.usable_evidence if record.data_type is data_type]

    def gap_for(self, data_type: EvidenceDataType) -> EvidenceRecord | None:
        for record in self.all_evidence:
            if record.data_type is data_type and not record.usable_as_evidence:
                return record
        return None


def derive_criteria(context: DerivationContext) -> list[CriterionEvaluation]:
    """Derive the disposition of all 28 criteria for one variant.

    Always returns exactly one evaluation per ACMG/AMP criterion. A criterion
    that could not be evaluated appears as ``not_evaluated`` with a reason —
    never silently omitted.
    """
    if not context.normalization_complete and not (
        context.evaluate_criteria_on_incomplete_normalization
    ):
        return [
            CriterionEvaluation(
                code=code,
                state=EvaluationState.NOT_EVALUATED.value,
                direction=spec.direction,
                guideline_strength=spec.default_strength,
                reason=(
                    "Variant normalization did not complete, so no criterion was evaluated. "
                    "Classifying on an ambiguously normalized variant risks attaching evidence "
                    "for a different allele."
                ),
                citation=spec.citation,
                rule_set=context.rule_set.name,
            )
            for code, spec in CRITERIA.items()
        ]

    derived: dict[str, CriterionEvaluation] = {}
    derived["PVS1"] = _derive_pvs1(context)
    derived["BA1"] = _derive_ba1(context)
    derived["PM2"] = _derive_pm2(context)
    derived["PP5"] = _derive_reputable_source(context, "PP5")
    derived["BP6"] = _derive_reputable_source(context, "BP6")

    evaluations: list[CriterionEvaluation] = []
    for code, spec in CRITERIA.items():
        if code in derived:
            evaluations.append(derived[code])
        else:
            evaluations.append(_not_evaluated(context, code, spec.not_derivable_reason))
    return evaluations


def _not_evaluated(context: DerivationContext, code: str, reason: str) -> CriterionEvaluation:
    spec = get_criterion(code)
    return CriterionEvaluation(
        code=code,
        state=EvaluationState.NOT_EVALUATED.value,
        direction=spec.direction,
        guideline_strength=spec.default_strength,
        reason=reason or "No derivation is implemented for this criterion.",
        citation=spec.citation,
        rule_set=context.rule_set.name,
        limitations=tuple(
            ["This criterion was not evaluated. That is a coverage gap, not a negative finding."]
        ),
    )


def _evaluation(
    context: DerivationContext,
    code: str,
    state: EvaluationState,
    reason: str,
    *,
    evidence: Sequence[EvidenceRecord] = (),
    strength: Strength | None = None,
    modifier: str | None = None,
    limitations: Sequence[str] = (),
) -> CriterionEvaluation:
    spec = get_criterion(code)
    return CriterionEvaluation(
        code=code,
        state=state.value,
        direction=spec.direction,
        strength=strength if state is EvaluationState.APPLIED else None,
        guideline_strength=spec.default_strength,
        modifier=modifier,
        evidence_ids=tuple(record.evidence_id for record in evidence),
        reason=reason,
        citation=spec.citation,
        rule_set=context.rule_set.name,
        limitations=tuple(limitations),
    )


# ---------------------------------------------------------------------------
# PVS1
# ---------------------------------------------------------------------------


def _derive_pvs1(context: DerivationContext) -> CriterionEvaluation:
    """PVS1: null variant in a gene where LoF is a known disease mechanism.

    Three independent facts are required, each from its own evidence type, so
    that a failure in any one is visible:

    1. the variant is a null allele (:data:`EvidenceDataType.MOLECULAR_CONSEQUENCE`);
    2. loss of function is the gene's disease mechanism
       (:data:`EvidenceDataType.GENE_DISEASE_MECHANISM`);
    3. transcript/exon context supports the PVS1 decision tree
       (:data:`EvidenceDataType.NMD_ESCAPE_PREDICTION`).
    """
    code = "PVS1"
    consequences = context.by_type(EvidenceDataType.MOLECULAR_CONSEQUENCE)
    mechanisms = context.by_type(EvidenceDataType.GENE_DISEASE_MECHANISM)
    nmd = context.by_type(EvidenceDataType.NMD_ESCAPE_PREDICTION)

    if not consequences:
        gap = context.gap_for(EvidenceDataType.MOLECULAR_CONSEQUENCE)
        detail = gap.describe_gap() if gap else "no molecular-consequence source configured"
        return _evaluation(
            context,
            code,
            EvaluationState.NOT_EVALUATED,
            f"Molecular consequence is unavailable ({detail}), so null-variant status cannot be "
            "established and PVS1 was not evaluated.",
        )

    terms = _consequence_terms(consequences[0])
    null_terms = sorted(term for term in terms if term in NULL_CONSEQUENCE_TERMS)
    if not null_terms:
        non_null = sorted(term for term in terms if term in NON_NULL_TRUNCATING_TERMS)
        detail = f" (includes non-null truncating/protein-altering terms: {non_null})" if non_null else ""
        return _evaluation(
            context,
            code,
            EvaluationState.REJECTED,
            f"Reported molecular consequence {sorted(terms)} is not a null variant type per "
            f"ACMG/AMP PVS1{detail}.",
            evidence=consequences[:1],
        )

    if not mechanisms:
        gap = context.gap_for(EvidenceDataType.GENE_DISEASE_MECHANISM)
        detail = gap.describe_gap() if gap else "no gene mechanism source configured"
        return _evaluation(
            context,
            code,
            EvaluationState.INDETERMINATE,
            f"Variant is a predicted null allele ({', '.join(null_terms)}), but the gene's disease "
            f"mechanism is not established ({detail}). PVS1 requires loss of function to be a "
            "known disease mechanism for the gene.",
            evidence=consequences[:1],
        )

    mechanism_value = mechanisms[0].observed_value
    if not mechanism_value.get("pvs1_mechanism_applicable", False):
        return _evaluation(
            context,
            code,
            EvaluationState.REJECTED,
            f"Gene mechanism is {mechanism_value.get('mechanism')!r}, for which PVS1 is not "
            "mechanism-applicable. Applying PVS1 outside a loss-of-function mechanism is a known "
            "cause of false pathogenic calls.",
            evidence=consequences[:1] + mechanisms[:1],
            limitations=_limitations_of(mechanisms[0]),
        )

    if context.config.require_transcript_context_for_pvs1 and not nmd:
        gap = context.gap_for(EvidenceDataType.NMD_ESCAPE_PREDICTION)
        detail = gap.describe_gap() if gap else "no transcript/exon context source configured"
        return _evaluation(
            context,
            code,
            EvaluationState.INDETERMINATE,
            f"Null allele ({', '.join(null_terms)}) in a loss-of-function gene, but transcript/exon "
            f"context is unavailable ({detail}), so the PVS1 decision tree cannot be executed. "
            "NGS-Agent does not downgrade PVS1 in the absence of evidence, because a downgrade "
            "requires positive evidence of NMD escape or last-exon location.",
            evidence=consequences[:1] + mechanisms[:1],
            limitations=(
                *_limitations_of(consequences[0]),
                *_limitations_of(mechanisms[0]),
                "PVS1 is indeterminate, not rejected. A transcript-model adapter (MANE Select "
                "exon boundaries) or a curator assertion would resolve it.",
            ),
        )

    limitations = [*_limitations_of(consequences[0]), *_limitations_of(mechanisms[0])]
    if nmd:
        limitations.extend(_limitations_of(nmd[0]))
        limitations.append(
            f"NMD/exon context asserted by {nmd[0].source.citation}: "
            f"{nmd[0].observed_value.get('basis', 'unspecified basis')}."
        )
    return _evaluation(
        context,
        code,
        EvaluationState.APPLIED,
        f"Null variant ({', '.join(null_terms)}) in {mechanism_value.get('gene_symbol')}, where "
        f"loss of function is the recorded disease mechanism; transcript context supports PVS1.",
        evidence=consequences[:1] + mechanisms[:1] + nmd[:1],
        strength=Strength.VERY_STRONG,
        limitations=limitations,
    )


def _consequence_terms(record: EvidenceRecord) -> set[str]:
    raw = record.observed_value.get("consequences") or []
    terms: set[str] = set()
    for item in raw if isinstance(raw, list) else [raw]:
        for token in str(item).replace("&", ",").split(","):
            token = token.strip().lower().replace(" ", "_")
            if token:
                terms.add(token)
    return terms


def _limitations_of(record: EvidenceRecord) -> tuple[str, ...]:
    return tuple(record.limitations)


# ---------------------------------------------------------------------------
# BA1 / PM2 — population frequency
# ---------------------------------------------------------------------------


def _frequency_records(context: DerivationContext) -> list[tuple[EvidenceRecord, ExactRatio]]:
    """Extract usable frequencies as exact ratios, skipping unusable ones."""
    usable: list[tuple[EvidenceRecord, ExactRatio]] = []
    for record in context.by_type(EvidenceDataType.ALLELE_FREQUENCY):
        value = record.observed_value.get("allele_frequency")
        ratio_payload = record.observed_value.get("allele_frequency_ratio")
        try:
            if isinstance(ratio_payload, dict):
                ratio = ExactRatio.model_validate(ratio_payload)
            elif value is not None:
                ratio = ratio_from_decimal_string(str(value))
            else:
                continue
        except Exception:  # noqa: BLE001 - malformed frequency must not crash a review
            continue
        usable.append((record, ratio))
    return usable


def _derive_ba1(context: DerivationContext) -> CriterionEvaluation:
    """BA1: allele frequency greater than 5% in a major population database."""
    code = "BA1"
    frequencies = _frequency_records(context)
    if not frequencies:
        gap = context.gap_for(EvidenceDataType.ALLELE_FREQUENCY)
        detail = gap.describe_gap() if gap else "no allele-frequency source configured"
        return _evaluation(
            context,
            code,
            EvaluationState.NOT_EVALUATED,
            f"No usable allele frequency ({detail}); BA1 was not evaluated. Absence of frequency "
            "data is not evidence of rarity.",
        )

    threshold = context.config.ba1_threshold()
    record, ratio = max(frequencies, key=lambda pair: pair[1])
    if ratio > threshold:
        return _evaluation(
            context,
            code,
            EvaluationState.APPLIED,
            f"Observed allele frequency {ratio.as_decimal_string()} "
            f"({record.observed_value.get('frequency_source')}) exceeds the BA1 threshold "
            f"{threshold.as_decimal_string()}.",
            evidence=[record],
            strength=Strength.STAND_ALONE,
            limitations=_limitations_of(record),
        )
    return _evaluation(
        context,
        code,
        EvaluationState.REJECTED,
        f"Observed allele frequency {ratio.as_decimal_string()} does not exceed the BA1 threshold "
        f"{threshold.as_decimal_string()}.",
        evidence=[record],
        limitations=(
            *_limitations_of(record),
            "Rejection is based on the highest frequency reported by any configured source. A "
            "higher-quality source with ancestry stratification could still meet BA1.",
        ),
    )


def _derive_pm2(context: DerivationContext) -> CriterionEvaluation:
    """PM2: absent from, or extremely rare in, population databases."""
    code = "PM2"
    spec = get_criterion(code)
    modifier: str | None = None
    strength = spec.default_strength
    effective = context.rule_set.effective_strength(code)
    if effective is None:
        return _evaluation(
            context,
            code,
            EvaluationState.NOT_EVALUATED,
            f"{code} is disabled by rule set {context.rule_set.name}.",
        )
    if effective is not spec.default_strength:
        strength = effective
        found = context.rule_set.modifier_for(code)
        modifier = found.rationale if found else "rule-set strength modifier"

    frequencies = _frequency_records(context)
    if not frequencies:
        gap = context.gap_for(EvidenceDataType.ALLELE_FREQUENCY)
        detail = gap.describe_gap() if gap else "no allele-frequency source configured"
        return _evaluation(
            context,
            code,
            EvaluationState.NOT_EVALUATED,
            f"No usable allele frequency ({detail}); {code} was not evaluated.",
        )

    threshold = context.config.pm2_threshold()
    # Use the *highest* reported frequency: rarity must hold across sources.
    record, ratio = max(frequencies, key=lambda pair: pair[1])
    if ratio > threshold:
        return _evaluation(
            context,
            code,
            EvaluationState.REJECTED,
            f"Observed allele frequency {ratio.as_decimal_string()} exceeds the configured "
            f"{code} rarity threshold {threshold.as_decimal_string()}.",
            evidence=[record],
            strength=None,
            modifier=modifier,
            limitations=_limitations_of(record),
        )

    allele_number = record.observed_value.get("allele_number")
    if context.config.require_observation_quality_for_pm2 and allele_number in (None, 0, "0"):
        return _evaluation(
            context,
            code,
            EvaluationState.INDETERMINATE,
            f"Observed allele frequency {ratio.as_decimal_string()} is at or below the "
            f"{threshold.as_decimal_string()} rarity threshold, but no allele number was reported, "
            "so observation quality cannot be assessed. Per ClinGen SVI, insufficient coverage "
            "yields 'not evaluated' rather than 'not met'.",
            evidence=[record],
            modifier=modifier,
            limitations=(
                *_limitations_of(record),
                f"{CLINGEN_SVI}",
            ),
        )

    absent = ratio.numerator == 0
    wording = "absent from" if absent else f"rare in ({ratio.as_decimal_string()})"
    return _evaluation(
        context,
        code,
        EvaluationState.APPLIED,
        f"Variant is {wording} the configured population sources, at or below the {code} rarity "
        f"threshold {threshold.as_decimal_string()}.",
        evidence=[record],
        strength=strength,
        modifier=modifier,
        limitations=(
            *_limitations_of(record),
            f"The {code} rarity threshold {threshold.as_decimal_string()} is a NGS-Agent "
            "implementation default, not a ClinGen universal value. ClinGen does not publish a "
            "disease-independent frequency cutoff for PM2; gene/disease-specific thresholds from "
            "a VCEP specification should replace it.",
        ),
    )


# ---------------------------------------------------------------------------
# PP5 / BP6 — "reputable source reports"
# ---------------------------------------------------------------------------


def _derive_reputable_source(context: DerivationContext, code: str) -> CriterionEvaluation:
    if not context.rule_set.allow_reputable_source_criteria:
        found = context.rule_set.modifier_for(code)
        return _evaluation(
            context,
            code,
            EvaluationState.NOT_EVALUATED,
            f"{code} is disabled by rule set {context.rule_set.name}: "
            f"{found.rationale if found else 'ClinGen SVI recommends against its use.'}",
            limitations=(f"{CLINGEN_SVI}",),
        )

    significances = context.by_type(EvidenceDataType.CLINICAL_SIGNIFICANCE)
    if not significances:
        gap = context.gap_for(EvidenceDataType.CLINICAL_SIGNIFICANCE)
        detail = gap.describe_gap() if gap else "no clinical-significance source configured"
        return _evaluation(
            context,
            code,
            EvaluationState.NOT_EVALUATED,
            f"No usable clinical-significance evidence ({detail}).",
        )

    record = significances[0]
    value = record.observed_value
    label = str(value.get("classification_label", ""))
    stars = value.get("review_status_stars")
    authoritative = bool(value.get("authoritative_review", False))
    limitations = [
        *_limitations_of(record),
        f"{code} is deprecated: {CLINGEN_SVI}",
        "A database classification is an interpretation by other curators, not primary evidence. "
        "It carries supporting weight only.",
    ]

    if authoritative:
        return _evaluation(
            context,
            code,
            EvaluationState.REJECTED,
            f"The source classification ({value.get('description_raw')!r}) carries "
            f"{value.get('review_status')!r} review status. NGS-Agent records authoritative "
            "classifications separately (decision basis 'authoritative_external_classification') "
            f"rather than laundering them through {code}.",
            evidence=[record],
            limitations=tuple(limitations),
        )

    if not isinstance(stars, int) or stars < context.config.pp5_bp6_minimum_stars:
        return _evaluation(
            context,
            code,
            EvaluationState.REJECTED,
            f"Source review status provides {stars} assertion level(s); {code} requires at least "
            f"{context.config.pp5_bp6_minimum_stars} and a reputable, criteria-providing submitter.",
            evidence=[record],
            limitations=tuple(limitations),
        )

    target = PATHOGENIC_CLASSIFICATION_LABELS if code == "PP5" else BENIGN_CLASSIFICATION_LABELS
    if label in target:
        return _evaluation(
            context,
            code,
            EvaluationState.APPLIED,
            f"Source reports {value.get('description_raw')!r} at review status "
            f"{value.get('review_status')!r} without an independent evaluation available to "
            "NGS-Agent.",
            evidence=[record],
            strength=Strength.SUPPORTING,
            limitations=tuple(limitations),
        )

    return _evaluation(
        context,
        code,
        EvaluationState.REJECTED,
        f"Source classification label {label!r} does not support {code}.",
        evidence=[record],
        limitations=tuple(limitations),
    )


# ---------------------------------------------------------------------------
# External (authoritative) classifications
# ---------------------------------------------------------------------------


class ExternalClassification(BaseModel):
    """A classification asserted by an authoritative external curator.

    Kept strictly separate from criteria. A ClinGen VCEP classification is not
    evidence for PP5; it is a competing expert opinion that NGS-Agent reports,
    reconciles with its own derivation, and escalates on disagreement.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_name: str
    source_version: str
    description_raw: str
    classification_label: str
    review_status: str
    review_status_stars: int | str
    authority: str
    accession: str | None
    last_evaluated: str | None
    traits: tuple[str, ...] = ()
    evidence_id: str
    limitations: tuple[str, ...] = ()

    @property
    def tier(self) -> str | None:
        """Map to the ACMG 5-tier space, or ``None`` if not mappable."""
        mapping = {
            "pathogenic": "pathogenic",
            "pathogenic_likely_pathogenic": "pathogenic",
            "pathogenic_low_penetrance": "pathogenic",
            "likely_pathogenic": "likely_pathogenic",
            "likely_pathogenic_low_penetrance": "likely_pathogenic",
            "uncertain_significance": "uncertain_significance",
            "likely_benign": "likely_benign",
            "benign": "benign",
            "benign_likely_benign": "benign",
        }
        return mapping.get(self.classification_label)


def extract_external_classifications(
    records: Sequence[EvidenceRecord],
) -> list[ExternalClassification]:
    """Pull authoritative classifications out of clinical-significance evidence."""
    found: list[ExternalClassification] = []
    for record in records:
        if record.data_type is not EvidenceDataType.CLINICAL_SIGNIFICANCE:
            continue
        if not record.usable_as_evidence:
            continue
        value = record.observed_value
        if not bool(value.get("authoritative_review", False)):
            continue
        stars = value.get("review_status_stars")
        review_status = str(value.get("review_status") or "")
        authority = "practice guideline" if stars == 4 else "expert panel (ClinGen VCEP)"
        found.append(
            ExternalClassification(
                source_name=record.source.name,
                source_version=record.source.version,
                description_raw=str(value.get("description_raw") or ""),
                classification_label=str(value.get("classification_label") or "not_classified"),
                review_status=review_status,
                review_status_stars=stars if isinstance(stars, int) else str(stars),
                authority=authority,
                accession=value.get("accession_version") or value.get("accession"),
                last_evaluated=value.get("last_evaluated"),
                traits=tuple(str(trait) for trait in (value.get("traits") or [])),
                evidence_id=record.evidence_id,
                limitations=_limitations_of(record),
            )
        )
    return found


def count_strengths(
    evaluations: Sequence[CriterionEvaluation], direction: Direction
) -> dict[Strength, int]:
    """Count *distinct applied criteria* per strength level.

    Counting uses a set of criterion codes. The legacy engine summed raw code
    occurrences, so three personas each mentioning PM2 produced ``pm=3`` and a
    Likely Pathogenic call from a single criterion. That inflation is
    structurally impossible here: one criterion contributes at most one count,
    at the strength the rule set assigns it.
    """
    counts: dict[Strength, int] = {strength: 0 for strength in Strength}
    seen: set[str] = set()
    for evaluation in evaluations:
        if evaluation.direction is not direction or not evaluation.applied:
            continue
        if evaluation.code in seen or evaluation.strength is None:
            continue
        seen.add(evaluation.code)
        counts[evaluation.strength] += 1
    return counts


def fired_rules(
    rule_set: RuleSet,
    evaluations: Sequence[CriterionEvaluation],
    direction: Direction,
) -> list[CombinationRule]:
    """Every combination rule satisfied by the applied criteria of one direction."""
    counts = count_strengths(evaluations, direction)
    return [
        rule
        for rule in rule_set.combination_rules
        if rule.direction is direction and rule.matches(counts)
    ]


def classify_from_rules(
    rule_set: RuleSet, evaluations: Sequence[CriterionEvaluation]
) -> tuple[str | None, CombinationRule | None, dict[str, Any]]:
    """Apply the rule set's combination rules in guideline precedence order.

    Returns ``(label, rule, counts)`` where ``label`` is ``None`` when no rule
    fires (the ACMG/AMP outcome for that case is VUS, decided by the caller so
    that abstention and conflict handling stay in one place).

    Precedence follows the guideline: Pathogenic, then Likely Pathogenic, then
    Benign, then Likely Benign. Within a tier, rules are evaluated in the order
    they appear in the guideline table and the first match is reported, but all
    matches are returned to the caller through :func:`fired_rules` so nothing is
    hidden.
    """
    pathogenic_counts = count_strengths(evaluations, Direction.PATHOGENIC)
    benign_counts = count_strengths(evaluations, Direction.BENIGN)
    counts = {"pathogenic": {key.value: value for key, value in pathogenic_counts.items()},
              "benign": {key.value: value for key, value in benign_counts.items()}}

    for label in ("pathogenic", "likely_pathogenic", "benign", "likely_benign"):
        for rule in rule_set.rules_producing(label):  # type: ignore[arg-type]
            relevant = pathogenic_counts if rule.direction is Direction.PATHOGENIC else benign_counts
            if rule.matches(relevant):
                return label, rule, counts
    return None, None, counts
