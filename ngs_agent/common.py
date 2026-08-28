"""Shared utility functions and helpers across NGS-Agent modules and containers."""

from __future__ import annotations

import json
import re
from typing import Any, Optional


DEFAULT_TRIM_PARAMS = {
    "LEADING": 3,
    "TRAILING": 3,
    "SLIDINGWINDOW": "4:20",
    "MINLEN": 36,
}


def extract_json(text: str) -> dict[str, Any] | None:
    """Extract and parse JSON object from raw text (handles markdown fences and mixed text)."""
    if not text or not isinstance(text, str):
        return None

    cleaned = text.strip()
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    md_match = re.search(r"```(?:json)?\s*({[\s\S]*?})\s*```", text, re.I)
    if md_match:
        try:
            obj = json.loads(md_match.group(1))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return None


def normalize_verdict(raw: str) -> str | None:
    """Normalize QC or decision verdict to canonical pass / trim_required / fail."""
    if not raw or not isinstance(raw, str):
        return None
    v = raw.strip().lower().replace("-", "_").replace(" ", "_")

    if v in ("pass", "trim_required", "fail"):
        return v
    if v in ("fail", "failed", "unusable", "error", "rejected"):
        return "fail"
    if "trim" in v:
        return "trim_required"
    if v in ("passed", "good", "ok", "okay", "accept", "accepted", "clean"):
        return "pass"
    return None


def normalize_params(params: dict[Any, Any] | None) -> dict[str, Any]:
    """Clamp and sanitize read trimming parameters."""
    merged = {**DEFAULT_TRIM_PARAMS, **(params or {})}
    try:
        merged["LEADING"] = max(0, min(40, int(merged["LEADING"])))
        merged["TRAILING"] = max(0, min(40, int(merged["TRAILING"])))
        merged["MINLEN"] = max(36, min(200, int(merged["MINLEN"])))
    except Exception:
        return dict(DEFAULT_TRIM_PARAMS)

    sw = str(merged.get("SLIDINGWINDOW", ""))
    if not re.match(r"^\d+:\d+$", sw):
        sw = DEFAULT_TRIM_PARAMS["SLIDINGWINDOW"]
    merged["SLIDINGWINDOW"] = sw
    return merged
