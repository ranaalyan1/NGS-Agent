"""Validation assay designer — closes the VUS-stays-VUS-forever loop.

In 2026 every VUS verdict ends with "consider functional studies" — and
almost no one actually orders the functional study. The VUS stays a VUS
forever. By 2030 this loop is closed by automated assay ordering via
cloud lab APIs (Strateos, Emerald, Arctoris).

This tool designs a structured wet-lab validation assay plan for a VUS.
In 2026, the output is a recommendation the clinician orders manually.
In 2030+, this becomes a direct API call to a cloud lab.

Assay types (mapped to variant consequences):
  - Splicing minigene assay — for splice-region variants
  - CRISPR knockin + cell phenotype — for variants needing patient-line context
  - Overexpression + reporter — for variants in coding regions
  - Allele-specific expression (ASE) — for regulatory variants
  - Protein stability assay (cycloheximide chase) — for missense in stable proteins
  - Yeast complementation — for LOF variants in conserved genes
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class AssayPlan:
    """A structured wet-lab validation plan."""

    assay_type: str
    rationale: str
    cell_line: str
    controls_positive: list[str]
    controls_negative: list[str]
    expected_pathogenic_readout: str
    expected_benign_readout: str
    duration_days: int
    estimated_cost_usd: int
    protocol_reference: str  # PMID or methods paper
    cloud_lab_compatible: bool = False
    limitations: list[str] = field(default_factory=list)


# Mapping from variant consequence → recommended assay
ASSAY_DECISION_TREE: dict[str, list[AssayPlan]] = {
    "splice_donor_variant": [
        AssayPlan(
            assay_type="minigene splicing assay",
            rationale="Splice donor variants can be validated by cloning the variant into a minigene construct and comparing splicing patterns (exon skipping, intron retention) vs wild-type.",
            cell_line="HEK293T (transient transfection)",
            controls_positive=["Known pathogenic splice variant in same gene"],
            controls_negative=["Wild-type construct"],
            expected_pathogenic_readout="Exon skipping or intron retention on RT-PCR gel",
            expected_benign_readout="Same splicing pattern as wild-type",
            duration_days=14,
            estimated_cost_usd=2500,
            protocol_reference="PMID:21378988",
            cloud_lab_compatible=True,
            limitations=["Minigene context may not match endogenous chromatin"],
        ),
    ],
    "splice_acceptor_variant": [
        # Same as donor
    ],
    "missense_variant": [
        AssayPlan(
            assay_type="overexpression + functional assay",
            rationale="Missense variants are validated by overexpressing wild-type vs variant protein and measuring function (enzyme activity, binding, stability).",
            cell_line="HEK293T or cell line endogenously expressing the gene",
            controls_positive=["Known pathogenic missense in same domain"],
            controls_negative=["Wild-type", "Known benign missense"],
            expected_pathogenic_readout="Loss of function (>50% reduction in activity)",
            expected_benign_readout="Activity within 20% of wild-type",
            duration_days=21,
            estimated_cost_usd=4500,
            protocol_reference="PMID:23788649 (Findlay 2018 — saturation genome editing)",
            cloud_lab_compatible=True,
            limitations=["Overexpression may mask dominant-negative effects", "Requires functional assay to exist for the gene"],
        ),
        AssayPlan(
            assay_type="protein stability assay (cycloheximide chase)",
            rationale="Missense variants that destabilize the protein can be detected by inhibiting translation and measuring protein half-life.",
            cell_line="HEK293T (transient transfection)",
            controls_positive=["Known destabilizing variant"],
            controls_negative=["Wild-type"],
            expected_pathogenic_readout="Reduced protein half-life (<50% of WT)",
            expected_benign_readout="Half-life within 20% of wild-type",
            duration_days=10,
            estimated_cost_usd=1800,
            protocol_reference="PMID:27429005",
            cloud_lab_compatible=True,
        ),
    ],
    "nonsense": [
        AssayPlan(
            assay_type="NMD assessment + allele-specific expression",
            rationale="Nonsense variants often trigger nonsense-mediated decay (NMD). Validate by measuring allele-specific expression from patient RNA.",
            cell_line="Patient-derived lymphoblastoids or iPSCs",
            controls_positive=["Known NMD-triggering variant"],
            controls_negative=["Wild-type allele in same patient (heterozygote)"],
            expected_pathogenic_readout=">70% reduction in mutant allele expression vs wild-type",
            expected_benign_readout="Mutant allele expressed at similar level to wild-type (NMD escape)",
            duration_days=14,
            estimated_cost_usd=3200,
            protocol_reference="PMID:25525159",
            cloud_lab_compatible=False,  # requires patient RNA
            limitations=["Requires patient RNA sample", "NMD escape in last exon variants not detected"],
        ),
    ],
    "frameshift_variant": [
        # Same as nonsense
    ],
    "synonymous_variant": [
        AssayPlan(
            assay_type="minigene splicing assay + codon usage analysis",
            rationale="Synonymous variants can affect splicing (exonic splicing enhancers) or mRNA stability (codon usage). Validate with minigene + RNA-seq.",
            cell_line="HEK293T",
            controls_positive=["Known splicing-affecting synonymous variant"],
            controls_negative=["Wild-type"],
            expected_pathogenic_readout="Splicing change or altered mRNA half-life",
            expected_benign_readout="No splicing change",
            duration_days=14,
            estimated_cost_usd=2500,
            protocol_reference="PMID:22101970",
            cloud_lab_compatible=True,
        ),
    ],
}


def design_assays(consequence: str | None, gene: str | None = None) -> list[AssayPlan]:
    """Design validation assays based on variant consequence."""
    if not consequence:
        return []
    c = consequence.lower()
    # Find matching assay
    for key, plans in ASSAY_DECISION_TREE.items():
        if key in c and plans:
            return plans
    return []


def _format(plans: list[AssayPlan], gene: str | None) -> str:
    if not plans:
        return (
            "No wet-lab validation assay recommended — the variant consequence "
            "doesn't have a standard functional readout. Consider waiting for "
            "literature evidence (pubmed_search) or co-segregation data instead."
        )
    out = [f"# Validation assay plan{' for ' + gene if gene else ''}\n"]
    out.append(f"Recommended {len(plans)} assay(s):\n")
    for i, p in enumerate(plans, 1):
        out.append(f"## Assay {i}: {p.assay_type}")
        out.append(f"  Rationale: {p.rationale}")
        out.append(f"  Cell line: {p.cell_line}")
        out.append(f"  Positive controls: {', '.join(p.controls_positive)}")
        out.append(f"  Negative controls: {', '.join(p.controls_negative)}")
        out.append(f"  Expected pathogenic readout: {p.expected_pathogenic_readout}")
        out.append(f"  Expected benign readout: {p.expected_benign_readout}")
        out.append(f"  Duration: {p.duration_days} days")
        out.append(f"  Estimated cost: ${p.estimated_cost_usd:,}")
        out.append(f"  Protocol reference: {p.protocol_reference}")
        out.append(f"  Cloud-lab compatible: {'Yes (Strateos/Emerald)' if p.cloud_lab_compatible else 'No (requires patient samples)'}")
        if p.limitations:
            out.append(f"  Limitations: {', '.join(p.limitations)}")
        out.append("")
    return "\n".join(out)


class DesignValidationAssayTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="design_validation_assay",
            description=(
                "Design a wet-lab validation assay plan for a VUS. Outputs a "
                "structured experiment plan: assay type, cell line, controls, "
                "expected readouts, duration, cost, protocol reference. In 2026 "
                "this is a recommendation the lab orders manually; in 2030+ this "
                "becomes a direct API call to Strateos/Emerald cloud labs. Use "
                "this AFTER emit_verdict when the verdict is VUS — closes the "
                "'VUS stays VUS forever' loop."
            ),
            parameters={
                "gene": {"type": "string"},
                "consequence": {"type": "string", "description": "VEP consequence"},
                "hgvs_c": {"type": "string"},
                "verdict_id": {"type": "string", "description": "Link back to the verdict"},
            },
            required=["gene", "consequence"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        plans = design_assays(params.get("consequence"), params.get("gene"))
        content = _format(plans, params.get("gene"))
        return ToolResponse(
            content=content,
            metadata={
                "verdict_id": params.get("verdict_id"),
                "gene": params.get("gene"),
                "consequence": params.get("consequence"),
                "assay_count": len(plans),
                "assays": [
                    {
                        "assay_type": p.assay_type,
                        "cell_line": p.cell_line,
                        "duration_days": p.duration_days,
                        "estimated_cost_usd": p.estimated_cost_usd,
                        "cloud_lab_compatible": p.cloud_lab_compatible,
                        "protocol_reference": p.protocol_reference,
                    }
                    for p in plans
                ],
            },
        )
