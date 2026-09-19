"""ACMG/AMP criterion definitions.

Every criterion is declared as data: its default strength, its direction, the
evidence data types it can be derived from, and a citation. Declaring them as
data (rather than as branches in a function) is what lets the engine explain a
*rejected* criterion as precisely as an applied one, and what lets a rule set
change a strength without touching derivation logic.

Primary source: Richards S, Aziz N, Bale S, et al. "Standards and guidelines
for the interpretation of sequence variants: a joint consensus recommendation
of the American College of Medical Genetics and Genomics and the Association
for Molecular Pathology." Genet Med. 2015;17(5):405-424.
doi:10.1038/gim.2015.30

Where ClinGen's Sequence Variant Interpretation (SVI) working group has issued
a recommendation that changes a criterion's use, that is recorded in
:mod:`ngs_agent.core.acmg.rule_sets` as an explicit, versioned modifier — not
by editing this table.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.errors import RuleSetError
from ngs_agent.core.evidence.models import EvidenceDataType

RICHARDS_2015 = (
    "Richards S, et al. Standards and guidelines for the interpretation of sequence variants "
    "(ACMG/AMP). Genet Med. 2015;17(5):405-424. doi:10.1038/gim.2015.30"
)
CLINGEN_SVI = (
    "ClinGen Sequence Variant Interpretation Working Group recommendations. "
    "https://www.clinicalgenome.org/working-groups/sequence-variant-interpretation/"
)
ABOU_TAYOUN_2018 = (
    "Abou Tayoun AN, et al. Recommendations for interpreting the loss of function PVS1 ACMG/AMP "
    "variant criterion. Hum Mutat. 2018;39(11):1517-1524. doi:10.1002/humu.23626"
)
TAVTIGIAN_2018 = (
    "Tavtigian SV, et al. Modeling the ACMG/AMP guideline criteria as a Bayesian classification "
    "framework. Genet Med. 2018;20(11):1122-1130. doi:10.1038/gim.2018.109"
)


class Strength(str, Enum):
    """ACMG/AMP evidence strength levels.

    There is deliberately no numeric value attached to these in the signed
    path. Tavtigian's Bayesian point system exists and is cited in
    :data:`TAVTIGIAN_2018`, but assigning "6 points = likely pathogenic, 90%
    certainty" is a model, not a measurement, and NGS-Agent will not present a
    model's output as a calibrated probability.
    """

    STAND_ALONE = "stand_alone"
    VERY_STRONG = "very_strong"
    STRONG = "strong"
    MODERATE = "moderate"
    SUPPORTING = "supporting"


class Direction(str, Enum):
    PATHOGENIC = "pathogenic"
    BENIGN = "benign"


#: Strength ordering, weakest last. Used only for reporting and for choosing
#: the strongest applicable modifier — never for arithmetic on evidence.
STRENGTH_RANK: dict[Strength, int] = {
    Strength.SUPPORTING: 1,
    Strength.MODERATE: 2,
    Strength.STRONG: 3,
    Strength.VERY_STRONG: 4,
    Strength.STAND_ALONE: 5,
}


class CriterionSpec(BaseModel):
    """Static definition of one ACMG/AMP criterion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    direction: Direction
    default_strength: Strength
    description: str
    citation: str = RICHARDS_2015
    #: Evidence data types the deterministic derivation can consume.
    evidence_types: tuple[EvidenceDataType, ...] = Field(default_factory=tuple)
    #: True when NGS-Agent can currently derive this criterion from evidence it
    #: knows how to retrieve. False criteria are reported as ``not_evaluated``
    #: with a reason, which is how the contract communicates "we did not look"
    #: separately from "we looked and it did not apply".
    derivable: bool = False
    #: Why a non-derivable criterion cannot yet be derived.
    not_derivable_reason: str = ""
    #: Strength modifiers this criterion is known to accept, e.g.
    #: ``("supporting",)`` for PM2 under ClinGen SVI.
    allowed_strengths: tuple[Strength, ...] = Field(default_factory=tuple)
    notes: str = ""

    def allows(self, strength: Strength) -> bool:
        return not self.allowed_strengths or strength in self.allowed_strengths


def _spec(
    code: str,
    direction: Direction,
    strength: Strength,
    description: str,
    *,
    evidence_types: tuple[EvidenceDataType, ...] = (),
    derivable: bool = False,
    not_derivable_reason: str = "",
    allowed_strengths: tuple[Strength, ...] = (),
    citation: str = RICHARDS_2015,
    notes: str = "",
) -> CriterionSpec:
    return CriterionSpec(
        code=code,
        direction=direction,
        default_strength=strength,
        description=description,
        citation=citation,
        evidence_types=evidence_types,
        derivable=derivable,
        not_derivable_reason=not_derivable_reason,
        allowed_strengths=allowed_strengths or (strength,),
        notes=notes,
    )


_NO_SOURCE = "No evidence source for this criterion is configured in this deployment."

#: The 28 ACMG/AMP 2015 criteria, in guideline order.
CRITERIA: dict[str, CriterionSpec] = {
    spec.code: spec
    for spec in (
        # -- Pathogenic: very strong ---------------------------------------
        _spec(
            "PVS1",
            Direction.PATHOGENIC,
            Strength.VERY_STRONG,
            "Null variant (nonsense, frameshift, canonical +/-1 or 2 splice sites, initiation "
            "codon, single or multiexon deletion) in a gene where loss of function is a known "
            "mechanism of disease.",
            evidence_types=(
                EvidenceDataType.MOLECULAR_CONSEQUENCE,
                EvidenceDataType.GENE_DISEASE_MECHANISM,
                EvidenceDataType.NMD_ESCAPE_PREDICTION,
            ),
            derivable=True,
            allowed_strengths=(Strength.VERY_STRONG, Strength.STRONG, Strength.MODERATE),
            citation=f"{RICHARDS_2015} Strength modifiers: {ABOU_TAYOUN_2018}",
            notes=(
                "Requires all three of: a null-variant consequence, an established loss-of-function "
                "disease mechanism for the gene, and transcript/exon context sufficient to run the "
                "PVS1 decision tree. NGS-Agent never infers PVS1 from a consequence alone."
            ),
        ),
        # -- Pathogenic: strong --------------------------------------------
        _spec(
            "PS1",
            Direction.PATHOGENIC,
            Strength.STRONG,
            "Same amino acid change as a previously established pathogenic variant regardless of "
            "nucleotide change.",
            not_derivable_reason=(
                "Requires a protein-level equivalence search over established pathogenic variants. "
                + _NO_SOURCE
            ),
        ),
        _spec(
            "PS2",
            Direction.PATHOGENIC,
            Strength.STRONG,
            "De novo (both maternity and paternity confirmed) in a patient with the disease and "
            "no family history.",
            evidence_types=(EvidenceDataType.DE_NOVO,),
            not_derivable_reason="De novo status is a family/trio observation; no source configured.",
        ),
        _spec(
            "PS3",
            Direction.PATHOGENIC,
            Strength.STRONG,
            "Well-established in vitro or in vivo functional studies supportive of a damaging "
            "effect on the gene or gene product.",
            evidence_types=(EvidenceDataType.FUNCTIONAL_ASSAY,),
            not_derivable_reason=(
                "Requires a curated functional-assay database with validation status per assay "
                "(ClinGen SVI PS3/BS3 guidance). " + _NO_SOURCE
            ),
        ),
        _spec(
            "PS4",
            Direction.PATHOGENIC,
            Strength.STRONG,
            "The prevalence of the variant in affected individuals is significantly increased "
            "compared with the prevalence in controls.",
            evidence_types=(EvidenceDataType.CASE_CONTROL,),
            not_derivable_reason="No case-control source configured.",
        ),
        # -- Pathogenic: moderate ------------------------------------------
        _spec(
            "PM1",
            Direction.PATHOGENIC,
            Strength.MODERATE,
            "Located in a mutational hot spot and/or critical and well-established functional "
            "domain (e.g. active site of an enzyme) without benign variation.",
            evidence_types=(EvidenceDataType.MUTATIONAL_HOTSPOT,),
            not_derivable_reason="No domain/hotspot source configured (e.g. ClinGen domain definitions).",
        ),
        _spec(
            "PM2",
            Direction.PATHOGENIC,
            Strength.MODERATE,
            "Absent from controls (or at extremely low frequency if recessive) in Exome "
            "Sequencing Project, 1000 Genomes Project, or Exome Aggregation Consortium.",
            evidence_types=(EvidenceDataType.ALLELE_FREQUENCY,),
            derivable=True,
            allowed_strengths=(Strength.MODERATE, Strength.SUPPORTING),
            citation=f"{RICHARDS_2015} Strength modification: {CLINGEN_SVI}",
            notes=(
                "ClinGen SVI recommends PM2 be applied only at supporting strength. The "
                "acmg-amp-2015 rule set applies it at moderate (guideline text); the "
                "acmg-amp-2015+clingen-svi-2020 rule set applies it at supporting. The choice is "
                "recorded on every evaluation."
            ),
        ),
        _spec(
            "PM3",
            Direction.PATHOGENIC,
            Strength.MODERATE,
            "For recessive disorders, detected in trans with a pathogenic variant.",
            not_derivable_reason="Requires phasing and a second variant's classification.",
        ),
        _spec(
            "PM4",
            Direction.PATHOGENIC,
            Strength.MODERATE,
            "Protein length changes as a result of in-frame deletions/insertions in a nonrepeat "
            "region or stop-loss variants.",
            not_derivable_reason="Requires protein-level consequence and repeat-region annotation.",
        ),
        _spec(
            "PM5",
            Direction.PATHOGENIC,
            Strength.MODERATE,
            "Novel missense change at an amino acid residue where a different missense change "
            "determined to be pathogenic has been seen before.",
            not_derivable_reason="Requires residue-level pathogenic missense enumeration.",
        ),
        _spec(
            "PM6",
            Direction.PATHOGENIC,
            Strength.MODERATE,
            "Assumed de novo, but without confirmation of paternity and maternity.",
            evidence_types=(EvidenceDataType.DE_NOVO,),
            not_derivable_reason="De novo status is a family/trio observation; no source configured.",
        ),
        # -- Pathogenic: supporting ----------------------------------------
        _spec(
            "PP1",
            Direction.PATHOGENIC,
            Strength.SUPPORTING,
            "Cosegregation with disease in multiple affected family members in a gene "
            "definitively known to cause the disease.",
            evidence_types=(EvidenceDataType.SEGREGATION,),
            not_derivable_reason="Segregation data is family-specific; no source configured.",
        ),
        _spec(
            "PP2",
            Direction.PATHOGENIC,
            Strength.SUPPORTING,
            "Missense variant in a gene that has a low rate of benign missense variation and in "
            "which missense variants are a common mechanism of disease.",
            evidence_types=(EvidenceDataType.SEQUENCE_CONSTRAINT,),
            not_derivable_reason="Requires gene-level missense constraint plus mechanism; no source configured.",
        ),
        _spec(
            "PP3",
            Direction.PATHOGENIC,
            Strength.SUPPORTING,
            "Multiple lines of computational evidence support a deleterious effect on the gene or "
            "gene product (conservation, evolutionary, splicing impact, etc.).",
            evidence_types=(
                EvidenceDataType.SPLICING_PREDICTION,
                EvidenceDataType.MISSENSE_PREDICTION,
            ),
            not_derivable_reason=(
                "Requires calibrated in silico predictors with published thresholds (ClinGen SVI "
                "PP3/BP4 calibration). No predictor source configured."
            ),
        ),
        _spec(
            "PP4",
            Direction.PATHOGENIC,
            Strength.SUPPORTING,
            "Patient's phenotype or family history is highly specific for a disease with a single "
            "genetic etiology.",
            not_derivable_reason="Requires phenotype input (HPO terms); out of scope for the variant pipeline.",
        ),
        _spec(
            "PP5",
            Direction.PATHOGENIC,
            Strength.SUPPORTING,
            "Reputable source recently reports variant as pathogenic, but the evidence is not "
            "available to the laboratory to perform an independent evaluation.",
            evidence_types=(EvidenceDataType.CLINICAL_SIGNIFICANCE,),
            derivable=True,
            citation=f"{RICHARDS_2015} Deprecation: {CLINGEN_SVI}",
            notes=(
                "ClinGen SVI recommends PP5/BP6 NOT be used, because 'reputable source' is "
                "undefined and the criterion allows a database's opinion to substitute for "
                "evidence. NGS-Agent derives it only under the acmg-amp-2015 rule set, only from "
                "sub-expert-panel submissions, and always attaches the deprecation as a limitation."
            ),
        ),
        # -- Benign: stand-alone -------------------------------------------
        _spec(
            "BA1",
            Direction.BENIGN,
            Strength.STAND_ALONE,
            "Allele frequency is greater than 5% in Exome Sequencing Project, 1000 Genomes "
            "Project, or Exome Aggregation Consortium.",
            evidence_types=(EvidenceDataType.ALLELE_FREQUENCY,),
            derivable=True,
            notes=(
                "BA1 is stand-alone benign and overrides other criteria in the guideline text. "
                "NGS-Agent does NOT apply it silently when pathogenic criteria are also present: "
                "that combination is a data conflict and is escalated to human review."
            ),
        ),
        # -- Benign: strong -------------------------------------------------
        _spec(
            "BS1",
            Direction.BENIGN,
            Strength.STRONG,
            "Allele frequency is greater than expected for disorder.",
            evidence_types=(EvidenceDataType.ALLELE_FREQUENCY,),
            not_derivable_reason=(
                "Requires a disease-specific maximum credible allele frequency. No disease "
                "prevalence/penetrance configuration is supplied, so BS1 cannot be computed "
                "honestly."
            ),
        ),
        _spec(
            "BS2",
            Direction.BENIGN,
            Strength.STRONG,
            "Observed in a healthy adult individual for a recessive (homozygous), dominant "
            "(heterozygous), or X-linked (hemizygous) disorder, with full penetrance expected at "
            "an early age.",
            not_derivable_reason="Requires observational cohort data with phenotype and age; no source configured.",
        ),
        _spec(
            "BS3",
            Direction.BENIGN,
            Strength.STRONG,
            "Well-established in vitro or in vivo functional studies show no damaging effect on "
            "protein function or splicing.",
            evidence_types=(EvidenceDataType.FUNCTIONAL_ASSAY,),
            not_derivable_reason="Requires a curated functional-assay database with validation status.",
        ),
        _spec(
            "BS4",
            Direction.BENIGN,
            Strength.STRONG,
            "Lack of segregation in affected members of a family.",
            evidence_types=(EvidenceDataType.SEGREGATION,),
            not_derivable_reason="Segregation data is family-specific; no source configured.",
        ),
        # -- Benign: supporting --------------------------------------------
        _spec(
            "BP1",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "Missense variant in a gene for which primarily truncating variants are known to "
            "cause disease.",
            evidence_types=(
                EvidenceDataType.MOLECULAR_CONSEQUENCE,
                EvidenceDataType.GENE_DISEASE_MECHANISM,
            ),
            not_derivable_reason=(
                "Requires knowing that truncating variants predominate in a gene, which is a "
                "gene-level statistical claim the mechanism seed table does not make."
            ),
        ),
        _spec(
            "BP2",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "Observed in trans with a pathogenic variant for a fully penetrant dominant "
            "gene/disorder, or observed in cis with a pathogenic variant in any inheritance pattern.",
            not_derivable_reason="Requires phasing relative to a second classified variant.",
        ),
        _spec(
            "BP3",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "In-frame deletions/insertions in a repetitive region without a known function.",
            not_derivable_reason="Requires repeat-region and functional-domain annotation.",
        ),
        _spec(
            "BP4",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "Multiple lines of computational evidence suggest no impact on gene or gene product "
            "(conservation, evolutionary, splicing impact, etc.).",
            evidence_types=(
                EvidenceDataType.SPLICING_PREDICTION,
                EvidenceDataType.MISSENSE_PREDICTION,
            ),
            not_derivable_reason=(
                "Requires calibrated in silico predictors with published thresholds. No predictor "
                "source configured. Note that BP4 must never be inferred from the absence of a "
                "deleterious prediction."
            ),
        ),
        _spec(
            "BP5",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "Variant found in a case with an alternate molecular basis for disease.",
            not_derivable_reason="Requires case-level molecular findings; out of scope.",
        ),
        _spec(
            "BP6",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "Reputable source recently reports variant as benign, but the evidence is not "
            "available to the laboratory to perform an independent evaluation.",
            evidence_types=(EvidenceDataType.CLINICAL_SIGNIFICANCE,),
            derivable=True,
            citation=f"{RICHARDS_2015} Deprecation: {CLINGEN_SVI}",
            notes="See PP5. Deprecated by ClinGen SVI; derived only under the acmg-amp-2015 rule set.",
        ),
        _spec(
            "BP7",
            Direction.BENIGN,
            Strength.SUPPORTING,
            "A synonymous (silent) variant for which splicing prediction algorithms predict no "
            "impact to the splice consensus sequence nor the creation of a new splice site AND "
            "the nucleotide is not highly conserved.",
            evidence_types=(
                EvidenceDataType.MOLECULAR_CONSEQUENCE,
                EvidenceDataType.SPLICING_PREDICTION,
                EvidenceDataType.SEQUENCE_CONSTRAINT,
            ),
            not_derivable_reason=(
                "Requires a splicing predictor that predicts NO impact plus conservation data. "
                "Absence of a splice prediction is not a prediction of no splice impact, so BP7 "
                "cannot be derived from missing evidence."
            ),
        ),
    )
}

PATHOGENIC_CODES = tuple(
    code for code, spec in CRITERIA.items() if spec.direction is Direction.PATHOGENIC
)
BENIGN_CODES = tuple(
    code for code, spec in CRITERIA.items() if spec.direction is Direction.BENIGN
)


def get_criterion(code: str) -> CriterionSpec:
    """Look up a criterion by code. Raises rather than returning ``None``.

    The legacy engine silently dropped codes it did not recognize — including
    real ACMG codes such as ``PP4`` and ``PM3`` — which meant an asserted
    criterion could vanish from a classification without a trace. Unknown codes
    are now a hard error.
    """
    normalized = code.strip().upper()
    spec = CRITERIA.get(normalized)
    if spec is None:
        raise RuleSetError(
            f"{code!r} is not one of the 28 ACMG/AMP 2015 criteria. Known codes: "
            f"{', '.join(sorted(CRITERIA))}. Unknown criteria are never silently ignored."
        )
    return spec


def strength_for(code: str) -> Strength:
    return get_criterion(code).default_strength
