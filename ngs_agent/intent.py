"""Offline natural-language intent router (no LLM needed).

Powers `ngsagent run "..."` and plain-English input in the TUI, so users
don't have to memorize subcommands and flags — OpenCode-style.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    """A parsed user request."""

    action: str  # watch|analyze|debate|doctor|init|models|plan|status|help|unknown
    target: str | None = None  # file path mentioned in the request
    gene: str | None = None  # gene symbol for debate
    tail: bool = False  # follow a log live
    raw: str = ""
    confidence: str = "low"  # high|medium|low

    def describe(self) -> str:
        """Human-readable interpretation, echoed before executing."""
        bits = {
            "watch": "watch a pipeline log",
            "analyze": "analyze variants",
            "debate": "run a VUS debate",
            "doctor": "check system readiness",
            "init": "run guided setup",
            "models": "list LLM providers",
            "plan": "show a pipeline plan",
            "status": "show current config",
            "help": "show help",
            "unknown": "do something unclear",
        }
        text = bits.get(self.action, self.action)
        if self.target:
            text += f" for {self.target}"
        if self.gene:
            text += f" (gene {self.gene})"
        if self.tail:
            text += " (live tail)"
        return f"Understood as: {text}"


INTENT_EXAMPLES = [
    'ngsagent run "check my pipeline log"',
    'ngsagent run "analyze variants.vcf"',
    'ngsagent run "debate the VUS in BRCA2"',
    'ngsagent run "follow pipeline.log live"',
    'ngsagent run "is my system ready?"',
    'ngsagent run "set things up"',
]

# ---------------------------------------------------------------------------
# Keyword scoring — order-independent, phrase-based
# ---------------------------------------------------------------------------

# action -> phrases (lowercase). Longer / more specific phrases first.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "debate": (
        "debate", "discuss", "second opinion", "vus", "uncertain significance",
        "uncertain_significance", "classify", "pathogenic or benign", "argue",
    ),
    "watch": (
        "watch", "tail", "follow", "monitor", "track", "check the log", "check log",
        "scan the log", "scan log", "pipeline log", "what failed", "why did it fail",
        "failures", "failure", "log",
    ),
    "analyze": (
        "analyze", "analyse", "analysis", "report", "variants", "variant",
        "interpret", "look at", "summarize", "summarise", "qc",
    ),
    "doctor": (
        "doctor", "diagnos", "am i ready", "is my system", "is everything",
        "system check", "setup check", "set-up check", "tools installed",
        "environment", "prereq", "health", "ready",
    ),
    "init": (
        "initialize", "initialise", "getting started", "first run", "onboard",
        "set up", "setup", "configure", "start", "init",
    ),
    "models": (
        "which model", "list models", "switch model", "change model",
        "provider", "backend", "model", "llm",
    ),
    "plan": (
        "rnaseq", "rna-seq", "wgs", "wes", "workflow", "pipeline",
        "stages", "steps", "plan",
    ),
    "status": (
        "config show", "show config", "current config", "what's configured",
        "what is configured", "status",
    ),
    "help": (
        "what can you", "how do i", "how to", "usage", "commands",
        "examples", "help",
    ),
}

# Explicit subcommand word at the start wins immediately.
LEADING_COMMAND = {
    "watch": "watch",
    "analyze": "analyze",
    "analyse": "analyze",
    "debate": "debate",
    "doctor": "doctor",
    "init": "init",
    "models": "models",
    "model": "models",
    "plan": "plan",
    "status": "status",
    "help": "help",
}

TAIL_HINTS = ("--tail", "tail", "follow", "live", "as it grows", "real time", "real-time", "monitor")

#: "set up" with words in between ("set things up", "set it all up").
SETUP_RE = re.compile(r"\bset\b[\w\s]{0,20}\bup\b")

GENE_RE = re.compile(r"(?:--gene\b\s*|gene\s+)([A-Za-z][A-Za-z0-9_.-]*)", re.IGNORECASE)
QUOTED_RE = re.compile(r"'([^']+)'|\"([^\"]+)\"")
FILE_TOKEN_RE = re.compile(r"[\w\-./\\]+\.(?:vcf(?:\.gz)?|log|txt|tsv|csv|out|err)\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _extract_file(text: str) -> str | None:
    """Find a file path in free text: quoted strings, file-like tokens, or existing paths."""
    for match in QUOTED_RE.finditer(text):
        candidate = (match.group(1) or match.group(2) or "").strip()
        if candidate and len(candidate) < 256:
            return candidate

    token_match = FILE_TOKEN_RE.search(text)
    if token_match:
        return token_match.group(0)

    # Fall back to any token that exists on disk (handles extension-less logs).
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    for token in tokens:
        cleaned = token.strip().strip(",;:")
        if cleaned and Path(cleaned).exists() and Path(cleaned).is_file():
            return cleaned
    return None


def _extract_gene(text: str) -> str | None:
    match = GENE_RE.search(text)
    if match:
        return match.group(1).upper()
    return None


def parse_intent(text: str) -> Intent:
    """Map free text to an Intent. Pure function — safe to unit test."""
    raw = text.strip()
    lowered = raw.lower()
    intent = Intent(action="unknown", raw=raw)

    if not raw:
        return intent

    # 1. Explicit leading subcommand wins.
    first = lowered.split()[0].lstrip("/")
    if first in LEADING_COMMAND:
        intent.action = LEADING_COMMAND[first]
        intent.confidence = "high"
    else:
        # 2. Score keyword hits per action.
        scores: dict[str, int] = {}
        for action, phrases in KEYWORDS.items():
            scores[action] = sum(1 for phrase in phrases if phrase in lowered)
        if SETUP_RE.search(lowered):
            scores["init"] = scores.get("init", 0) + 2
        best = max(scores, key=lambda a: scores[a])
        if scores[best] > 0:
            intent.action = best
            intent.confidence = "medium" if scores[best] > 1 else "low"

    # 3. Extract file / gene / tail modifiers.
    intent.target = _extract_file(raw)
    intent.gene = _extract_gene(raw)
    intent.tail = any(hint in lowered for hint in TAIL_HINTS)

    # 4. Disambiguate with the file type.
    suffix = intent.target.lower() if intent.target else ""
    if suffix.endswith((".log", ".out", ".err")):
        if intent.action in ("analyze", "unknown") or "log" in lowered:
            intent.action = "watch"
            intent.confidence = "high"
    elif suffix.endswith((".vcf", ".vcf.gz")):
        if intent.action in ("watch", "unknown"):
            intent.action = "analyze" if intent.action == "unknown" else intent.action
            intent.confidence = "high"
        elif intent.action == "analyze":
            intent.confidence = "high"

    # Debate signals beat a bare VCF mention.
    debate_hits = sum(1 for phrase in KEYWORDS["debate"] if phrase in lowered)
    if debate_hits and intent.action in ("analyze", "unknown"):
        intent.action = "debate"
        intent.confidence = "high" if intent.target or intent.gene else "medium"

    return intent


def to_command(intent: Intent) -> list[str] | None:
    """Translate an Intent to `ngsagent` argv. None = handle in-process (help/status)."""
    if intent.action == "watch":
        cmd = ["watch"]
        if intent.target:
            cmd.append(intent.target)
        if intent.tail:
            cmd.append("--tail")
        return cmd
    if intent.action == "analyze":
        cmd = ["analyze"]
        if intent.target:
            cmd.append(intent.target)
        return cmd
    if intent.action == "debate":
        cmd = ["debate"]
        if intent.target:
            cmd.append(intent.target)
        if intent.gene:
            cmd += ["--gene", intent.gene]
        return cmd
    if intent.action in ("doctor", "init", "models", "plan"):
        return [intent.action]
    return None
