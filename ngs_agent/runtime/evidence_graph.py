"""Evidence Graph — the universal substrate for variant interpretation.

In 2026 most NGS tools (including NGS-Agent v0.4) do point queries:
gnomad_query → clinvar_rcv → pubmed_search → ... — each a separate API call,
each returning a separate blob, no joint reasoning.

By 2030 this is replaced by an evidence graph: every claim is a node,
every piece of evidence is a weighted edge. The classifier queries the
graph ("what does the evidence say about X?") and gets a weighted summary
across all sources.

This file implements the graph in pure Python (NetworkX backing) so it
works without a database. Each existing point-query tool can populate
the graph; the new `evidence_graph_query` tool replaces 5 separate calls
with one graph query.

Schema (RDF-compatible — we'll move to SPARQL in 2028):

  Nodes (claim types):
    variant:17-43091752-T-G           — a specific variant
    gene:BRCA2                         — a gene
    phenotype:Hereditary_breast_cancer — a phenotype (HPO/OMIM)
    functional:splice_impact_high      — a functional claim
    classification:Pathogenic          — a classification claim

  Edges (evidence types):
    variant --[evidence:gnomad]-->            population_frequency
    variant --[evidence:clinvar]-->           clinical_assertion
    variant --[evidence:pubmed]-->            literature_support
    variant --[evidence:alphamissense]-->     pathogenicity_prediction
    gene   --[evidence:clingen]-->            gene_disease_validity
    gene   --[evidence:constraint]-->         missense_constraint
    variant --[evidence:classification]-->    classification

  Each edge carries:
    source: 'gnomad' | 'clinvar' | 'pubmed' | 'clingen' | 'alphamissense' | ...
    weight: 0.0..1.0 (confidence in this evidence)
    citation: PMID / RCV / gnomAD ID
    recency: unix timestamp
    properties: dict (source-specific data: AF, review_status, score, etc.)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import networkx as nx


@dataclass
class EvidenceNode:
    """A node in the evidence graph — a claim or entity."""

    id: str               # canonical ID, e.g. 'variant:17-43091752-T-G'
    kind: str             # 'variant' | 'gene' | 'phenotype' | 'functional' | 'classification'
    label: str            # human-readable
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidenceEdge:
    """An edge = a piece of evidence connecting two nodes."""

    source: str           # 'gnomad' | 'clinvar' | 'pubmed' | 'clingen' | ...
    weight: float         # 0.0..1.0
    citation: str         # PMID, RCV, gnomAD ID, etc.
    recency: float = field(default_factory=time.time)
    properties: dict[str, Any] = field(default_factory=dict)


class EvidenceGraph:
    """In-memory evidence graph.

    Thread-safe enough for the agent loop (single-threaded async).
    For multi-process deployment, swap the NetworkX backing for Redis Graph
    or Apache AGE on Postgres.
    """

    def __init__(self) -> None:
        self._g: nx.MultiDiGraph = nx.MultiDiGraph()

    # ---------- mutation ----------
    def add_node(self, node: EvidenceNode) -> None:
        self._g.add_node(node.id, kind=node.kind, label=node.label, **node.properties)

    def add_edge(self, src_id: str, dst_id: str, edge: EvidenceEdge) -> None:
        """Add evidence edge. Auto-creates missing nodes (call add_node explicitly
        for richer metadata)."""
        if src_id not in self._g:
            self._g.add_node(src_id)
        if dst_id not in self._g:
            self._g.add_node(dst_id)
        self._g.add_edge(
            src_id, dst_id,
            source=edge.source,
            weight=edge.weight,
            citation=edge.citation,
            recency=edge.recency,
            **edge.properties,
        )

    # ---------- query ----------
    def query(self, node_id: str, depth: int = 1) -> dict[str, Any]:
        """Return all evidence within `depth` hops of `node_id`.

        Returns:
          {
            'node': the queried node's properties (or None if absent),
            'edges': list of {src, dst, source, weight, citation, properties},
            'aggregate': weighted summary by source,
            'net_pathogenicity_score': 0..1 (if classification evidence exists),
          }
        """
        if node_id not in self._g:
            return {"node": None, "edges": [], "aggregate": {}, "net_pathogenicity_score": None}

        node_props = dict(self._g.nodes[node_id])

        # BFS to depth
        edges_out: list[dict] = []
        visited: set[str] = {node_id}
        frontier = [node_id]
        for _ in range(depth):
            next_frontier = []
            for n in frontier:
                for src, dst, data in self._g.out_edges(n, data=True):
                    edges_out.append({
                        "src": src, "dst": dst,
                        "source": data.get("source", "?"),
                        "weight": data.get("weight", 0.5),
                        "citation": data.get("citation", ""),
                        "recency": data.get("recency", 0),
                        "properties": {k: v for k, v in data.items()
                                       if k not in ("source", "weight", "citation", "recency")},
                    })
                    if dst not in visited:
                        visited.add(dst)
                        next_frontier.append(dst)
            frontier = next_frontier

        # Aggregate by source
        aggregate: dict[str, dict] = {}
        for e in edges_out:
            src = e["source"]
            aggregate.setdefault(src, {"count": 0, "avg_weight": 0.0, "citations": []})
            aggregate[src]["count"] += 1
            aggregate[src]["avg_weight"] += e["weight"]
            if e["citation"]:
                aggregate[src]["citations"].append(e["citation"])
        for src, agg in aggregate.items():
            agg["avg_weight"] = agg["avg_weight"] / agg["count"] if agg["count"] else 0

        # Compute net pathogenicity score (if any classification edges exist)
        net_score = self._compute_pathogenicity_score(node_id, edges_out)

        return {
            "node": node_props,
            "edges": edges_out,
            "aggregate": aggregate,
            "net_pathogenicity_score": net_score,
        }

    def _compute_pathogenicity_score(self, node_id: str, edges: list[dict]) -> float | None:
        """Compute a weighted pathogenicity score from all evidence edges.

        Sources contribute directionally:
          - clinvar 'pathogenic' edges → +weight
          - clinvar 'benign' edges → -weight
          - gnomad low AF → +0.3 (rarity)
          - gnomad high AF → -0.5 (common)
          - alphamissense >= 0.564 → +0.4
          - spliceai >= 0.5 → +0.5
          - pubmed functional studies → ±weight (depending on conclusion)
          - clingen HI gene + LOF variant → +0.5

        Returns a score in [-1.0, 1.0] (positive = pathogenic, negative = benign),
        or None if no directional evidence exists.
        """
        score = 0.0
        has_directional = False

        for e in edges:
            src = e["source"]
            w = e["weight"]
            props = e["properties"]

            if src == "clinvar":
                cs = str(props.get("clinical_significance", "")).lower()
                if "pathogenic" in cs and "benign" not in cs:
                    score += w * 0.5
                    has_directional = True
                elif "benign" in cs:
                    score -= w * 0.5
                    has_directional = True

            elif src == "gnomad":
                af = props.get("af")
                if af is not None:
                    has_directional = True
                    if af == 0:
                        score += 0.3  # absent — supporting pathogenic
                    elif af < 1e-5:
                        score += 0.2
                    elif af >= 0.05:
                        score -= 0.5  # BA1
                    elif af >= 0.01:
                        score -= 0.3  # BS1

            elif src == "alphamissense":
                am = props.get("am_score")
                if am is not None:
                    has_directional = True
                    if am >= 0.564:
                        score += 0.4
                    elif am < 0.34:
                        score -= 0.3

            elif src == "spliceai":
                sa = props.get("spliceai_score")
                if sa is not None:
                    has_directional = True
                    if sa >= 0.5:
                        score += 0.5
                    elif sa < 0.1:
                        score -= 0.1

            elif src == "clingen":
                if props.get("haploinsufficient"):
                    has_directional = True
                    score += 0.3

            elif src == "pubmed":
                has_directional = True
                # PubMed edges carry a 'direction' property set by the LLM
                direction = props.get("direction", "neutral")
                if direction == "pathogenic":
                    score += w * 0.3
                elif direction == "benign":
                    score -= w * 0.3

        if not has_directional:
            return None

        # Clamp to [-1, 1]
        return max(-1.0, min(1.0, score))

    # ---------- inspection ----------
    def node_count(self) -> int:
        return self._g.number_of_nodes()

    def edge_count(self) -> int:
        return self._g.number_of_edges()

    def has_node(self, node_id: str) -> bool:
        return node_id in self._g

    def to_dict(self) -> dict:
        """Serialize the graph for provenance / persistence."""
        return {
            "nodes": [
                {"id": n, **dict(d)} for n, d in self._g.nodes(data=True)
            ],
            "edges": [
                {"src": u, "dst": v, **dict(d)}
                for u, v, d in self._g.edges(data=True)
            ],
        }


# ---------- node ID helpers ----------
def variant_node_id(chrom: str, pos: int, ref: str, alt: str) -> str:
    return f"variant:{chrom.replace('chr', '')}-{pos}-{ref}-{alt}"


def gene_node_id(gene: str) -> str:
    return f"gene:{gene.upper()}"


def phenotype_node_id(hpo_or_omim: str) -> str:
    return f"phenotype:{hpo_or_omim}"


def classification_node_id(classification: str) -> str:
    return f"classification:{classification.replace(' ', '_')}"
