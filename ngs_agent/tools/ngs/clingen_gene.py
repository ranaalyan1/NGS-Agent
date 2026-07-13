"""ClinGen gene-level table — haploinsufficiency (HI), triplosensitivity (TS),
missense constraint Z-score, and known LOF disease mechanism.

Sources:
  - ClinGen Gene-Disease Validity curation (https://search.clinicalgenome.org)
  - gnomAD v4 constraint metrics
  - ClinGen HI/TS dosage sensitivity map

These are baked-in for common clinical genes. For full coverage, replace the
GENE_TABLE dict with a load from ClinGen's official JSON dumps.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse
from ...runtime.evidence_graph import EvidenceEdge, EvidenceNode, gene_node_id


@dataclass
class GeneInfo:
    gene: str
    # Dosage sensitivity
    haploinsufficiency_score: int   # 0 (no evidence) | 1 (little) | 2 (sufficient) | 3 (some) | 30 (gene-associated)
    triplosensitivity_score: int   # same scale
    # Constraint (gnomAD v4)
    missense_z: float | None = None       # >3.09 = constrained
    lof_pLI: float | None = None          # >0.9 = LOF-intolerant
    lof_oe: float | None = None           # <0.35 = LOF-constrained
    # Mechanism
    has_lof_disease_mechanism: bool = False
    has_gof_disease_mechanism: bool = False
    has_known_pathogenic_missense: bool = False
    # Inheritance pattern
    inheritance: str = ""  # "AD" | "AR" | "XL" | "mitochondrial"
    # Associated diseases
    diseases: list[str] = None

    def is_haploinsufficient(self) -> bool:
        """Use the ClinGen HI score to determine if PVS1 LOF mechanism is established."""
        return self.haploinsufficiency_score >= 2 and self.has_lof_disease_mechanism

    def is_missense_constrained(self) -> bool:
        """For PP2: gene has low rate of benign missense → missense in this gene
        more likely pathogenic. Z > 3.09 = constrained."""
        return self.missense_z is not None and self.missense_z > 3.09

    def is_lof_intolerant(self) -> bool:
        """pLI > 0.9 OR LOF oe < 0.35 → gene is LOF-intolerant."""
        if self.lof_pLI is not None and self.lof_pLI > 0.9:
            return True
        if self.lof_oe is not None and self.lof_oe < 0.35:
            return True
        return False


# ---------- Curated gene table (common clinical genes) ----------
GENE_TABLE: dict[str, GeneInfo] = {
    "BRCA1": GeneInfo(
        gene="BRCA1",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.86,
        lof_pLI=0.99,
        lof_oe=0.16,
        has_lof_disease_mechanism=True,
        has_known_pathogenic_missense=True,
        inheritance="AD",
        diseases=["Hereditary breast and ovarian cancer"],
    ),
    "BRCA2": GeneInfo(
        gene="BRCA2",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=1.61,
        lof_pLI=1.0,
        lof_oe=0.11,
        has_lof_disease_mechanism=True,
        has_known_pathogenic_missense=True,
        inheritance="AD",
        diseases=["Hereditary breast and ovarian cancer"],
    ),
    "TP53": GeneInfo(
        gene="TP53",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.78,
        lof_pLI=0.99,
        lof_oe=0.17,
        has_lof_disease_mechanism=True,
        has_gof_disease_mechanism=True,
        has_known_pathogenic_missense=True,
        inheritance="AD",
        diseases=["Li-Fraumeni syndrome"],
    ),
    "MLH1": GeneInfo(
        gene="MLH1",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.32,
        lof_pLI=0.99,
        lof_oe=0.18,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Lynch syndrome"],
    ),
    "MSH2": GeneInfo(
        gene="MSH2",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.36,
        lof_pLI=0.99,
        lof_oe=0.19,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Lynch syndrome"],
    ),
    "MSH6": GeneInfo(
        gene="MSH6",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.04,
        lof_pLI=1.0,
        lof_oe=0.19,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Lynch syndrome"],
    ),
    "APC": GeneInfo(
        gene="APC",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=-0.31,
        lof_pLI=0.99,
        lof_oe=0.07,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Familial adenomatous polyposis"],
    ),
    "PTEN": GeneInfo(
        gene="PTEN",
        haploinsufficiency_score=3,
        triplosensitivity_score=1,
        missense_z=4.31,
        lof_pLI=1.0,
        lof_oe=0.11,
        has_lof_disease_mechanism=True,
        has_gof_disease_mechanism=True,
        has_known_pathogenic_missense=True,
        inheritance="AD",
        diseases=["PTEN hamartoma tumor syndrome", "Cowden syndrome"],
    ),
    "RB1": GeneInfo(
        gene="RB1",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.10,
        lof_pLI=1.0,
        lof_oe=0.08,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Retinoblastoma", "Hereditary retinoblastoma"],
    ),
    "STK11": GeneInfo(
        gene="STK11",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=2.05,
        lof_pLI=0.95,
        lof_oe=0.16,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Peutz-Jeghers syndrome"],
    ),
    "PALB2": GeneInfo(
        gene="PALB2",
        haploinsufficiency_score=3,
        triplosensitivity_score=0,
        missense_z=1.31,
        lof_pLI=0.99,
        lof_oe=0.13,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Familial breast cancer"],
    ),
    "CHEK2": GeneInfo(
        gene="CHEK2",
        haploinsufficiency_score=2,
        triplosensitivity_score=0,
        missense_z=1.61,
        lof_pLI=0.45,
        lof_oe=0.51,
        has_lof_disease_mechanism=True,
        inheritance="AD",
        diseases=["Hereditary breast cancer"],
    ),
}


def get_gene_info(gene: str) -> GeneInfo | None:
    return GENE_TABLE.get(gene.upper())


def _format(info: GeneInfo) -> str:
    out = [f"# ClinGen gene info — {info.gene}\n"]
    out.append(f"  Haploinsufficiency score: {info.haploinsufficiency_score} "
               f"({'sufficient evidence' if info.haploinsufficiency_score >= 3 else 'see ClinGen'})")
    out.append(f"  Triplosensitivity score:  {info.triplosensitivity_score}")
    out.append(f"  Missense Z-score:         {info.missense_z} "
               f"({'CONSTRAINED (>3.09)' if info.is_missense_constrained() else ''})")
    out.append(f"  LOF pLI:                  {info.lof_pLI}")
    out.append(f"  LOF oe:                   {info.lof_oe}")
    out.append(f"  LOF-intolerant:           {info.is_lof_intolerant()}")
    out.append(f"  LOF disease mechanism:    {info.has_lof_disease_mechanism}")
    out.append(f"  GoF disease mechanism:    {info.has_gof_disease_mechanism}")
    out.append(f"  Has known pathogenic missense: {info.has_known_pathogenic_missense}")
    out.append(f"  Inheritance:              {info.inheritance}")
    if info.diseases:
        out.append(f"  Diseases:                 {', '.join(info.diseases)}")

    out.append("\n## ACMG implications")
    if info.is_haploinsufficient():
        out.append("  - PVS1 applies to LOF variants (nonsense, frameshift, canonical splice, multi-exon del)")
    if info.is_missense_constrained():
        out.append("  - PP2 applies to missense variants in this gene")
    if info.has_known_pathogenic_missense:
        out.append("  - PM5 may apply: novel missense at residue where a different pathogenic missense was seen")
    if not info.has_lof_disease_mechanism:
        out.append("  - PVS1 does NOT apply: LOF is not an established disease mechanism for this gene")
    return "\n".join(out)


class ClinGenGeneTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="clingen_gene",
            description=(
                "Look up ClinGen-curated gene-level info: haploinsufficiency (HI) "
                "and triplosensitivity (TS) scores, gnomAD missense Z-score, pLI, "
                "LOF oe, and known disease mechanisms. Use this BEFORE "
                "acmg_classify to determine if PVS1 applies (HI score >= 2 + LOF "
                "mechanism), if PP2 applies (missense Z > 3.09), and to inform "
                "PP4 (phenotype match). Built-in curated table covers BRCA1, "
                "BRCA2, TP53, MLH1, MSH2, MSH6, APC, PTEN, RB1, STK11, PALB2, "
                "CHEK2."
            ),
            parameters={
                "gene": {"type": "string"},
            },
            required=["gene"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        info = get_gene_info(params["gene"])
        if not info:
            return ToolResponse(
                content=(
                    f"Gene {params['gene']} not in built-in ClinGen table. "
                    "Available: " + ", ".join(sorted(GENE_TABLE.keys()))
                ),
                metadata={"status": "not_in_table", "available_genes": list(GENE_TABLE.keys())},
            )

        # v0.5: populate evidence graph
        if ctx.evidence_graph is not None:
            gid = gene_node_id(info.gene)
            ctx.evidence_graph.add_node(EvidenceNode(
                id=gid, kind="gene", label=info.gene,
                properties={
                    "haploinsufficiency_score": info.haploinsufficiency_score,
                    "missense_z": info.missense_z,
                    "inheritance": info.inheritance,
                },
            ))
            ctx.evidence_graph.add_edge(
                gid,
                f"gene_disease_validity:{gid}",
                EvidenceEdge(
                    source="clingen",
                    weight=0.9 if info.is_haploinsufficient() else 0.5,
                    citation=f"ClinGen gene curation: {info.gene}",
                    properties={
                        "haploinsufficient": info.is_haploinsufficient(),
                        "missense_constrained": info.is_missense_constrained(),
                        "lof_intolerant": info.is_lof_intolerant(),
                        "inheritance": info.inheritance,
                        "diseases": info.diseases,
                    },
                ),
            )

        return ToolResponse(
            content=_format(info),
            metadata={
                "gene": info.gene,
                "haploinsufficiency_score": info.haploinsufficiency_score,
                "triplosensitivity_score": info.triplosensitivity_score,
                "missense_z": info.missense_z,
                "lof_pLI": info.lof_pLI,
                "lof_oe": info.lof_oe,
                "has_lof_disease_mechanism": info.has_lof_disease_mechanism,
                "has_known_pathogenic_missense": info.has_known_pathogenic_missense,
                "is_haploinsufficient": info.is_haploinsufficient(),
                "is_missense_constrained": info.is_missense_constrained(),
                "is_lof_intolerant": info.is_lof_intolerant(),
                "inheritance": info.inheritance,
                "diseases": info.diseases,
            },
        )
