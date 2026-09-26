"""The command-line door: ``ngs <path>``.

One command shape, one answer, same verdict as the Box. This file contains no
logic about the data: it calls ``core.assess``, asks ``core.answer`` for the
plain-language blocks, and prints them as terminal cards.

Exit codes:
    0  pass, or warnings only
    1  at least one failed finding
    2  the input could not be interpreted
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from core.answer import Answer, answer_verdict
from core.assess import assess_path
from core.models import (
    DECISION_LABELS,
    DECISION_UNKNOWN,
    KIND_LABELS,
    SEVERITY_FAIL,
    Verdict,
)
from core.report import render_json
from core.version import RULESET_VERSION, TOOL_VERSION

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNKNOWN = 2

WIDTH = 78
RULE = "─" * WIDTH

_COLOURS = {
    "fail": "\033[31m",
    "warn": "\033[33m",
    "info": "\033[36m",
    "pass": "\033[32m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _colour(text: str, name: str, enabled: bool) -> str:
    if not enabled or name not in _COLOURS:
        return text
    return f"{_COLOURS[name]}{text}{_COLOURS['reset']}"


def _use_colour() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def exit_code(verdict: Verdict) -> int:
    """0 = pass or warnings, 1 = something failed, 2 = we cannot tell."""
    if verdict.decision == DECISION_UNKNOWN:
        return EXIT_UNKNOWN
    if any(f.severity == SEVERITY_FAIL for f in verdict.findings):
        return EXIT_FAILED
    return EXIT_OK


def _card(title: str, subtitle: str, colour: bool) -> list[str]:
    label = _colour(f" {title} ", "bold", colour)
    top = f"┌─{label}" + "─" * max(0, WIDTH - len(title) - 4) + "┐"
    sub = f"│ {subtitle:<{WIDTH - 2}} │"
    bottom = "└" + "─" * (WIDTH - 2) + "┘"
    return [top, sub, bottom]


def _wrap(text: str, indent: str = "  ", width: int = WIDTH) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = indent
    for word in words:
        candidate = f"{current} {word}" if current.strip() else f"{indent}{word}"
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = f"{indent}{word}"
        else:
            current = candidate
    if current.strip():
        lines.append(current)
    return lines or [indent]


def render_terminal(verdict: Verdict, answer: Answer, colour: bool | None = None) -> str:
    """The verdict as terminal cards. Same content as the HTML report."""
    colour = _use_colour() if colour is None else colour
    out: list[str] = []
    kind_label = KIND_LABELS.get(verdict.kind, verdict.kind)
    decision_label = DECISION_LABELS.get(verdict.decision, verdict.decision)
    badge = (
        "fail"
        if any(f.severity == SEVERITY_FAIL for f in verdict.findings)
        else ("warn" if verdict.findings else "pass")
    )
    if verdict.decision == DECISION_UNKNOWN:
        badge = "info"

    out += _card(verdict.subject, f"{kind_label} · {decision_label}", colour)
    out.append("")
    for line in _wrap(verdict.headline, indent="  "):
        out.append(_colour(line, badge, colour))
    out.append("")

    for block in answer.blocks:
        out.append(_colour(block.heading.upper(), "bold", colour))
        for line in block.lines:
            if line.startswith("Bottom line:"):
                continue  # already printed under the card
            if line.startswith(("What it is", "What it means")) or line.startswith("  "):
                out += _wrap(line.strip(), indent="    ")
            elif line.startswith(("Problem", "Warning", "Note")):
                out.append(_colour("  " + line, "bold", colour))
            else:
                out += _wrap(line, indent="  ")
        out.append("")

    out.append(_colour(RULE, "dim", colour))
    out.append(
        _colour(
            f"  ngs-agent {verdict.tool_version} · ruleset {verdict.ruleset_version} · "
            f"{verdict.timestamp}",
            "dim",
            colour,
        )
    )
    if verdict.details.get("input_sha256"):
        out.append(_colour(f"  input SHA-256 {verdict.details['input_sha256']}", "dim", colour))
    out.append(
        _colour(
            "  Every claim above is backed by the receipt printed beside it.",
            "dim",
            colour,
        )
    )
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ngs",
        description="Drop an NGS file on it and get a plain-language answer with receipts.",
        epilog="exit codes: 0 = pass or warnings, 1 = something failed, 2 = unknown input",
    )
    parser.add_argument(
        "path",
        help="a quality-control report (FastQC or combined summary), a run folder, "
        "or a pipeline log (Nextflow, Snakemake, Cromwell/WDL)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print the full verdict as JSON instead of cards"
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ngs-agent {TOOL_VERSION} (ruleset {RULESET_VERSION})",
    )
    args = parser.parse_args(argv)

    verdict = assess_path(args.path)
    answer = answer_verdict(verdict)

    if args.json:
        print(json.dumps(json.loads(render_json(verdict, answer)), indent=2))
    else:
        print(render_terminal(verdict, answer))
    return exit_code(verdict)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
