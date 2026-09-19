"""Wrap over-length *plain* string literals that begin their line.

Splitting ``"aaa bbb"`` into ``"aaa " "bbb"`` relies on Python's implicit string
concatenation, so the runtime value is byte-identical. Only single-line plain
(non-f, non-triple-quoted) literals that start at the line's first non-space
character are touched -- the dominant pattern in these files is a block of
implicitly concatenated sentence fragments inside parentheses.

Everything else is left alone for a human edit. Verify with ruff + pytest after.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path("/home/user/NGS-Agent")
RUFF = [sys.executable, "-m", "ruff", "check", "ngs_agent/core", "tests/core",
        "--select", "E501", "--output-format=concise"]


def long_lines() -> dict[str, set[int]]:
    out = subprocess.run(RUFF, capture_output=True, text=True, cwd=ROOT).stdout
    found: dict[str, set[int]] = {}
    for m in re.finditer(r"^(.*?\.py):(\d+):\d+: E501", out, re.MULTILINE):
        found.setdefault(m.group(1), set()).add(int(m.group(2)))
    return found


def split_body(body: str, quote: str) -> tuple[str, str] | None:
    """Split at the last space that keeps both halves free of the quote char."""
    idx = body.rfind(" ", 1, len(body) - 1)
    while idx > 0 and body[idx - 1] == "\\":
        idx = body.rfind(" ", 1, idx - 1)
    if idx <= 0:
        return None
    left, right = body[: idx + 1], body[idx + 1 :]
    if not right or quote in left or quote in right:
        return None
    return left, right


def wrap_file(path: Path, linenos: set[int]) -> int:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, str]] = []

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return 0

    for tok in tokens:
        if tok.type != tokenize.STRING or tok.start[0] != tok.end[0]:
            continue
        lineno, col = tok.start
        if lineno not in linenos:
            continue
        text = tok.string
        low = text.lower()
        if low[0] == "f" or low[:2] in {"rf", "fr", "rb", "br"} or text.startswith('"""'):
            continue
        # Must begin the line's content.
        if lines[lineno - 1][:col].strip():
            continue
        quote = "'" if text.startswith("'") else '"'
        body = text[len(quote):-len(quote)]
        split = split_body(body, quote)
        if split is None:
            continue
        left, right = split
        trailing = lines[lineno - 1][tok.end[1]:].rstrip("\n")
        if trailing.strip() not in {"", ",", ")", "]", "}", "),", "],", "},"}:
            continue
        pad = " " * col
        edits.append((lineno, f"{pad}{quote}{left}{quote}\n{pad}{quote}{right}{quote}{trailing}\n"))

    if not edits:
        return 0
    for lineno, replacement in sorted(edits, reverse=True):
        lines[lineno - 1] = replacement
    path.write_text("".join(lines), encoding="utf-8")
    return len(edits)


total = 0
for relpath, linenos in sorted(long_lines().items()):
    total += wrap_file(ROOT / relpath, linenos)
print(f"wrapped {total} plain string literals")
