"""ACMG/AMP 2015 evidence calculator — deterministic rules engine.

v0.4: now delegates PVS1 to the full ClinGen SVI decision tree (pvs1_engine),
looks up ClinGen gene-level HI/TS/constraint automatically, and flags PP5 as
deprecated per the 2023 ClinGen SVI revision (still computes it but warns).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse
from .clingen_gene import get_gene_info
from .pvs1_engine import PVS1Input, PVS1Strength, TranscriptInfo, classify_pvs1

# ---------- Bayesian combining weights (Tavtigian 2018) ----------
WEIGHTS: dict[str, float] = {
    # Pathogenic — Very Strong
    "PVS1":  8.0,
    # PVS1 downgrade weights (ClinGen SVI 2018)
    "PVS1_Strong": 4.0, "PVS1_Moderate": 2.0, "PVS1_Supporting": 1.0,
    # Pathogenic — Strong
    "PS1":   4.0, "PS2": 4.0, "PS3": 4.0, "PS4": 4.0,
    # Pathogenic — Moderate
    "PM1":   2.0, "PM2": 2.0, "PM3": 2.0, "PM4": 2.0,
    "PM5":   2.0, "PM6": 2.0,
    # Pathogenic — Supporting
    "PP1":   1.0, "PP2": 1.0, "PP3": 1.0, "PP4": 1.0, "PP5": 1.0,
    # Benign — Stand-alone
    "BA1":  -8.0,
    # Benign — Strong
    "BS1":  -4.0, "BS2": -4.0, "BS3": -4.0, "BS4": -4.0,
    # Benign — Supporting
    "BP1":  -1.0, "BP2": -1.0, "BP3": -1.0, "BP4": -1.0, "BP5": -1.0,
    "BP6":  -1.0, "BP7": -1.0,
}

DEPRECATED_CRITERIA = {"PP5", "BP6"}


@dataclass
class VariantInput:
    gene: str
    chrom: str
    pos: int
    ref: str
    alt: str
    consequence: str | None = None
    hgvs_p: str | None = None
    hgvs_c: str | None = None
    rsid: str | None = None
    gnomad_af: float | None = None
    gnomad_popmax_af: float | None = None
    faf95: float | None = None
    clinvar: str | None = None
    clinvar_review_status: str | None = None
    spliceai_score: float | None = None
    alphamissense_score: float | None = None
    reveal_score: float | None = None
    gene_haploinsufficient: bool = False
    gene_known_lof_disease: bool = False
    gene_missense_constrained: bool = False
    has_known_pathogenic_missense: bool = False
    nmd_predicted: bool = True
    last_exon: bool = False
    last_50nt_penultimate: bool = False
    segregation_families: int = 0
    case_control_pvalue: float | None = None
    de_novo_confirmed: bool = False
    de_novo_assumed: bool = False


@dataclass
class AcmgResult:
    applied: dict[str, str] = field(default_factory=dict)
    deprecated_used: list[str] = field(default_factory=list)
    score: float = 0.0
    classification: str = "VUS"
    combining_rule: str = ""

    def to_dict(self) -> dict:
        return {
            "applied": self.applied,
            "deprecated_used": self.deprecated_used,
            "score": round(self.score, 2),
            "classification": self.classification,
            "combining_rule": self.combining_rule,
        }


def _is_lof(consequence: str | None) -> bool:
    if not consequence:
        return False
    c = consequence.lower()
    return any(k in c for k in ("stop_gained", "nonsense", "frameshift", "splice_donor", "splice_acceptor", "initiator_codon", "start_lost"))


def _is_missense(consequence: str | None) -> bool:
    if not consequence:
        return False
    return "missense" in consequence.lower()


def _is_synonymous(consequence: str | None) -> bool:
    if not consequence:
        return False
    return "synonymous" in consequence.lower()


def _variant_type_for_pvs1(consequence: str | None) -> str | None:
    if not consequence:
        return None
    c = consequence.lower()
    if "stop_gained" in c or "nonsense" in c:
        return "nonsense"
    if "frameshift" in c:
        return "frameshift"
    if "splice_donor" in c:
        return "splice_donor"
    if "splice_acceptor" in c:
        return "splice_acceptor"
    if "initiator_codon" in c or "start_lost" in c:
        return "initiation_codon"
    return None


def classify_acmg(v: VariantInput) -> AcmgResult:
    """Apply ACMG 2015 rules + Tavtigian 2018 Bayesian combining."""
    res = AcmgResult()

    # ---- Auto-lookup ClinGen gene info ----
    gene_info = get_gene_info(v.gene)
    if gene_info:
        if not v.gene_haploinsufficient:
            v.gene_haploinsufficient = gene_info.is_haploinsufficient()
        if not v.gene_known_lof_disease:
            v.gene_known_lof_disease = gene_info.has_lof_disease_mechanism
        if not v.gene_missense_constrained:
            v.gene_missense_constrained = gene_info.is_missense_constrained()
        if not v.has_known_pathogenic_missense:
            v.has_known_pathogenic_missense = gene_info.has_known_pathogenic_missense

    # ---- PVS1: full ClinGen SVI decision tree ----
    variant_type = _variant_type_for_pvs1(v.consequence)
    if variant_type and v.gene_haploinsufficient and v.gene_known_lof_disease:
        transcript = TranscriptInfo(
            gene=v.gene,
            has_known_lof_mechanism=True,
            nmd_predicted=v.nmd_predicted,
            last_50nt_of_penultimate_exon=v.last_50nt_penultimate,
            last_exon_number=999 if v.last_exon else 0,
            is_canonical_donor=variant_type == "splice_donor",
            is_canonical_acceptor=variant_type == "splice_acceptor",
        )
        pvs1_input = PVS1Input(variant_type=variant_type, transcript=transcript)
        strength = classify_pvs1(pvs1_input)
        if strength != PVS1Strength.NONE:
            code = strength.value
            res.applied[code] = (
                f"ClinGen SVI PVS1 decision tree applied: variant_type={variant_type}, "
                f"NMD_predicted={v.nmd_predicted}, last_exon={v.last_exon}."
            )

    # ---- PS2 / PM6: de novo ----
    if v.de_novo_confirmed:
        res.applied["PS2"] = (
            "De novo (paternity and maternity confirmed) in patient with the "
            "disease and no family history."
        )
    elif v.de_novo_assumed:
        res.applied["PM6"] = (
            "Assumed de novo (paternity and maternity NOT confirmed)."
        )

    # ---- PM2: absent from controls in gnomAD ----
    if v.gnomad_af is not None and v.gnomad_af == 0:
        res.applied["PM2"] = "Variant absent from gnomAD v4 (AF=0). Supporting pathogenic evidence."
    elif v.gnomad_af is not None and v.gnomad_af < 1e-5:
        res.applied["PM2"] = f"Extremely rare in gnomAD v4 (AF={v.gnomad_af:.2e}). Supporting pathogenic evidence."

    # ---- PP2: missense in gene with low rate of benign missense ----
    if _is_missense(v.consequence) and v.gene_missense_constrained:
        res.applied["PP2"] = (
            f"Missense variant in {v.gene} which has a high missense Z-score "
            "(constrained) — low rate of benign missense."
        )

    # ---- PP3: multiple in silico predictors agree on damaging ----
    damaging_predictors = 0
    pred_evidence = []
    if v.spliceai_score is not None and v.spliceai_score >= 0.5:
        damaging_predictors += 1
        pred_evidence.append(f"SpliceAI={v.spliceai_score:.2f}")
    if v.alphamissense_score is not None and v.alphamissense_score >= 0.564:
        damaging_predictors += 1
        pred_evidence.append(f"AlphaMissense={v.alphamissense_score:.2f}")
    if v.reveal_score is not None and v.reveal_score >= 0.7:
        damaging_predictors += 1
        pred_evidence.append(f"REVEL={v.reveal_score:.2f}")
    if damaging_predictors >= 2:
        res.applied["PP3"] = f"Multiple in silico predictors indicate damaging: {', '.join(pred_evidence)}."

    # ---- PP5: DEPRECATED in 2023 ClinGen SVI ----
    if v.clinvar and "pathogenic" in v.clinvar.lower():
        review = (v.clinvar_review_status or "").lower()
        if "multiple" in review or "reviewed by expert panel" in review or "practice guideline" in review:
            res.applied["PP5"] = (
                f"ClinVar classifies as pathogenic with strong review "
                f"({v.clinvar_review_status}). "
                f"⚠ PP5 is DEPRECATED per 2023 ClinGen SVI — use as informational only."
            )
            res.deprecated_used.append("PP5")
        elif "criteria provided" in review or "single submitter" in review:
            res.applied["PP5"] = (
                f"ClinVar classifies as pathogenic ({v.clinvar_review_status or 'single submitter'}). "
                f"⚠ PP5 is DEPRECATED per 2023 ClinGen SVI — use as informational only."
            )
            res.deprecated_used.append("PP5")

    # ---- BA1 / BS1 ----
    if v.gnomad_af is not None and v.gnomad_af >= 0.05:
        res.applied["BA1"] = f"Allele frequency in gnomAD >= 5% (AF={v.gnomad_af:.2%}). Stand-alone benign evidence."
    elif v.gnomad_af is not None and v.gnomad_af >= 0.01:
        res.applied["BS1"] = f"Allele frequency in gnomAD >= 1% (AF={v.gnomad_af:.2%}), greater than expected for the disease."

    # ---- BS2 ----
    if v.gnomad_popmax_af is not None and v.gnomad_popmax_af >= 0.005:
        res.applied["BS2"] = f"Popmax AF in gnomAD >= 0.5% ({v.gnomad_popmax_af:.2%}), observed in healthy adult controls."

    # ---- BP4 ----
    benign_predictors = 0
    benign_evidence = []
    if v.spliceai_score is not None and v.spliceai_score < 0.1:
        benign_predictors += 1
        benign_evidence.append(f"SpliceAI={v.spliceai_score:.2f}")
    if v.alphamissense_score is not None and v.alphamissense_score < 0.34:
        benign_predictors += 1
        benign_evidence.append(f"AlphaMissense={v.alphamissense_score:.2f}")
    if v.reveal_score is not None and v.reveal_score < 0.3:
        benign_predictors += 1
        benign_evidence.append(f"REVEL={v.reveal_score:.2f}")
    if benign_predictors >= 2 and _is_missense(v.consequence):
        res.applied["BP4"] = f"Multiple in silico predictors indicate benign: {', '.join(benign_evidence)}."

    # ---- BP7 ----
    if _is_synonymous(v.consequence):
        if v.spliceai_score is None or v.spliceai_score < 0.1:
            res.applied["BP7"] = "Synonymous variant with no predicted splice impact."

    # ---- Combine ----
    res.score = sum(WEIGHTS.get(code, 0) for code in res.applied if code in WEIGHTS)

    if res.score >= 6.0:
        res.classification = "Pathogenic"
        res.combining_rule = "Bayesian (Tavtigian 2018): score >= 6.0"
    elif res.score >= 2.0:
        res.classification = "Likely Pathogenic"
        res.combining_rule = "Bayesian (Tavtigian 2018): 2.0 <= score < 6.0"
    elif res.score <= -6.0:
        res.classification = "Benign"
        res.combining_rule = "Bayesian (Tavtigian 2018): score <= -6.0"
    elif res.score <= -2.0:
        res.classification = "Likely Benign"
        res.combining_rule = "Bayesian (Tavtigian 2018): -6.0 < score <= -2.0"
    else:
        res.classification = "VUS"
        res.combining_rule = "Bayesian (Tavtigian 2018): -2.0 < score < 2.0"

    return res


def _format(res: AcmgResult, v: VariantInput) -> str:
    out = [f"# ACMG/AMP 2015 classification — {v.gene} {v.chrom}:{v.pos}{v.ref}>{v.alt}"]
    out.append(f"Final: **{res.classification}** (Bayesian score = {res.score:.2f})")
    out.append(f"Combining rule: {res.combining_rule}\n")

    if res.deprecated_used:
        out.append("⚠  Deprecated criteria used (informational only): " + ", ".join(res.deprecated_used))
        out.append("   PP5/BP6 are deprecated per 2023 ClinGen SVI revision.\n")

    if not res.applied:
        out.append("No ACMG criteria triggered — variant remains VUS by default.")
        out.append("The LLM should gather evidence via gnomad_query, clinvar_query, pubmed_search")
        out.append("and re-run acmg_classify with updated inputs.")
        return "\n".join(out)

    out.append("## Applied criteria")
    for code, rationale in sorted(res.applied.items()):
        weight = WEIGHTS.get(code, 0)
        out.append(f"- **{code}** (weight={weight:+.1f}): {rationale}")

    out.append("\n## Not yet evaluated (LLM should investigate)")
    not_evaluated = [
        ("PS1", "Same amino acid change as known pathogenic variant — query ClinVar/HGMD"),
        ("PS3", "Well-established functional studies — query PubMed"),
        ("PS4", "Prevalence in affecteds significantly increased over controls"),
        ("PM1", "Located in a mutational hot spot / functional domain — query ClinGen"),
        ("PM3", "Recessive: detected in trans with a pathogenic variant (use trio_analysis)"),
        ("PM4", "Protein length change due to in-frame indel in non-repeat region"),
        ("PM5", "Novel missense at residue where different pathogenic missense seen"),
        ("PP1", "Co-segregation with disease in multiple affected family members"),
        ("PP4", "Patient phenotype highly specific for gene's disease"),
    ]
    for code, desc in not_evaluated:
        if code not in res.applied:
            out.append(f"- {code}: {desc}")

    return "\n".join(out)


class AcmgClassifyTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="acmg_classify",
            description=(
                "Apply ACMG/AMP 2015 evidence criteria to a variant and compute a "
                "Bayesian classification (Tavtigian 2018). v0.4: uses the full ClinGen "
                "SVI PVS1 decision tree (Mane 2018), auto-looks-up ClinGen gene HI/TS/"
                "constraint for PVS1/PP2, and flags PP5/BP6 as DEPRECATED per 2023 SVI. "
                "Deterministic — runs BEFORE LLM critique. ALWAYS call this first; the "
                "LLM then critiques by gathering additional evidence (PS1/PS3/PS4/PM5 "
                "require database lookups)."
            ),
            parameters={
                "gene": {"type": "string"},
                "chrom": {"type": "string"},
                "pos": {"type": "integer"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
                "consequence": {"type": "string"},
                "hgvs_p": {"type": "string"},
                "hgvs_c": {"type": "string"},
                "rsid": {"type": "string"},
                "gnomad_af": {"type": "number"},
                "gnomad_popmax_af": {"type": "number"},
                "faf95": {"type": "number"},
                "clinvar": {"type": "string"},
                "clinvar_review_status": {"type": "string"},
                "spliceai_score": {"type": "number"},
                "alphamissense_score": {"type": "number"},
                "reveal_score": {"type": "number"},
                "gene_haploinsufficient": {"type": "boolean"},
                "gene_known_lof_disease": {"type": "boolean"},
                "gene_missense_constrained": {"type": "boolean"},
                "has_known_pathogenic_missense": {"type": "boolean"},
                "nmd_predicted": {"type": "boolean"},
                "last_exon": {"type": "boolean"},
                "last_50nt_penultimate": {"type": "boolean"},
                "segregation_families": {"type": "integer"},
                "case_control_pvalue": {"type": "number"},
                "de_novo_confirmed": {"type": "boolean"},
                "de_novo_assumed": {"type": "boolean"},
            },
            required=["gene", "chrom", "pos", "ref", "alt"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        valid = {k: params[k] for k in params if k in VariantInput.__dataclass_fields__}
        try:
            v = VariantInput(**valid)
        except TypeError as e:
            return ToolResponse(content=f"Invalid params: {e}", is_error=True)

        result = classify_acmg(v)
        return ToolResponse(
            content=_format(result, v),
            metadata={"acmg": result.to_dict()},
        )
