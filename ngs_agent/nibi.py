"""Nibi — the NGS Agent mascot.

A tiny genome creature living in data, exploring sequences and powering
bioinformatics workflows. Designed as the friendly face of NGS-Agent v1.0.0.

Anatomy (per the official character sheet):
  - DNA Antennae     — connects to life's code
  - Big Eyes         — sees everything
  - Cell Nucleus     — always processing
  - Adapter Feet     — moves through data
  - Sequence Tail    — made of reads (ATCG)

Nine expressions are provided, mirroring the design sheet:
  happy, thinking, analyzing, running, success, error,
  curious, coffee, sleeping
"""
from __future__ import annotations

from typing import Iterable

# ---------------------------------------------------------------------------
# Tagline (must appear in banner — per design sheet)
# ---------------------------------------------------------------------------
TAGLINE = "Analyze \u2022 Automate \u2022 Accelerate"

# ---------------------------------------------------------------------------
# Design details (per "Design Details" panel of the character sheet)
# ---------------------------------------------------------------------------
DESIGN_DETAILS: list[tuple[str, str, str]] = [
    ("DNA Antennae", "\u2727", "Connects to life's code \u2014 two helix strands on top of Nibi's head."),
    ("Big Eyes", "\u2727", "Sees everything \u2014 never misses a variant, a flag, or a log line."),
    ("Cell Nucleus", "\u2727", "Always processing \u2014 a glowing core that digests FASTQ, VCF, and QC."),
    ("Adapter Feet", "\u2727", "Moves through data \u2014 ligates to any pipeline, file, or stream."),
    ("Sequence Tail", "\u2727", "Made of reads ATCG \u2014 a trailing wisp of base calls."),
]

# ---------------------------------------------------------------------------
# Workflow state labels (per "In Terminal" panel of the character sheet)
# These are the exact strings the design sheet shows in the terminal mockup.
# ---------------------------------------------------------------------------
WORKFLOW_STATES: list[tuple[str, str]] = [
    ("fastq_loaded", "FASTQ Loaded"),
    ("qc_complete", "QC Complete"),
    ("aligning", "Aligning..."),
    ("almost_there", "Almost there!"),
]

# ---------------------------------------------------------------------------
# Pixel / Terminal icon (16-32px style). Compact 3-line form for inline use.
# Renders as a small Nibi face with antennae, eyes, and belly nucleus.
# ---------------------------------------------------------------------------
PIXEL_ICON = r"""
  /\_/\
 ( o.o )
  (●)
"""


# ---------------------------------------------------------------------------
# The nine expressions. Each is an 8-line ASCII block. Width ~12 chars.
# Convention:
#   line 1-2  : DNA antennae
#   line 3    : top of head
#   line 4    : eyes
#   line 5    : mouth / expression line
#   line 6    : belly nucleus (glowing)
#   line 7    : bottom of body + feet
#   line 8    : sequence tail (ATCG...)
# ---------------------------------------------------------------------------
_EXPRESSIONS: dict[str, str] = {
    "happy": r"""
   /\  /\
    |  |
  .------.
 |  o  o  |
 |   __   |
 |  (◉)  |
  \______/
   |__|
  ATCG~
""",
    "thinking": r"""
   /\  /\
    |  |
  .------.
 |  -  -  |
 |   ..   |
 |  (◉)  |
  \______/
   |__|
  ATCG?
""",
    "analyzing": r"""
   /\  /\
    |  |
  .------.
 |  O  O  |  🔍
 |   oo   |
 |  (◉)  |
  \______/
   |__|
  ATCG·
""",
    "running": r"""
   /\  /\
    |  |
  .------.
 |  >  <  |  ✨
 |   __   |
 |  (◉)  |
  \______/
   |__|
  ATCG>
""",
    "success": r"""
   /\  /\
    |  |
  .------.
 |  ^  ^  |  ✨
 |   __   |
 |  (◉)  |
  \______/
   |__|
  ATCG!
""",
    "error": r"""
   /\  /\
    |  |
  .------.
 |  x  x  |
 |   ~~   |
 |  (◉)  |
  \______/
   |__|
  ATCGx
""",
    "curious": r"""
   /\  /\
    |  |
  .------.
 |  ?  ?  |
 |   o    |
 |  (◉)  |
  \______/
   |__|
  ATCG?
""",
    "coffee": r"""
   /\  /\
    |  |
  .------.
 |  ~  ~  |  ☕
 |   __   |
 |  (◉)  |
  \______/
   |__|
  ATCG~
""",
    "sleeping": r"""
   /\  /\
    |  |
  .------.
 |  -  -  |  z
 |   ..   |  Z
 |  (◉)  |
  \______/
   |__|
  ATCG~
""",
}

# Aliases — lets the TUI / CLI use friendlier keys
_ALIASES: dict[str, str] = {
    "happy": "happy",
    "thinking": "thinking",
    "analyze": "analyzing",
    "analyzing": "analyzing",
    "run": "running",
    "running": "running",
    "success": "success",
    "ok": "success",
    "done": "success",
    "error": "error",
    "err": "error",
    "fail": "error",
    "curious": "curious",
    "doctor": "curious",
    "coffee": "coffee",
    "break": "coffee",
    "sleep": "sleeping",
    "sleeping": "sleeping",
    "idle": "sleeping",
}

# Default expression used when an unknown key is requested
DEFAULT_EXPRESSION = "happy"

# Map runtime states -> Nibi expressions (used by TUI / workflow renderer)
STATE_TO_EXPRESSION: dict[str, str] = {
    "idle": "sleeping",
    "ready": "happy",
    "thinking": "thinking",
    "analyzing": "analyzing",
    "running": "running",
    "tool_call": "running",
    "tool_result": "analyzing",
    "success": "success",
    "error": "error",
    "permission": "curious",
    "long_running": "coffee",
    "compacting": "thinking",
}

# Map workflow steps -> Nibi expressions
WORKFLOW_TO_EXPRESSION: dict[str, str] = {
    "fastq_loaded": "happy",
    "qc_complete": "analyzing",
    "aligning": "running",
    "almost_there": "success",
}


def get_expression(name: str) -> str:
    """Return the ASCII art for the named expression.

    Accepts canonical names (``happy``, ``analyzing``) and aliases (``ok``,
    ``err``, ``doctor``). Unknown names fall back to :data:`DEFAULT_EXPRESSION`.
    """
    key = _ALIASES.get(name.lower(), DEFAULT_EXPRESSION)
    return _EXPRESSIONS[key]


def expression_names() -> list[str]:
    """Return the canonical list of nine expression names."""
    return list(_EXPRESSIONS.keys())


def render_expression_panel(name: str) -> dict:
    """Return a single expression with its label, as structured segments.

    Returns ``{"label": <str>, "art": <plain str>}`` so the caller can
    print the art with a style applied (avoids inline-markup conflicts
    with backslashes in the ASCII art).
    """
    canon = _ALIASES.get(name.lower(), DEFAULT_EXPRESSION)
    label = canon.replace("_", " ").title()
    return {"label": label, "art": get_expression(canon)}


def render_gallery(names: Iterable[str] | None = None) -> str:
    """Render multiple expressions side-by-side as a gallery.

    Defaults to all nine expressions in three rows of three.
    """
    if names is None:
        names = expression_names()
    names = list(names)
    art_blocks = [get_expression(n).rstrip("\n").splitlines() for n in names]
    height = max(len(b) for b in art_blocks)
    # Pad each block to the same height
    for b in art_blocks:
        while len(b) < height:
            b.append("")
    # Compute column width
    col_width = max(len(line) for b in art_blocks for line in b) + 2
    # Group into rows of 3
    rows: list[list[list[str]]] = []
    for i in range(0, len(art_blocks), 3):
        rows.append(art_blocks[i : i + 3])
    out: list[str] = []
    for row_idx, row_blocks in enumerate(rows):
        # Header
        labels = [
            names[row_idx * 3 + j].replace("_", " ").title()
            for j in range(len(row_blocks))
        ]
        out.append("".join(label.ljust(col_width) for label in labels))
        # Body
        for line_idx in range(height):
            out.append(
                "".join(
                    (b[line_idx] if line_idx < len(b) else "").ljust(col_width)
                    for b in row_blocks
                )
            )
        out.append("")  # blank line between rows
    return "\n".join(out).rstrip()


def render_banner(version: str = "1.0.0", expression: str = "happy") -> dict:
    """Render the full CLI banner as structured segments.

    Returns a dict with keys:
        - ``title``: ``NGS Agent v1.0.0`` (rich-styled string)
        - ``tagline``: ``Analyze \u2022 Automate \u2022 Accelerate`` (rich-styled string)
        - ``art``: Nibi ASCII art (plain string, no inline markup \u2014 caller should
          apply a style via ``console.print(art, style="orange3")``)
        - ``subtitle``: short Nibi description

    The structured form avoids rich-markup parser issues with backslashes
    and brackets inside ASCII art.
    """
    return {
        "title": f"[bold orange3]NGS Agent v{version}[/bold orange3]",
        "tagline": f"[dim]{TAGLINE}[/dim]",
        "art": get_expression(expression),
        "subtitle": "[dim]A tiny genome creature living in data.[/dim]",
    }


def print_banner(console, version: str = "1.0.0", expression: str = "happy") -> None:
    """Convenience: print the full banner to a rich Console."""
    b = render_banner(version=version, expression=expression)
    console.print(b["title"])
    console.print(b["tagline"])
    console.print()
    console.print(b["art"], style="orange3")
    console.print()
    console.print(b["subtitle"])


def render_workflow_progress(current_state: str = "fastq_loaded") -> str:
    """Render the four workflow steps with checkmarks and a Nibi face.

    Mirrors the 'In Terminal' panel of the design sheet:
        FASTQ Loaded -> QC Complete -> Aligning... -> Almost there!
    """
    canon = _ALIASES.get(current_state.lower(), DEFAULT_EXPRESSION)
    # Find index of current state in WORKFLOW_STATES
    keys = [k for k, _ in WORKFLOW_STATES]
    try:
        idx = keys.index(current_state)
    except ValueError:
        idx = 0
    # Mini Nibi for the workflow line
    mini = "[orange3]( o.o )[/orange3]"
    parts: list[str] = []
    for i, (key, label) in enumerate(WORKFLOW_STATES):
        if i < idx:
            parts.append(f"[green]\u2713[/green] {label}")
        elif i == idx:
            parts.append(f"[bold orange3]\u25b6 {label}[/bold orange3]")
        else:
            parts.append(f"[dim]\u25cb {label}[/dim]")
    line1 = "  " + "  ->  ".join(parts)
    line2 = f"  {mini}  [dim]nibi:~$[/dim] [bold]{WORKFLOW_STATES[idx][1]}[/bold]"
    return line1 + "\n" + line2


__all__ = [
    "TAGLINE",
    "DESIGN_DETAILS",
    "WORKFLOW_STATES",
    "PIXEL_ICON",
    "DEFAULT_EXPRESSION",
    "STATE_TO_EXPRESSION",
    "WORKFLOW_TO_EXPRESSION",
    "get_expression",
    "expression_names",
    "render_expression_panel",
    "render_gallery",
    "render_banner",
    "print_banner",
    "render_workflow_progress",
]
