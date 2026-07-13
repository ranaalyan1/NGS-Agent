"""AlphaMissense lookup tool.

AlphaMissense provides pathogenicity scores for all possible missense
variants in the human proteome (Cheng et al. 2023, DeepMind).

Thresholds (per the AlphaMissense paper):
  >= 0.564 = likely pathogenic
  0.34 - 0.564 = ambiguous
  < 0.34 = likely benign

This tool loads from a TSV file at ~/.ngsagent/alphamissense_scores.tsv
(pre-download from Ensembl/AlphaMissense release). Falls back to None when
the variant isn't in the file or the file doesn't exist.

File format:
  chrom<TAB>pos<TAB>ref<TAB>alt<TAB>am_score
  1<TAB>100<TAB>A<TAB>G<TAB>0.82

The full AlphaMissense reference is ~71M variants (~3GB TSV). For panel-based
workflows, filter to your panel BED first.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..base import BaseTool, ToolContext, ToolInfo, ToolResponse

SCORES_FILE = Path.home() / ".ngsagent" / "alphamissense_scores.tsv"

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
                _CACHE[key] = float(row["am_score"])
            except (KeyError, ValueError):
                continue
    return _CACHE


def lookup_alphamissense(
    chrom: str, pos: int, ref: str, alt: str,
) -> tuple[float | None, str]:
    """Return (score, source). source is 'precomputed' or 'not_available'."""
    cache = _load_cache()
    key = (chrom.replace("chr", ""), pos, ref.upper(), alt.upper())
    if key in cache:
        return cache[key], "precomputed"
    return None, "not_available"


def classify(score: float | None) -> str:
    if score is None:
        return "UNKNOWN"
    if score >= 0.564:
        return "LIKELY_PATHOGENIC"
    if score < 0.34:
        return "LIKELY_BENIGN"
    return "AMBIGUOUS"


class AlphaMissenseTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="alphamissense_query",
            description=(
                "Look up the AlphaMissense pathogenicity score for a missense "
                "variant. Scores: >= 0.564 = likely pathogenic; < 0.34 = likely "
                "benign; 0.34-0.564 = ambiguous. Use to support PP3 (>= 2 "
                "damaging predictors agree) or BP4 (>= 2 benign predictors agree). "
                "Requires a pre-downloaded AlphaMissense TSV at "
                "~/.ngsagent/alphamissense_scores.tsv. If the file is missing "
                "or the variant isn't in it, returns 'not_available' — the LLM "
                "should then rely on REVEL or other predictors."
            ),
            parameters={
                "chrom": {"type": "string"},
                "pos": {"type": "integer"},
                "ref": {"type": "string"},
                "alt": {"type": "string"},
            },
            required=["chrom", "pos", "ref", "alt"],
        )

    async def run(self, params: dict[str, Any], ctx: ToolContext) -> ToolResponse:
        score, source = lookup_alphamissense(
            params["chrom"], int(params["pos"]),
            params["ref"], params["alt"],
        )
        classification = classify(score)

        if score is None:
            content = (
                f"# AlphaMissense — {params['chrom']}:{params['pos']}{params['ref']}>{params['alt']}\n"
                f"  Score:    NOT AVAILABLE\n"
                f"  Source:   {source}\n"
                f"  Action:   Use REVEL / SpliceAI / other predictors instead.\n"
            )
        else:
            content = (
                f"# AlphaMissense — {params['chrom']}:{params['pos']}{params['ref']}>{params['alt']}\n"
                f"  Score:    {score:.3f}\n"
                f"  Class:    {classification}\n"
                f"  Source:   {source}\n"
            )
            if classification == "LIKELY_PATHOGENIC":
                content += "  → Supports PP3 (one damaging predictor)\n"
            elif classification == "LIKELY_BENIGN":
                content += "  → Supports BP4 (one benign predictor)\n"

        return ToolResponse(
            content=content,
            metadata={
                "alphamissense_score": score,
                "classification": classification,
                "source": source,
            },
        )
