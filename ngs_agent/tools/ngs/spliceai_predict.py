"""SpliceAI predictor tool.

Production deployment: use the official SpliceAI Keras model from
https://github.com/Illumina/SpliceAI. The model takes a 50bp window around
the variant (or larger for long-range effects) and outputs delta scores for
acceptor gain, acceptor loss, donor gain, donor loss. Max delta > 0.5 is
the high-impact threshold.

This implementation:
  - Provides the interface (BaseTool) for the LLM
  - If a precomputed SpliceAI annotation TSV is available at
    ~/.ngsagent/spliceai_scores.tsv, it loads from there
  - Otherwise it falls back to a sequence-position heuristic:
    splice-region (±1, ±2 = canonical; ±3..±20 = extended) variants get
    conservative scores; deep-intronic and exonic far-from-splice variants
    get 0.
  - For clinical use, MUST install the real model.

File format (TSV):
  chrom<TAB>pos<TAB>ref<TAB>alt<TAB>delta_score
  1<TAB>100<TAB>A<TAB>G<TAB>0.82
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

SCORES_FILE = Path.home() / ".ngsagent" / "spliceai_scores.tsv"

# In-memory cache, lazily loaded
_CACHE: dict[tuple[str, int, str, str], float] | None = None


def _load_cache() -> dict[tuple[str, int, str, str], float]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    if not SCORES_FILE.exists():
        return _CACHE
    with open(SCORES_FILE) as f:
        reader = csv.DictReader(f, delimiter="\t")
        for row in reader:
            try:
                key = (row["chrom"].replace("chr", ""), int(row["pos"]),
                       row["ref"].upper(), row["alt"].upper())
                _CACHE[key] = float(row["delta_score"])
            except (KeyError, ValueError):
                continue
    return _CACHE


def heuristic_spliceai(consequence: str | None, distance_to_splice: int | None) -> float:
    """Fallback heuristic when no precomputed scores are available.

    Returns a conservative SpliceAI delta estimate based on:
      - consequence (splice_donor_variant / splice_acceptor_variant / splice_region_variant)
      - distance to nearest splice junction
    """
    if not consequence:
        return 0.0
    c = consequence.lower()
    if "splice_donor_variant" in c or "splice_acceptor_variant" in c:
        return 0.95  # canonical — almost certainly high-impact
    if "splice_region_variant" in c:
        if distance_to_splice is None:
            return 0.4
        if distance_to_splice <= 3:
            return 0.7
        if distance_to_splice <= 10:
            return 0.3
        return 0.1
    return 0.0


def predict_spliceai(
    chrom: str, pos: int, ref: str, alt: str,
    consequence: str | None = None,
    distance_to_splice: int | None = None,
) -> tuple[float, str]:
    """Return (delta_score, source).

    source is one of: 'precomputed' | 'heuristic'
    """
    cache = _load_cache()
    key = (chrom.replace("chr", ""), pos, ref.upper(), alt.upper())
    if key in cache:
        return cache[key], "precomputed"
    return heuristic_spliceai(consequence, distance_to_splice), "heuristic"


class SpliceAITool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="spliceai_predict",
            description=(
                "Predict the splice impact of a variant using SpliceAI delta "
                "score (0-1). Score >= 0.5 = high impact (likely exon skipping "
                "or cryptic site); 0.2-0.5 = moderate; <0.2 = low. Use for "
                "ANY variant within 50bp of a splice junction, and for deep-"
                "intronic variants with suspected cryptic splice creation. "
                "If precomputed scores are available at "
                "~/.ngsagent/spliceai_scores.tsv, uses those; otherwise falls "
                "back to a position-based heuristic. For clinical use, install "
                "the real SpliceAI Keras model and pre-compute scores for your "
                "panel. PP3 evidence requires score >= 0.5 (high-impact)."
            ),
            parameters={
                "chrom": {"type": "string"},
                "pos": {"type": "integer"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
                "consequence": {"type": "string", "description": "VEP consequence"},
                "distance_to_splice": {"type": "integer", "description": "Distance to nearest splice junction (bp)"},
            },
            required=["chrom", "pos", "ref", "alt"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        score, source = predict_spliceai(
            params["chrom"],
            int(params["pos"]),
            params["ref"],
            params["alt"],
            consequence=params.get("consequence"),
            distance_to_splice=params.get("distance_to_splice"),
        )

        if score >= 0.5:
            impact = "HIGH"
            interp = "Likely exon skipping or cryptic splice site creation. PP3 applies."
        elif score >= 0.2:
            impact = "MODERATE"
            interp = "Possible splice impact. Use PP3 only with additional predictors."
        else:
            impact = "LOW"
            interp = "No predicted splice impact. BP4 may apply (no splice disruption)."

        content = (
            f"# SpliceAI prediction — {params['chrom']}:{params['pos']}{params['ref']}>{params['alt']}\n"
            f"  Delta score: {score:.2f} (source: {source})\n"
            f"  Impact:      {impact}\n"
            f"  Interpretation: {interp}\n"
        )
        return ToolResponse(
            content=content,
            metadata={
                "spliceai_score": score,
                "source": source,
                "impact": impact,
            },
        )
