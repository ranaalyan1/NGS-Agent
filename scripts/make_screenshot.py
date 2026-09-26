#!/usr/bin/env python3
"""Render real CLI output into docs/images/quickstart.png.

This is not a mock-up: the script runs ``doors.cli`` on a real fixture and draws
the bytes it printed, in a real monospace font, on a terminal-coloured canvas.
Re-run it whenever the CLI output changes:

    python scripts/make_screenshot.py

Requires Pillow (dev only; the product itself does not).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images" / "quickstart.png"

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_SIZE = 14
PADDING = 26
TITLE_BAR = 34
LINE_HEIGHT = FONT_SIZE + 5

BG = (18, 24, 31)
TITLE_BG = (31, 41, 51)
INK = (222, 230, 238)
MUTED = (140, 156, 172)
ACCENT = (122, 197, 224)
RED = (224, 122, 108)
GREEN = (126, 211, 156)
YELLOW = (226, 176, 92)
DIM = (110, 124, 138)


def colour_for(line: str) -> tuple[int, int, int]:
    stripped = line.strip()
    if stripped.startswith(("WHAT THIS IS", "WHAT MATTERS", "WHAT TO DO", "RECEIPTS")):
        return ACCENT
    if stripped.startswith("Problem"):
        return RED
    if stripped.startswith("Warning"):
        return YELLOW
    if stripped.startswith("Note"):
        return ACCENT
    if stripped.startswith(("┌", "│", "└")):
        return INK
    if stripped.startswith(("ngs-agent", "input SHA-256", "Every claim", "──")):
        return DIM
    if stripped.startswith(("What it is", "What it means", "1.", "2.", "3.")):
        return INK
    if stripped.startswith("QC-") or stripped.startswith("AUD-") or " @ " in stripped:
        return MUTED
    return INK


def run_cli(args: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "doors.cli", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"NO_COLOR": "1", "PATH": "/usr/bin:/bin"},
    )
    return result.stdout.rstrip("\n")


def main() -> None:
    from PIL import Image, ImageDraw, ImageFont

    fixture = ROOT / "fixtures" / "fastqc" / "sample_fastqc.zip"
    lines = run_cli([str(fixture)]).splitlines()
    # Trim to the interesting part so the image stays readable in the README.
    if len(lines) > 42:
        lines = lines[:42] + ["  …"]

    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    width = int(max(font.getlength(line) for line in lines) + PADDING * 2)
    height = TITLE_BAR + len(lines) * LINE_HEIGHT + PADDING * 2

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, TITLE_BAR], fill=TITLE_BG)
    for index, colour in enumerate(((224, 108, 100), (226, 176, 92), (126, 211, 156))):
        x = 16 + index * 18
        draw.ellipse([x, TITLE_BAR // 2 - 5, x + 10, TITLE_BAR // 2 + 5], fill=colour)
    draw.text(
        (width // 2 - 60, TITLE_BAR // 2 - 7), "ngs  sample_fastqc.zip", font=font, fill=MUTED
    )

    y = TITLE_BAR + PADDING
    for line in lines:
        draw.text((PADDING, y), line, font=font, fill=colour_for(line))
        y += LINE_HEIGHT

    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT)
    print(f"wrote {OUT} ({width}x{height})")


if __name__ == "__main__":
    main()
