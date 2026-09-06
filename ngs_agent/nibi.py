"""Nibi — the NGS-Agent mascot.

A tiny genome creature with DNA antennae, big round eyes, a glowing cell
nucleus, adapter feet, and a sequence tail. Lives in the terminal and
reacts to what NGS-Agent is doing.

Expressions: happy, thinking, analyzing, running, success, error,
             curious, coffee, sleeping.

Mouse-tracking eye movement works on POSIX terminals via ANSI SGR mouse
mode. On Windows / non-TTY the mascot renders statically.

Used by ``ngs_agent.tui``.
"""

from __future__ import annotations

import os
import queue
import select
import sys
import threading
import time
from typing import Any, Callable, List, Literal, Optional

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

# ---------------------------------------------------------------------------
# Expression type
# ---------------------------------------------------------------------------

Expression = Literal[
    "happy", "thinking", "analyzing", "running",
    "success", "error", "curious", "coffee", "sleeping",
]

# ---------------------------------------------------------------------------
# Nibi ASCII art — 18 rows × 26 cols
#
# Anatomy (matches the design image):
#   rows 0-2  : DNA double-helix antennae  (╭═╮ G═C ╰═╯ style)
#   row  3    : antenna stalks merging into head
#   rows 4-5  : head top curve
#   rows 6-7  : eyes  (big round: ( ◉ ) style, pupils track mouse)
#   row  8    : mouth — swapped per expression
#   rows 9-10 : belly + glowing nucleus  ·  ⊛  ·
#   rows 11-12: body lower curve + sequence tail (ATGC)
#   rows 13-14: adapter feet
# ---------------------------------------------------------------------------

# Base body — expression-neutral rows (eyes and mouth are substituted)
# Column layout: leading 2 spaces so centering works nicely
_BODY = [
    #0         1         2
    #0123456789012345678901234567
    "    ╭═╮   ╭═╮              ",  # 0  antenna tops
    "    ║G║   ║C║              ",  # 1  DNA bases
    "    ╰═╯   ╰═╯              ",  # 2  antenna bottoms
    "      ╲   ╱               ",  # 3  stalks
    "    ╭─────────────╮        ",  # 4  head top
    "  ╭─╯             ╰─╮      ",  # 5  head curve
    "  │  ( ◉ )   ( ◉ )  │      ",  # 6  eyes  ← pupils here at cols 7,8 and 17,18
    "  │                  │      ",  # 7  face space
    "  │       ‿‿‿        │      ",  # 8  mouth  ← swapped per expression
    "  │    ·  ⊛  ·      │      ",  # 9  nucleus
    "  │                  │      ",  # 10 belly
    "  ╰─╮             ╭─╯      ",  # 11 lower curve
    "    ╰─────────────╯         ",  # 12 body bottom
    "    A  │  T  G  │  C       ",  # 13 sequence tail + legs
    "       ╰──╯  ╰──╯          ",  # 14 adapter feet
]

# Eye row index and pupil column positions (left-eye, right-eye)
_EYE_ROW = 6
_LEFT_PUPIL_COL = 7   # the ◉ char in "( ◉ )"
_RIGHT_PUPIL_COL = 17

# Mouth row index
_MOUTH_ROW = 8

# Mouth strings per expression (must fit in the body width between │ chars)
# Each string is exactly the content of row 8 (replaces "       ‿‿‿        ")
_MOUTHS: dict[Expression, str] = {
    "happy":     "  │       ‿‿‿        │      ",   # smile
    "thinking":  "  │       ···        │      ",   # dots
    "analyzing": "  │      ─────       │      ",   # flat focused
    "running":   "  │      ≋≋≋≋≋       │      ",   # vibrating
    "success":   "  │      \\(^▽^)/     │      ",   # celebration
    "error":     "  │       ︵         │      ",   # sad
    "curious":   "  │       ·‿·        │      ",   # curious small smile
    "coffee":    "  │      ～～～       │      ",   # steam/drinking
    "sleeping":  "  │      ─ ─ ─       │      ",   # flat sleeping
}

# Eye strings per expression (replaces row 6 entirely)
_EYES: dict[Expression, str] = {
    "happy":     "  │  ( ◉ )   ( ◉ )  │      ",
    "thinking":  "  │  ( ◔ )   ( ◉ )  │      ",   # one eye up
    "analyzing": "  │  ( ◈ )   ( ◈ )  │      ",   # focused square pupils
    "running":   "  │  ( ◉ )   ( ◉ )  │      ",
    "success":   "  │  ( ★ )   ( ★ )  │      ",   # star eyes
    "error":     "  │  ( × )   ( × )  │      ",   # X eyes
    "curious":   "  │  ( ◔ )   ( ◔ )  │      ",   # looking up
    "coffee":    "  │  ( - )   ( - )  │      ",   # half-closed
    "sleeping":  "  │  ( _ )   ( _ )  │      ",   # closed
}

# Nucleus glow per expression
_NUCLEUS: dict[Expression, str] = {
    "happy":     "  │    ·  ⊛  ·      │      ",
    "thinking":  "  │    ·  ⊙  ·      │      ",
    "analyzing": "  │    ·  ⊕  ·      │      ",
    "running":   "  │    ·  ⊗  ·      │      ",
    "success":   "  │    ✦  ⊛  ✦      │      ",
    "error":     "  │    ·  ⊘  ·      │      ",
    "curious":   "  │    ·  ⊙  ·      │      ",
    "coffee":    "  │    ·  ⊛  ·      │      ",
    "sleeping":  "  │    ·  ⊙  ·      │      ",
}

# Pupil movement bounds (for mouse tracking)
PUPIL_DX_RANGE = (-2, 2)
PUPIL_DY_RANGE = (-1, 1)

# Map pupil char per expression (replaces ◉)
_PUPIL_CHAR: dict[Expression, str] = {
    "happy":     "◉",
    "thinking":  "◔",
    "analyzing": "◈",
    "running":   "◉",
    "success":   "★",
    "error":     "×",
    "curious":   "◔",
    "coffee":    "-",
    "sleeping":  "_",
}


# ---------------------------------------------------------------------------
# render_nibi
# ---------------------------------------------------------------------------

def render_nibi(
    theme: dict,
    expression: Expression = "happy",
    pupil_dx: int = 0,
    pupil_dy: int = 0,
) -> Text:
    """Render Nibi as a styled rich.Text.

    Parameters
    ----------
    theme:
        The active TUI theme dict (keys: accent, accent_dim, muted, …).
    expression:
        One of the 9 named expressions that changes eyes, mouth, nucleus.
    pupil_dx, pupil_dy:
        Pixel offset for mouse-tracking pupil movement.
        Only applied on expressions with movable pupils (happy, running,
        curious). Clamped to PUPIL_DX_RANGE / PUPIL_DY_RANGE.
    """
    accent     = theme.get("accent",     "#00FF9C")
    accent_dim = theme.get("accent_dim", "#00805A")
    muted      = theme.get("muted",      "dim white")

    # Coral/salmon body color — Nibi's signature look from the design sheet
    body_color = "#FF6B6B"

    # Build the grid from the base body, substituting expression rows
    grid: list[str] = list(_BODY)
    grid[_MOUTH_ROW] = _MOUTHS.get(expression, _MOUTHS["happy"])
    grid[_EYE_ROW]   = _EYES.get(expression, _EYES["happy"])
    grid[9]          = _NUCLEUS.get(expression, _NUCLEUS["happy"])

    # Apply mouse-tracking pupil offset for expressions that support it
    movable = expression in ("happy", "running", "curious", "thinking", "analyzing")
    if movable:
        dx = max(PUPIL_DX_RANGE[0], min(PUPIL_DX_RANGE[1], pupil_dx))
        dy = max(PUPIL_DY_RANGE[0], min(PUPIL_DY_RANGE[1], pupil_dy))
        eye_row = list(grid[_EYE_ROW + dy] if dy != 0 else grid[_EYE_ROW])
        # The pupil chars sit at fixed relative positions within the eye string.
        # We shift them horizontally by dx within the ( ) bracket.
        # Left eye bracket spans roughly cols 5-9, right eye cols 15-19.
        pupil_char = _PUPIL_CHAR.get(expression, "◉")
        eye_str = grid[_EYE_ROW]
        chars   = list(eye_str)
        # Shift left pupil col
        lc = _LEFT_PUPIL_COL + dx
        lc = max(5, min(9, lc))
        # Shift right pupil col
        rc = _RIGHT_PUPIL_COL + dx
        rc = max(15, min(19, rc))
        # Clear old pupil positions and place new ones
        for c in range(5, 10):
            if c < len(chars) and chars[c] in ("◉", "◔", "◈", "★", "×", "-", "_"):
                chars[c] = " "
        for c in range(15, 20):
            if c < len(chars) and chars[c] in ("◉", "◔", "◈", "★", "×", "-", "_"):
                chars[c] = " "
        if lc < len(chars):
            chars[lc] = pupil_char
        if rc < len(chars):
            chars[rc] = pupil_char
        grid[_EYE_ROW] = "".join(chars)

    # Build styled Text
    out = Text(no_wrap=True)
    for row in grid:
        for ch in row:
            if ch in ("╭", "╮", "╰", "╯", "─", "│", "╲", "╱", "═", "║"):
                out.append(ch, style=f"bold {body_color}")
            elif ch in ("◉", "◔", "◈"):
                out.append(ch, style="bold white")
            elif ch == "★":
                out.append(ch, style="bold yellow")
            elif ch == "×":
                out.append(ch, style="bold red")
            elif ch in ("-", "_"):
                out.append(ch, style=body_color)
            elif ch in ("⊛", "⊙", "⊕", "⊗", "⊘"):
                out.append(ch, style=f"bold {accent}")
            elif ch == "✦":
                out.append(ch, style="bold yellow")
            elif ch in ("G", "C", "A", "T"):
                out.append(ch, style=f"bold {accent_dim}")
            elif ch in ("‿",):
                out.append(ch, style=f"bold {body_color}")
            elif ch in ("·", "～", "≋", "─", "="):
                out.append(ch, style=muted)
            elif ch == " ":
                out.append(ch)
            else:
                out.append(ch, style=body_color)
        out.append("\n")
    return out


# ---------------------------------------------------------------------------
# Expression hint text shown below Nibi
# ---------------------------------------------------------------------------

EXPRESSION_HINTS: dict[Expression, str] = {
    "happy":     "Nibi is ready  ·  move your mouse  ·  press any key to continue",
    "thinking":  "Nibi is thinking...",
    "analyzing": "Nibi is analyzing your data...",
    "running":   "Nibi is running the pipeline...",
    "success":   "Done! Nibi is happy with the results.",
    "error":     "Nibi encountered an error.",
    "curious":   "Nibi is curious about that file...",
    "coffee":    "Nibi is taking a coffee break. ☕",
    "sleeping":  "Nibi is sleeping. zZz",
}


# ---------------------------------------------------------------------------
# Mouse tracker — SGR mouse mode, background thread, careful cleanup
# ---------------------------------------------------------------------------

try:
    import termios  # type: ignore[import]
    import tty      # type: ignore[import]
    HAVE_TERMIOS = True
except ImportError:
    HAVE_TERMIOS = False


class MouseTracker:
    """Track terminal mouse motion via ANSI SGR + any-event mouse mode.

    Runs a daemon thread that reads raw stdin, parses
    ``ESC[<btn;x;y;M`` sequences, and fires callbacks with ``(x, y)``.
    Non-mouse keypresses are queued in ``self.keypresses``.
    All terminal state is restored on ``stop()``.
    """

    def __init__(self) -> None:
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.callbacks: List[Callable[[int, int], None]] = []
        self.keypresses: "queue.Queue[bytes]" = queue.Queue()
        self._fd: Optional[int] = None
        self._old_termios: Any = None

    def add_callback(self, cb: Callable[[int, int], None]) -> None:
        self.callbacks.append(cb)

    def start(self) -> bool:
        if not HAVE_TERMIOS:
            return False
        if self.running:
            return True
        try:
            self._fd = sys.stdin.fileno()
            self._old_termios = termios.tcgetattr(self._fd)
        except Exception:
            return False
        sys.stdout.write("\033[?1006h\033[?1003h")
        sys.stdout.flush()
        try:
            tty.setraw(self._fd, termios.TCSANOW)
        except termios.error:
            sys.stdout.write("\033[?1003l\033[?1006l")
            sys.stdout.flush()
            self._fd = None
            return False
        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)
        sys.stdout.write("\033[?1003l\033[?1006l")
        sys.stdout.flush()
        if self._fd is not None and self._old_termios is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_termios)
            except termios.error:
                pass
        self._fd = None
        self._old_termios = None

    def has_keypress(self) -> bool:
        return not self.keypresses.empty()

    def get_keypress(self, timeout: float = 0.0) -> Optional[bytes]:
        try:
            return self.keypresses.get(timeout=timeout)
        except queue.Empty:
            return None

    def _read_loop(self) -> None:
        assert self._fd is not None
        while self.running:
            try:
                if not select.select([self._fd], [], [], 0.1)[0]:
                    continue
                ch = os.read(self._fd, 1)
                if not ch:
                    continue
                if ch != b"\033":
                    self.keypresses.put(ch)
                    continue
                if not select.select([self._fd], [], [], 0.05)[0]:
                    self.keypresses.put(ch)
                    continue
                ch2 = os.read(self._fd, 1)
                if ch2 != b"[":
                    self.keypresses.put(ch)
                    self.keypresses.put(ch2)
                    continue
                if not select.select([self._fd], [], [], 0.05)[0]:
                    self.keypresses.put(ch)
                    self.keypresses.put(ch2)
                    continue
                ch3 = os.read(self._fd, 1)
                if ch3 != b"<":
                    self.keypresses.put(ch)
                    self.keypresses.put(ch2)
                    self.keypresses.put(ch3)
                    continue
                buf = b""
                while self.running:
                    if not select.select([self._fd], [], [], 0.1)[0]:
                        break
                    c = os.read(self._fd, 1)
                    if not c or c in (b"M", b"m"):
                        break
                    buf += c
                    if len(buf) > 30:
                        break
                try:
                    parts = buf.decode("ascii").split(";")
                    if len(parts) == 3:
                        x, y = int(parts[1]), int(parts[2])
                        for cb in self.callbacks:
                            cb(x, y)
                except (ValueError, UnicodeDecodeError):
                    pass
            except (OSError, IOError):
                break


# ---------------------------------------------------------------------------
# show_nibi_intro — welcome screen with live eye tracking
# ---------------------------------------------------------------------------

def show_nibi_intro(
    console: Console,
    theme: dict,
    duration: float = 8.0,
    expression: Expression = "happy",
    on_skip: Optional[Callable[[], None]] = None,
) -> None:
    """Render Nibi with live mouse-tracking pupils in a rich.Live block.

    Exits after ``duration`` seconds or when the user presses any key.
    Falls back to a 2-second static display on Windows / non-TTY.
    """
    tracker = MouseTracker()
    state: dict = {"x": None, "y": None, "dx": 0, "dy": 0}

    def on_mouse(x: int, y: int) -> None:
        state["x"] = x
        state["y"] = y

    tracker.add_callback(on_mouse)

    try:
        term_width = console.size.width
    except AttributeError:
        term_width = console.size.columns  # type: ignore[attr-defined]
    nibi_center_x = term_width // 2
    nibi_eye_screen_row = 17
    muted = theme.get("muted", "dim white")

    def build() -> Group:
        nibi = render_nibi(theme, expression, state["dx"], state["dy"])
        hint = Text(
            EXPRESSION_HINTS.get(expression, ""),
            style=f"italic {muted}",
        )
        return Group(Align.center(nibi), Align.center(hint))

    started = False
    try:
        with Live(build(), console=console, refresh_per_second=15,
                  transient=False) as live:
            if not tracker.start():
                time.sleep(2.0)
                return
            started = True
            start_time = time.time()
            while time.time() - start_time < duration:
                if tracker.has_keypress():
                    tracker.get_keypress()
                    if on_skip:
                        on_skip()
                    break
                x, y = state["x"], state["y"]
                if x is not None:
                    rel_x = x - nibi_center_x
                    sens_x = max(5, nibi_center_x // 4)
                    dx = max(PUPIL_DX_RANGE[0], min(PUPIL_DX_RANGE[1], rel_x // sens_x))
                    rel_y = y - nibi_eye_screen_row
                    dy = max(PUPIL_DY_RANGE[0], min(PUPIL_DY_RANGE[1], rel_y // 5))
                    if dx != state["dx"] or dy != state["dy"]:
                        state["dx"] = dx
                        state["dy"] = dy
                        live.update(build())
                time.sleep(0.05)
    finally:
        if started:
            tracker.stop()


# ---------------------------------------------------------------------------
# show_nibi_inline — one-shot render for status bars and non-live contexts
# ---------------------------------------------------------------------------

def show_nibi_inline(
    console: Console,
    theme: dict,
    expression: Expression = "happy",
) -> None:
    """Print Nibi statically (no Live block, no mouse tracking)."""
    from rich.align import Align
    console.print(Align.center(render_nibi(theme, expression)))
    hint = EXPRESSION_HINTS.get(expression, "")
    if hint:
        console.print(
            Align.center(Text(hint, style=f"italic {theme.get('muted', 'dim white')}"))
        )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from ngs_agent.config import load_config
    from ngs_agent.tui import THEMES

    cfg = load_config()
    theme = THEMES.get(cfg.get("theme", "dark"), THEMES["dark"])
    con = Console(force_terminal=True, legacy_windows=False)
    con.clear()
    con.print()
    con.print(Text("NGS-Agent — Nibi preview", style=f"bold {theme['accent']}"),
              justify="center")
    con.print()

    # Cycle through all expressions
    import time as _t
    exprs: list[Expression] = [
        "happy", "thinking", "analyzing", "running",
        "success", "error", "curious", "coffee", "sleeping",
    ]
    for expr in exprs:
        con.clear()
        show_nibi_inline(con, theme, expr)
        con.print(Align.center(Text(f"Expression: {expr}", style=theme["accent"])))
        _t.sleep(1.5)

    con.clear()
    con.print()
    con.print(Text("Live eye-tracking (8s) — move your mouse",
                   style=theme["muted"]), justify="center")
    con.print()
    show_nibi_intro(con, theme, duration=8.0)
    con.print()
    con.print(Text("Done.", style=theme["accent"]))
