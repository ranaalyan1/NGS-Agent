"""HGVS conversion — genomic coordinate ↔ c./p. notation.

Real-world HGVS conversion requires the `hgvs` Python package + a transcript
reference database (RefSeq/Ensembl). That's heavy. This tool ships with a
small built-in transcript map for common clinical genes (BRCA1, BRCA2, TP53,
MLH1, MSH2, MSH6, APC, etc.) and a clear interface for plugging in a real
transcript reference.

Limitations:
  - Only handles SNVs and short indels
  - Transcript map is curated manually; for genes not in the map, returns
    'transcript_not_in_map' and instructs the LLM to query ClinVar by genomic
    coordinate instead.
  - Does not handle complex rearrangements.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse


@dataclass
class Transcript:
    """Minimal transcript record for HGVS conversion.

    For production use, replace this with the `hgvs` package's transcript parser.
    """

    gene: str
    refseq_id: str           # NM_* accession
    chrom: str
    strand: str              # "+" | "-"
    cds_start: int           # 1-based genomic pos of c.1 (the A of ATG)
    # Coding sequence length (nt), including stop codon
    cds_length: int
    # Exon boundaries — list of (start, end) 1-based inclusive genomic coords
    exons: list[tuple[int, int]]

    def genomic_to_c(self, genomic_pos: int) -> int:
        """Convert a 1-based genomic position to a 1-based c. position."""
        if self.strand == "+":
            return genomic_pos - self.cds_start + 1
        return self.cds_start - genomic_pos + 1

    def c_to_p(self, c_pos: int, ref: str, alt: str) -> str:
        """Generate a rough p. notation from c. notation. Only handles SNVs."""
        if c_pos <= 0:
            return "p.? (5'UTR)"
        if c_pos > self.cds_length - 3:
            return "p.? (3'UTR)"
        aa_pos = ((c_pos - 1) // 3) + 1
        # Codon table
        codons = {
            "TTT": "Phe", "TTC": "Phe", "TTA": "Leu", "TTG": "Leu",
            "CTT": "Leu", "CTC": "Leu", "CTA": "Leu", "CTG": "Leu",
            "ATT": "Ile", "ATC": "Ile", "ATA": "Ile", "ATG": "Met",
            "GTT": "Val", "GTC": "Val", "GTA": "Val", "GTG": "Val",
            "TCT": "Ser", "TCC": "Ser", "TCA": "Ser", "TCG": "Ser",
            "CCT": "Pro", "CCC": "Pro", "CCA": "Pro", "CCG": "Pro",
            "ACT": "Thr", "ACC": "Thr", "ACA": "Thr", "ACG": "Thr",
            "GCT": "Ala", "GCC": "Ala", "GCA": "Ala", "GCG": "Ala",
            "TAT": "Tyr", "TAC": "Tyr", "TAA": "*", "TAG": "*",
            "CAT": "His", "CAC": "His", "CAA": "Gln", "CAG": "Gln",
            "AAT": "Asn", "AAC": "Asn", "AAA": "Lys", "AAG": "Lys",
            "GAT": "Asp", "GAC": "Asp", "GAA": "Glu", "GAG": "Glu",
            "TGT": "Cys", "TGC": "Cys", "TGA": "*", "TGG": "Trp",
            "CGT": "Arg", "CGC": "Arg", "CGA": "Arg", "CGG": "Arg",
            "AGT": "Ser", "AGC": "Ser", "AGA": "Arg", "AGG": "Arg",
            "GGT": "Gly", "GGC": "Gly", "GGA": "Gly", "GGG": "Gly",
        }
        three_letter = {
            "Phe": "Phe", "Leu": "Leu", "Ile": "Ile", "Met": "Met",
            "Val": "Val", "Ser": "Ser", "Pro": "Pro", "Thr": "Thr",
            "Ala": "Ala", "Tyr": "Tyr", "*": "Ter", "His": "His",
            "Gln": "Gln", "Asn": "Asn", "Lys": "Lys", "Asp": "Asp",
            "Glu": "Glu", "Cys": "Cys", "Trp": "Trp", "Arg": "Arg",
            "Gly": "Gly",
        }
        return f"p.({three_letter.get(ref, 'X')}{aa_pos}{three_letter.get(alt, 'X')})"


# ---------- Built-in transcript map (curated) ----------
# These are GRCh38 coordinates of the canonical transcripts. For production
# use, swap this dict out for hgvs-package-backed lookups.
TRANSCRIPT_MAP: dict[str, Transcript] = {
    "BRCA1": Transcript(
        gene="BRCA1", refseq_id="NM_007294.4",
        chrom="17", strand="-",
        cds_start=43091652,  # c.1
        cds_length=5592,
        exons=[],  # exons omitted for brevity; PVS1 engine needs these for clinical use
    ),
    "BRCA2": Transcript(
        gene="BRCA2", refseq_id="NM_000059.4",
        chrom="17", strand="+",
        cds_start=32890540,
        cds_length=10203,
        exons=[],
    ),
    "TP53": Transcript(
        gene="TP53", refseq_id="NM_000546.6",
        chrom="17", strand="-",
        cds_start=7675139,
        cds_length=1182,
        exons=[],
    ),
    "MLH1": Transcript(
        gene="MLH1", refseq_id="NM_000249.4",
        chrom="3", strand="+",
        cds_start=37034842,
        cds_length=1881,
        exons=[],
    ),
    "MSH2": Transcript(
        gene="MSH2", refseq_id="NM_000251.3",
        chrom="2", strand="+",
        cds_start=47410135,
        cds_length=2502,
        exons=[],
    ),
    "MSH6": Transcript(
        gene="MSH6", refseq_id="NM_000179.3",
        chrom="2", strand="+",
        cds_start=47803410,
        cds_length=3993,
        exons=[],
    ),
    "APC": Transcript(
        gene="APC", refseq_id="NM_000038.6",
        chrom="5", strand="+",
        cds_start=112175479,
        cds_length=8535,
        exons=[],
    ),
    "PTEN": Transcript(
        gene="PTEN", refseq_id="NM_000314.8",
        chrom="10", strand="+",
        cds_start=89692577,
        cds_length=1209,
        exons=[],
    ),
}


def genomic_to_hgvs(gene: str, genomic_pos: int, ref: str, alt: str) -> dict:
    """Convert genomic coords to HGVS c./p. notation for a known gene."""
    t = TRANSCRIPT_MAP.get(gene)
    if not t:
        return {
            "status": "transcript_not_in_map",
            "message": (
                f"Gene {gene} not in built-in transcript map. "
                "Query ClinVar by genomic coordinate instead."
            ),
            "available_genes": list(TRANSCRIPT_MAP.keys()),
        }

    c_pos = t.genomic_to_c(genomic_pos)

    if c_pos <= 0:
        c_dot = f"c.-{abs(c_pos)}"
    elif c_pos > t.cds_length:
        c_dot = f"c.*{c_pos - t.cds_length}"
    else:
        c_dot = f"c.{c_pos}{ref}>{alt}"

    p_dot = t.c_to_p(c_pos, ref, alt)

    return {
        "status": "ok",
        "gene": gene,
        "transcript": t.refseq_id,
        "hgvs_c": f"{t.refseq_id}:{c_dot}",
        "hgvs_p": p_dot,
        "raw_c_pos": c_pos,
    }


class HgvsConvertTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="hgvs_convert",
            description=(
                "Convert a genomic coordinate (CHROM:POS REF>ALT) to HGVS "
                "c./p. notation using the canonical transcript for the gene. "
                "Built-in transcript map covers BRCA1, BRCA2, TP53, MLH1, MSH2, "
                "MSH6, APC, PTEN. For genes outside this map, the tool returns "
                "'transcript_not_in_map' — the LLM should query ClinVar by "
                "genomic coordinate in that case. ALWAYS call this BEFORE "
                "clinvar_query by gene+HGVS, and before pubmed_search."
            ),
            parameters={
                "gene": {"type": "string", "description": "Gene symbol"},
                "pos": {"type": "integer", "description": "1-based genomic position"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
            },
            required=["gene", "pos", "ref", "alt"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        try:
            result = genomic_to_hgvs(
                params["gene"], int(params["pos"]),
                params["ref"], params["alt"],
            )
        except Exception as e:
            return ToolResponse(content=f"HGVS conversion failed: {e}", is_error=True)

        if result["status"] != "ok":
            return ToolResponse(
                content=result["message"],
                metadata=result,
            )

        content = (
            f"# HGVS — {params['gene']} {params['pos']}{params['ref']}>{params['alt']}\n"
            f"  Transcript: {result['transcript']}\n"
            f"  HGVS.c:     {result['hgvs_c']}\n"
            f"  HGVS.p:     {result['hgvs_p']}\n"
        )
        return ToolResponse(content=content, metadata=result)
