"""Evidence graph query tool — replaces 5 point-query tools with one graph query.

The LLM calls this AFTER gnomad_query / clinvar_rcv / pubmed_search etc. have
populated the graph (those tools now write to the graph in addition to returning
their text response). The query tool returns a weighted summary:

  "For BRCA2 17:43091752 T>G, evidence summary:
     - gnomad: AF=2e-5 (rare, supports pathogenic)
     - clinvar: VUS (1 submitter, weight=0.3)
     - pubmed: 3 functional studies, 2 support pathogenic, 1 inconclusive
     - alphamissense: 0.71 (likely pathogenic)
     - spliceai: 0.02 (no impact)
     - clingen: BRCA2 is haploinsufficient (HI=3)
   Net pathogenicity score: +0.65 (Likely Pathogenic range)"
"""
from __future__ import annotations

from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse
from ...runtime.evidence_graph import (
    EvidenceGraph, EvidenceNode, EvidenceEdge,
    variant_node_id, gene_node_id,
)


class EvidenceGraphQueryTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="evidence_graph_query",
            description=(
                "Query the evidence graph for all gathered evidence about a "
                "variant or gene. Returns a weighted summary across ALL sources "
                "(gnomAD, ClinVar, PubMed, LitVar, ClinGen, AlphaMissense, "
                "SpliceAI) and a net pathogenicity score in [-1.0, +1.0]. "
                "CALL THIS after gathering evidence with the individual tools "
                "and BEFORE calling acmg_classify the second time. The graph "
                "is per-session — evidence only appears here after the source "
                "tools have been called."
            ),
            parameters={
                "chrom": {"type": "string", "description": "Leave empty to query by gene only"},
                "pos": {"type": "integer"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
                "gene": {"type": "string", "description": "Query by gene symbol"},
                "depth": {"type": "integer", "default": 1, "minimum": 1, "maximum": 3},
            },
            required=[],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        graph: EvidenceGraph | None = getattr(ctx, "evidence_graph", None)
        if graph is None:
            return ToolResponse(
                content="Evidence graph not available in this context.",
                is_error=True,
            )

        # Determine the query node
        node_id = None
        if params.get("chrom") and params.get("pos") and params.get("ref") and params.get("alt"):
            node_id = variant_node_id(
                params["chrom"], int(params["pos"]),
                params["ref"], params["alt"],
            )
        elif params.get("gene"):
            node_id = gene_node_id(params["gene"])
        else:
            return ToolResponse(
                content="Provide either chrom+pos+ref+alt or gene.",
                is_error=True,
            )

        depth = int(params.get("depth", 1))
        result = graph.query(node_id, depth=depth)

        if result["node"] is None:
            return ToolResponse(
                content=(
                    f"No evidence in graph for {node_id}. "
                    "Call gnomad_query, clinvar_rcv, pubmed_search, etc. first."
                ),
                metadata={"query": node_id, "found": False},
            )

        # Format
        out = [f"# Evidence graph query — {node_id}\n"]
        out.append(f"Depth: {depth} | Edges: {len(result['edges'])} | "
                   f"Sources: {len(result['aggregate'])}\n")

        if result["aggregate"]:
            out.append("## By source")
            for src, agg in sorted(result["aggregate"].items()):
                out.append(
                    f"  • {src}: {agg['count']} edge(s), "
                    f"avg_weight={agg['avg_weight']:.2f}, "
                    f"citations={agg['citations'][:3]}"
                )
            out.append("")

        if result["net_pathogenicity_score"] is not None:
            score = result["net_pathogenicity_score"]
            if score >= 0.5:
                interp = "Pathogenic range"
            elif score >= 0.2:
                interp = "Likely Pathogenic range"
            elif score > -0.2:
                interp = "VUS range"
            elif score > -0.5:
                interp = "Likely Benign range"
            else:
                interp = "Benign range"
            out.append(f"## Net pathogenicity score: {score:+.2f} ({interp})")

        out.append("\n## Edge details")
        for e in result["edges"][:20]:  # cap
            props_str = ", ".join(f"{k}={v}" for k, v in list(e["properties"].items())[:3])
            out.append(
                f"  [{e['source']}] {e['src']} → {e['dst']} "
                f"(w={e['weight']:.2f}, cite={e['citation'][:40]}) "
                f"{{{props_str}}}"
            )
        if len(result["edges"]) > 20:
            out.append(f"  ... and {len(result['edges']) - 20} more")

        return ToolResponse(
            content="\n".join(out),
            metadata={
                "query": node_id,
                "found": True,
                "edge_count": len(result["edges"]),
                "aggregate": result["aggregate"],
                "net_pathogenicity_score": result["net_pathogenicity_score"],
            },
        )
