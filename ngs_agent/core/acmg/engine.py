"""The deterministic ACMG/AMP curation engine.

Input: a normalized variant plus the evidence records the ledger says are
usable. Output: a :class:`ClassificationOutcome` that states a label, *why*,
*from which evidence*, what was rejected, what was missing, what conflicts, and
whether a human must review it.

The engine is a pure function of its inputs. It performs no I/O, reads no
clock, calls no model, and generates no randomness. Given the same evidence
snapshot and rule set it returns byte-identical output — which is what makes
:command:`ngsagent replay` a meaningful command rather than a decorative one.

There is no ``confidence`` field, and that is a decision, not an omission. The
ACMG/AMP framework produces a categorical call from a rule table; there is no
measured probability behind "0.95", and presenting one would invite a reviewer
to treat an arbitrary constant as a calibrated risk. Where uncertainty exists
it is expressed structurally: as ``indeterminate`` criteria, ``conflicts``,
``missing_evidence``, ``limitations``, and ``abstained``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.acmg.criteria import CRITERIA, Direction, Strength
from ngs_agent.core.acmg.derivation import (
    CriterionEvaluation,
    DerivationConfig,
    DerivationContext,
    EvaluationState,
    ExternalClassification,
    classify_from_rules,
    count_strengths,
    derive_criteria,
    extract_external_classifications,
    fired_rules,
)
from ngs_agent.core.acmg.rule_sets import LABEL_DISPLAY, RuleSet, get_rule_set
from ngs_agent.core.evidence.models import EvidenceDataType, EvidenceRecord, VerificationStatus
from ngs_agent.core.normalization import NormalizedVariant
from ngs_agent.core.version import ENGINE_VERSION

#: Evidence classes a deployment is expected to configure for a germline review.
#: Used to compute coverage, never to fabricate a criterion.
EXPECTED_EVIDENCE_TYPES: tuple[EvidenceDataType, ...] = (
    EvidenceDataType.MOLECULAR_CONSEQUENCE,
    EvidenceDataType.GENE_DISEASE_MECHANISM,
    EvidenceDataType.ALLELE_FREQUENCY,
    EvidenceDataType.CLINICAL_SIGNIFICANCE,
)

POSITIVE_TIERS = frozenset({"pathogenic", "likely_pathogenic"})
NEGATIVE_TIERS = frozenset({"benign", "likely_benign"})
UNCERTAIN_TIER = "uncertain_significance"


def tier_group(label: str) -> Literal["positive", "negative", "uncertain"]:
    if label in POSITIVE_TIERS:
        return "positive"
    if label in NEGATIVE_TIERS:
        return "negative"
    return "uncertain"


class ConflictRecord(BaseModel):
    """A contradiction the reviewer must resolve. Never resolved automatically."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "criteria_vs_criteria",
        "criteria_vs_external",
        "within_source",
        "normalization",
        "build",
    ]
    severity: Literal["blocking", "notable"]
    description: str
    evidence_ids: tuple[str, ...] = ()
    resolution: str = "Requires human review; NGS-Agent does not resolve conflicting evidence."


class FiredRule(BaseModel):
    """A combination rule that the applied criteria satisfied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    produces: str
    text: str
    citation: str


class EvidenceCoverage(BaseModel):
    """Which expected evidence classes were available, and what the gaps were."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_type: str
    status: Literal["usable", "present_but_unusable", "gap", "not_queried"]
    detail: str = ""


class AbstentionPolicy(BaseModel):
    """When the engine must decline to produce a signed classification.

    Every flag defaults to the safe setting. Relaxing one is a deliberate,
    reviewable configuration change that is recorded in the audit trail.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Abstain when normalization did not complete. Relaxing this also lets the
    #: derivation layer evaluate criteria for an ambiguously normalized allele
    #: (see :class:`~ngs_agent.core.acmg.derivation.DerivationContext`); the
    #: incomplete normalization is disclosed as a limitation either way.
    abstain_on_incomplete_normalization: bool = True
    #: Abstain when any blocking conflict is present.
    abstain_on_blocking_conflict: bool = True
    #: Abstain when no evidence source produced a usable record at all.
    abstain_when_no_usable_evidence: bool = True
    #: Abstain when no criterion could be applied and no authoritative external
    #: classification exists. This is the "we genuinely do not know" case.
    abstain_when_nothing_applies: bool = True
    #: Abstain when any criterion that contributed to the label rests on
    #: evidence that failed identity verification.
    abstain_on_unverified_supporting_evidence: bool = True
    #: Research use only: a human must sign off on every result.
    require_human_review_always: bool = True


class ClassificationOutcome(BaseModel):
    """The engine's complete, self-describing verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    display_label: str
    abstained: bool
    decision_state: Literal[
        "classified", "insufficient_evidence", "conflict", "abstained", "manual_review_required"
    ]
    decision_basis: str
    requires_human_review: bool
    rule_set: str
    rule_set_version: str
    rule_set_citation: str
    engine_version: str = ENGINE_VERSION

    applied_criteria: tuple[CriterionEvaluation, ...] = ()
    rejected_criteria: tuple[CriterionEvaluation, ...] = ()
    indeterminate_criteria: tuple[CriterionEvaluation, ...] = ()
    not_evaluated_criteria: tuple[CriterionEvaluation, ...] = ()

    fired_rules: tuple[FiredRule, ...] = ()
    strength_counts: dict[str, dict[str, int]] = Field(default_factory=dict)

    external_classifications: tuple[ExternalClassification, ...] = ()
    concordance: str = "not_comparable"
    conflicts: tuple[ConflictRecord, ...] = ()
    evidence_coverage: tuple[EvidenceCoverage, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def criteria_by_state(self, state: EvaluationState) -> tuple[CriterionEvaluation, ...]:
        key = {
            EvaluationState.APPLIED: "applied_criteria",
            EvaluationState.REJECTED: "rejected_criteria",
            EvaluationState.INDETERMINATE: "indeterminate_criteria",
            EvaluationState.NOT_EVALUATED: "not_evaluated_criteria",
        }[state]
        return getattr(self, key)


class AcmgEngine:
    """Stateless, deterministic classification engine."""

    def __init__(
        self,
        *,
        rule_set: RuleSet | str | None = None,
        config: DerivationConfig | None = None,
        policy: AbstentionPolicy | None = None,
    ) -> None:
        self.rule_set = rule_set if isinstance(rule_set, RuleSet) else get_rule_set(rule_set)
        self.config = config or DerivationConfig()
        self.policy = policy or AbstentionPolicy()

    # -- public API ---------------------------------------------------------

    def evaluate(
        self,
        *,
        variant: NormalizedVariant,
        gene: str | None,
        usable_evidence: Sequence[EvidenceRecord],
        all_evidence: Sequence[EvidenceRecord] = (),
        normalization_complete: bool = True,
    ) -> ClassificationOutcome:
        """Classify one variant from structured evidence only."""
        all_records = list(all_evidence) or list(usable_evidence)
        context = DerivationContext(
            variant=variant,
            gene=gene,
            rule_set=self.rule_set,
            config=self.config,
            usable_evidence=tuple(usable_evidence),
            all_evidence=tuple(all_records),
            normalization_complete=normalization_complete,
            evaluate_criteria_on_incomplete_normalization=(
                not self.policy.abstain_on_incomplete_normalization
            ),
        )
        evaluations = derive_criteria(context)
        applied = [item for item in evaluations if item.state == EvaluationState.APPLIED.value]
        rejected = [item for item in evaluations if item.state == EvaluationState.REJECTED.value]
        indeterminate = [
            item for item in evaluations if item.state == EvaluationState.INDETERMINATE.value
        ]
        not_evaluated = [
            item for item in evaluations if item.state == EvaluationState.NOT_EVALUATED.value
        ]

        criteria_label, rule, counts = classify_from_rules(self.rule_set, evaluations)
        rules = fired_rules(self.rule_set, evaluations, Direction.PATHOGENIC) + fired_rules(
            self.rule_set, evaluations, Direction.BENIGN
        )
        externals = extract_external_classifications(usable_evidence)
        coverage = _coverage(all_records)
        gaps = _gap_messages(all_records)

        conflicts: list[ConflictRecord] = []
        limitations: list[str] = []

        if not normalization_complete and self.policy.abstain_on_incomplete_normalization:
            conflicts.append(
                ConflictRecord(
                    kind="normalization",
                    severity="blocking",
                    description=(
                        "Variant normalization did not complete, so the allele under review may "
                        "not be the allele the evidence describes."
                    ),
                )
            )

        has_pathogenic = any(item.direction is Direction.PATHOGENIC for item in applied)
        has_benign = any(item.direction is Direction.BENIGN for item in applied)
        if has_pathogenic and has_benign:
            pathogenic_codes = [
                item.code for item in applied if item.direction is Direction.PATHOGENIC
            ]
            benign_codes = [item.code for item in applied if item.direction is Direction.BENIGN]
            conflicts.append(
                ConflictRecord(
                    kind="criteria_vs_criteria",
                    severity="blocking",
                    description=(
                        "Pathogenic and benign criteria were both applied: "
                        f"{pathogenic_codes} versus {benign_codes}. "
                        "ACMG/AMP treats contradictory evidence as uncertain significance; "
                        "NGS-Agent additionally abstains and escalates."
                    ),
                    evidence_ids=tuple(
                        evidence_id for item in applied for evidence_id in item.evidence_ids
                    ),
                )
            )

        conflicts.extend(_source_conflicts(all_records))

        effective_criteria_label = criteria_label or UNCERTAIN_TIER
        external_label = _reconcile_external(externals, conflicts)
        concordance = _concordance(effective_criteria_label, external_label)

        # Discordance is only a *conflict* when NGS-Agent actually formed an
        # independent opinion. With no applied criteria there is nothing to
        # contradict an authoritative source; that case adopts it instead.
        if external_label and applied and tier_group(effective_criteria_label) != tier_group(
            external_label
        ):
            conflicts.append(
                ConflictRecord(
                    kind="criteria_vs_external",
                    severity="blocking",
                    description=(
                        f"ACMG criteria derive "
                        f"{LABEL_DISPLAY.get(effective_criteria_label, effective_criteria_label)} "
                        f"while an authoritative external source reports "
                        f"{LABEL_DISPLAY.get(external_label, external_label)}. NGS-Agent does not "
                        "choose between them."
                    ),
                    evidence_ids=tuple(item.evidence_id for item in externals),
                )
            )
            concordance = "discordant"

        if applied and self.policy.abstain_on_unverified_supporting_evidence:
            unverified = _unverified_supporting(applied, usable_evidence)
            if unverified:
                conflicts.append(
                    ConflictRecord(
                        kind="criteria_vs_criteria",
                        severity="blocking",
                        description=(
                            "Criteria were applied on evidence whose identity could not be "
                            f"verified: {unverified}."
                        ),
                    )
                )

        blocking = [item for item in conflicts if item.severity == "blocking"]

        label, state, basis, abstained = self._decide(
            criteria_label=criteria_label,
            applied=applied,
            externals=externals,
            external_label=external_label,
            blocking=blocking,
            usable_evidence=usable_evidence,
            normalization_complete=normalization_complete,
        )

        limitations.extend(gaps)
        if not normalization_complete:
            limitations.append(
                "Variant normalization did not complete, so the allele under review may not be "
                "the allele the retrieved evidence describes. Every criterion and external "
                "classification below inherits that uncertainty."
            )
        limitations.extend(
            _outcome_limitations(
                label,
                applied,
                externals,
                basis,
                self.rule_set,
                unresolved=[*indeterminate, *rejected],
            )
        )
        if not applied:
            limitations.append(
                "No ACMG/AMP criterion could be applied from the configured evidence sources. "
                f"{len(not_evaluated)} of {len(CRITERIA)} criteria were not evaluated because no "
                "source is configured for them."
            )
        if indeterminate:
            limitations.append(
                "Indeterminate criteria (relevant evidence present but insufficient to decide): "
                f"{[item.code for item in indeterminate]}. These are open questions, not negative "
                "findings."
            )

        return ClassificationOutcome(
            label=label,
            display_label=LABEL_DISPLAY.get(label, label),
            abstained=abstained,
            decision_state=state,
            decision_basis=basis,
            requires_human_review=(
                self.policy.require_human_review_always or abstained or bool(conflicts)
            ),
            rule_set=self.rule_set.name,
            rule_set_version=self.rule_set.version,
            rule_set_citation=self.rule_set.citation,
            applied_criteria=tuple(applied),
            rejected_criteria=tuple(rejected),
            indeterminate_criteria=tuple(indeterminate),
            not_evaluated_criteria=tuple(not_evaluated),
            fired_rules=tuple(
                FiredRule(
                    rule_id=item.rule_id,
                    produces=item.produces,
                    text=item.text,
                    citation=item.citation,
                )
                for item in rules
            ),
            strength_counts=counts,
            external_classifications=tuple(externals),
            concordance=concordance,
            conflicts=tuple(conflicts),
            evidence_coverage=tuple(coverage),
            missing_evidence=tuple(gaps),
            limitations=tuple(_dedupe(limitations)),
        )

    # -- decision -----------------------------------------------------------

    def _decide(
        self,
        *,
        criteria_label: str | None,
        applied: Sequence[CriterionEvaluation],
        externals: Sequence[ExternalClassification],
        external_label: str | None,
        blocking: Sequence[ConflictRecord],
        usable_evidence: Sequence[EvidenceRecord],
        normalization_complete: bool,
    ) -> tuple[str, str, str, bool]:
        """Resolve ``(label, decision_state, decision_basis, abstained)``."""
        if blocking and self.policy.abstain_on_blocking_conflict:
            return UNCERTAIN_TIER, "conflict", "conflicting_evidence", True

        if not normalization_complete and self.policy.abstain_on_incomplete_normalization:
            return (
                criteria_label or UNCERTAIN_TIER,
                "abstained",
                "normalization_incomplete",
                True,
            )

        if not usable_evidence and self.policy.abstain_when_no_usable_evidence:
            return UNCERTAIN_TIER, "insufficient_evidence", "no_usable_evidence", True

        independent = (
            bool(applied) and criteria_label is not None and criteria_label != UNCERTAIN_TIER
        )
        nothing_applied = not applied

        if nothing_applied and external_label:
            # We could not form an independent opinion, but an authoritative
            # curator has. Adopt it with the basis stated explicitly, rather
            # than reporting VUS as though we had evaluated and found nothing.
            return (
                external_label,
                "classified",
                "authoritative_external_classification",
                False,
            )

        if nothing_applied and not external_label:
            if self.policy.abstain_when_nothing_applies:
                return UNCERTAIN_TIER, "insufficient_evidence", "no_criteria_met", True
            return UNCERTAIN_TIER, "classified", "no_criteria_met", False

        if independent:
            basis = "acmg_criteria"
            if external_label and tier_group(external_label) == tier_group(criteria_label or ""):
                basis = "acmg_criteria_concordant_with_authoritative_source"
            return criteria_label or UNCERTAIN_TIER, "classified", basis, False

        # Criteria were applied but no combination rule fired: ACMG/AMP VUS.
        return (
            UNCERTAIN_TIER,
            "classified",
            "acmg_criteria_insufficient_for_any_tier",
            False,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _reconcile_external(
    externals: Sequence[ExternalClassification], conflicts: list[ConflictRecord]
) -> str | None:
    """Determine a single authoritative external tier, recording disagreements."""
    tiers = {item.tier for item in externals if item.tier}
    if not tiers:
        return None
    if len(tiers) > 1:
        conflicts.append(
            ConflictRecord(
                kind="within_source",
                severity="blocking",
                description=(
                    "Authoritative external sources disagree among themselves: "
                    f"{sorted(tiers)}."
                ),
                evidence_ids=tuple(item.evidence_id for item in externals),
            )
        )
        return None
    tier = tiers.pop()
    # Multiple records agreeing at the same tier but different sub-labels
    # (Pathogenic vs Likely pathogenic) are reported as notable, not blocking.
    labels = {item.classification_label for item in externals if item.tier == tier}
    if len(labels) > 1:
        conflicts.append(
            ConflictRecord(
                kind="within_source",
                severity="notable",
                description=(
                    f"Authoritative sources agree on the {tier} tier but differ on the specific "
                    f"label: {sorted(labels)}."
                ),
                evidence_ids=tuple(item.evidence_id for item in externals),
            )
        )
    return tier


def _concordance(criteria_label: str, external_label: str | None) -> str:
    if not external_label:
        return "not_comparable"
    if criteria_label == external_label:
        return "concordant"
    if tier_group(criteria_label) == tier_group(external_label):
        return "partially_concordant"
    return "discordant"


def _source_conflicts(records: Sequence[EvidenceRecord]) -> list[ConflictRecord]:
    conflicts: list[ConflictRecord] = []
    for record in records:
        if record.verification is VerificationStatus.SOURCE_REPORTED_CONFLICT:
            conflicts.append(
                ConflictRecord(
                    kind="within_source",
                    severity="notable",
                    description=(
                        f"{record.source.name} reports internal disagreement about this variant "
                        f"({record.observed_value.get('description_raw')!r}, review status "
                        f"{record.observed_value.get('review_status')!r})."
                    ),
                    evidence_ids=(record.evidence_id,),
                )
            )
    return conflicts


def _unverified_supporting(
    applied: Sequence[CriterionEvaluation], records: Sequence[EvidenceRecord]
) -> list[str]:
    by_id = {record.evidence_id: record for record in records}
    problems: list[str] = []
    for evaluation in applied:
        for evidence_id in evaluation.evidence_ids:
            record = by_id.get(evidence_id)
            if record is None:
                problems.append(f"{evaluation.code}: evidence {evidence_id} is not in the ledger")
            elif not record.usable_as_evidence:
                problems.append(f"{evaluation.code}: evidence {evidence_id} is not usable")
    return problems


def _coverage(records: Sequence[EvidenceRecord]) -> list[EvidenceCoverage]:
    coverage: list[EvidenceCoverage] = []
    for data_type in EXPECTED_EVIDENCE_TYPES:
        matching = [record for record in records if record.data_type is data_type]
        if not matching:
            coverage.append(
                EvidenceCoverage(
                    data_type=data_type.value, status="not_queried",
                        detail="No adapter queried this class."
                )
            )
            continue
        if any(record.usable_as_evidence for record in matching):
            coverage.append(
                EvidenceCoverage(data_type=data_type.value, status="usable", detail="")
            )
        elif any(record.informative for record in matching):
            coverage.append(
                EvidenceCoverage(
                    data_type=data_type.value,
                    status="present_but_unusable",
                    detail=matching[0].describe_gap(),
                )
            )
        else:
            coverage.append(
                EvidenceCoverage(
                    data_type=data_type.value, status="gap", detail=matching[0].describe_gap()
                )
            )
    return coverage


def _gap_messages(records: Sequence[EvidenceRecord]) -> list[str]:
    messages: list[str] = []
    for record in records:
        if record.usable_as_evidence:
            continue
        messages.append(
            f"{record.data_type.value} ({record.source.name}): {record.describe_gap()}"
        )
    return _dedupe(messages)


def _outcome_limitations(
    label: str,
    applied: Sequence[CriterionEvaluation],
    externals: Sequence[ExternalClassification],
    basis: str,
    rule_set: RuleSet,
    unresolved: Sequence[CriterionEvaluation] = (),
) -> list[str]:
    limitations: list[str] = [
        "RESEARCH USE ONLY. NGS-Agent is not a medical device, has not undergone clinical "
        "validation, and is not cleared or approved for diagnostic use. Every result requires "
        "review by a qualified clinical laboratory scientist or clinical geneticist.",
        f"Classification applies rule set {rule_set.name} v{rule_set.version}: {rule_set.citation}",
    ]
    if basis == "authoritative_external_classification":
        limitations.append(
            "The label was adopted from an authoritative external classification because "
            "NGS-Agent could not independently derive any ACMG/AMP criterion from the configured "
            "evidence sources. This is a report of what that source concluded, not an "
            "independent NGS-Agent classification."
        )
    if label in POSITIVE_TIERS:
        limitations.append(
            "A pathogenic-tier result must not be acted on without independent confirmation of "
            "the variant, the transcript, and the underlying evidence by the reviewing laboratory."
        )
    for item in applied:
        for limitation in item.limitations:
            limitations.append(f"{item.code}: {limitation}")
    # Criteria that were *not* applied still rest on evidence, and that evidence
    # still has caveats. An indeterminate PVS1 resting on a seeded gene table is
    # exactly the kind of near-miss a reviewer must be told about: it is the
    # difference between "we could not decide" and "we could not decide, and
    # here is how firm the ground we stopped on actually is".
    for item in unresolved:
        for limitation in item.limitations:
            limitations.append(f"{item.code} ({item.state}): {limitation}")
    for external in externals:
        for limitation in external.limitations:
            limitations.append(f"external[{external.source_name}]: {limitation}")
    if not rule_set.allow_reputable_source_criteria:
        limitations.append(
            f"Rule set {rule_set.name} disables PP5/BP6 per ClinGen SVI; database opinions are "
            "reported as external classifications rather than used as criteria."
        )
    return limitations


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def strength_summary(evaluations: Sequence[CriterionEvaluation]) -> dict[str, Any]:
    """Reporting helper: applied-criteria counts per direction and strength."""
    return {
        direction.value: {
            strength.value: count
            for strength, count in count_strengths(evaluations, direction).items()
            if count
        }
        for direction in Direction
    }


__all__ = [
    "AbstentionPolicy",
    "AcmgEngine",
    "ClassificationOutcome",
    "ConflictRecord",
    "EvidenceCoverage",
    "FiredRule",
    "Strength",
    "tier_group",
]
