"""Nibi — the NGS-Agent mascot.

A tiny genome creature whose eyes track the mouse cursor across the terminal.

ASCII art is rendered as a 2D character grid; pupils are repositioned by
editing specific cells before converting to a rich.Text object. Mouse
motion is captured via ANSI SGR mouse mode (``\\033[?1006h`` +
``\\033[?1003h``) read in a background thread.

Used by ``ngs_agent.tui`` during the welcome screen intro.
"""

from __future__ import annotations

import os
import sys
import time
import select
import queue
import threading
from typing import Callable, List, Optional

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text


# ---------------------------------------------------------------------------
# Nibi ASCII art
# ---------------------------------------------------------------------------
# 15 rows × 22 cols. Eyes (sclera ◯ + pupil ●) are on rows 6-7.
# Pupils default to centered; render_nibi() moves them by (dx, dy).
#
#   row 0:        ╭─╮ ╭─╮            DNA antennae tops
#   row 1:        │A│ │T│            DNA bases (A/T/G/C)
#   row 2:        ╰╮╯ ╰╮╯            antennae bottoms
#   row 3:         ╰╮ ╭╯             antennae merge
#   row 4:      ╭───────────╮        head top
#   row 5:     ╱             ╲       head curve
#   row 6:    │   ◯     ◯    │       sclera (eye sockets)
#   row 7:    │   ●     ●    │       pupils (default centered)
#   row 8:    │               │      face
#   row 9:    │    ⊙⊙⊙⊙⊙    │       glowing cell nucleus
#   row 10:   │               │      belly
#   row 11:    ╲             ╱       body curve
#   row 12:     ╰───────────╯        body bottom
#   row 13:      ╱ │     │ ╲         legs + tail
#   row 14:     ╱  │     │  ╲        feet
# ---------------------------------------------------------------------------

NIBI_GRID: List[str] = [
    "        ╭─╮ ╭─╮        ",
    "        │A│ │T│        ",
    "        ╰╮╯ ╰╮╯        ",
    "         ╰╮ ╭╯         ",
    "      ╭───────────╮    ",
    "     ╱             ╲   ",
    "    │   ◯     ◯    │   ",   # row 6: sclera (eye sockets) — fixed
    "    │   ·     ·    │   ",   # row 7: pupil track (centered default)
    "    │               │   ",  # row 8: face
    "    │    ⊙⊙⊙⊙⊙    │   ",   # row 9: glowing cell nucleus
    "    │               │   ",  # row 10: belly
    "     ╲             ╱   ",
    "      ╰───────────╯    ",
    "       ╱ │     │ ╲     ",
    "      ╱  │     │  ╲    ",
]

NIBI_HEIGHT = len(NIBI_GRID)
NIBI_WIDTH = max(len(r) for r in NIBI_GRID)

# Pupil default positions (row, col) — 0-indexed within NIBI_GRID.
# Row 7 is the pupil track (between sclera at row 6 and face at row 8).
# When dy=-1 (looking up), the pupil moves to row 6, displacing the sclera
# to the centered position one row below — handled in render_nibi.
LEFT_PUPIL = (7, 8)
RIGHT_PUPIL = (7, 14)
LEFT_SCLERA = (6, 8)
RIGHT_SCLERA = (6, 14)

# Pupil movement bounds
PUPIL_DX_RANGE = (-2, 2)
PUPIL_DY_RANGE = (-1, 1)


def render_nibi(theme: dict, pupil_dx: int = 0, pupil_dy: int = 0) -> Text:
    """Render Nibi as a rich Text with pupils offset by (dx, dy).

    Pupils are clamped to PUPIL_DX_RANGE / PUPIL_DY_RANGE so they stay
    within the eye region of the ASCII art. When the pupil moves UP
    (dy < 0), it lands on the sclera row — in that case the sclera is
    rendered one row below to preserve the "eye socket" look.
    """
    accent = theme.get("accent", "#00FF9C")
    accent_dim = theme.get("accent_dim", "#00805A")
    muted = theme.get("muted", "dim white")

    # Clone the grid so we can mutate pupil positions
    grid = [list(row) for row in NIBI_GRID]

    # Clear default pupil positions
    for r, c in (LEFT_PUPIL, RIGHT_PUPIL):
        grid[r][c] = " "

    # Clamp offsets
    dx = max(PUPIL_DX_RANGE[0], min(PUPIL_DX_RANGE[1], pupil_dx))
    dy = max(PUPIL_DY_RANGE[0], min(PUPIL_DY_RANGE[1], pupil_dy))

    # Place pupils and adjust sclera position
    for pupil_pos, sclera_pos in (
        (LEFT_PUPIL, LEFT_SCLERA),
        (RIGHT_PUPIL, RIGHT_SCLERA),
    ):
        pr, pc = pupil_pos
        sr, sc = sclera_pos

        nr, nc = pr + dy, pc + dx

        # If pupil moves up onto the sclera row, push sclera down one row
        if dy < 0 and 0 <= sr + 1 < len(grid) and 0 <= sc < len(grid[sr + 1]):
            grid[sr][sc] = " "       # remove sclera from default position
            grid[sr + 1][sc] = "◯"  # place sclera one row below

        # Place pupil (only if within bounds)
        if 0 <= nr < len(grid) and 0 <= nc < len(grid[nr]):
            grid[nr][nc] = "●"

    # Build styled Text, character by character
    out = Text(no_wrap=True)
    for row in grid:
        line = "".join(row).rstrip()
        for ch in line:
            if ch == "●":
                out.append(ch, style="bold #000000 on #FFFFFF")
            elif ch == "◯":
                out.append(ch, style=f"bold {accent}")
            elif ch == "⊙":
                out.append(ch, style=f"bold {accent}")
            elif ch in ("╭", "╮", "╰", "╯", "─", "│", "╱", "╲"):
                out.append(ch, style=accent)
            elif ch in ("A", "T", "G", "C"):
                out.append(ch, style=f"bold {accent_dim}")
            elif ch == " ":
                out.append(ch)
            else:
                out.append(ch, style=accent)
        out.append("\n")
    return out


# ---------------------------------------------------------------------------
# Mouse tracker — SGR mouse mode, background thread, careful cleanup
# ---------------------------------------------------------------------------

# Mouse tracking requires termios, which is POSIX-only.
try:
    import termios  # type: ignore[import]
    import tty      # type: ignore[import]
    HAVE_TERMIOS = True
except ImportError:  # pragma: no cover - Windows
    HAVE_TERMIOS = False


class MouseTracker:
    """Track mouse motion in the terminal using ANSI SGR mouse mode.

    Enables ``\\033[?1006h`` (SGR encoding) + ``\\033[?1003h`` (any-event
    tracking — fires on every mouse motion, not just clicks).

    A background thread reads stdin in raw mode, parses
    ``\\033[<button;x;y;M`` sequences, and invokes registered callbacks
    with ``(x, y)`` coordinates (1-indexed, origin top-left of terminal).

    Non-mouse keypresses (e.g. user pressing Enter to skip the intro) are
    queued in ``self.keypresses`` for the main thread to drain.

    All terminal state changes are wrapped in try/finally so a crash or
    Ctrl+C will still restore the terminal.
    """

    def __init__(self) -> None:
        self.running: bool = False
        self.thread: Optional[threading.Thread] = None
        self.callbacks: List[Callable[[int, int], None]] = []
        self.keypresses: "queue.Queue[bytes]" = queue.Queue()
        self._fd: Optional[int] = None
        self._old_termios = None

    def add_callback(self, cb: Callable[[int, int], None]) -> None:
        self.callbacks.append(cb)

    def start(self) -> bool:
        """Enable mouse tracking. Returns False if unsupported."""
        if not HAVE_TERMIOS:
            return False
        if self.running:
            return True
        try:
            self._fd = sys.stdin.fileno()
            self._old_termios = termios.tcgetattr(self._fd)
        except (termios.error, AttributeError, ValueError):
            return False

        # Enable SGR mouse mode + any-event tracking
        sys.stdout.write("\033[?1006h\033[?1003h")
        sys.stdout.flush()

        # Raw mode so we can read individual bytes
        try:
            tty.setraw(self._fd, termios.TCSANOW)
        except termios.error:
            sys.stdout.write("\033[?1003l\033[?1006l")
            sys.stdout.flush()
            self._fd = None
            self._old_termios = None
            return False

        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        """Disable mouse tracking and restore terminal. Safe to call multiple times."""
        if not self.running:
            return
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)

        # Disable mouse tracking modes
        sys.stdout.write("\033[?1003l\033[?1006l")
        sys.stdout.flush()

        # Restore terminal settings
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
        """Background thread: parse SGR mouse events, queue other keypresses."""
        assert self._fd is not None
        while self.running:
            try:
                if not select.select([self._fd], [], [], 0.1)[0]:
                    continue
                ch = os.read(self._fd, 1)
                if not ch:
                    continue

                if ch != b"\033":
                    # Regular keypress — queue for main thread
                    self.keypresses.put(ch)
                    continue

                # Possible escape sequence (mouse event, arrow key, etc.)
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

                # Read parameters until M (press/motion) or m (release)
                buf = b""
                while self.running:
                    if not select.select([self._fd], [], [], 0.1)[0]:
                        break
                    c = os.read(self._fd, 1)
                    if not c:
                        break
                    if c in (b"M", b"m"):
                        break
                    buf += c
                    if len(buf) > 30:
                        break

                try:
                    parts = buf.decode("ascii").split(";")
                    if len(parts) == 3:
                        x = int(parts[1])
                        y = int(parts[2])
                        for cb in self.callbacks:
                            cb(x, y)
                except (ValueError, UnicodeDecodeError):
                    pass
            except (OSError, IOError):
                break


# ---------------------------------------------------------------------------
# Nibi intro — shows the mascot with eye-tracking for a few seconds
# ---------------------------------------------------------------------------

def show_nibi_intro(
    console: Console,
    theme: dict,
    duration: float = 8.0,
    on_skip: Optional[Callable[[], None]] = None,
) -> None:
    """Show Nibi with cursor-tracking eyes.

    Renders Nibi in a ``rich.Live`` region. A background ``MouseTracker``
    captures mouse motion; the main thread updates Nibi's pupils to point
    toward the cursor. Exits when the user presses any key, or after
    ``duration`` seconds.

    On terminals without mouse support (Windows, non-TTY, etc.), Nibi is
    shown statically for 2 seconds and the function returns.
    """
    tracker = MouseTracker()
    state: dict = {"x": None, "y": None, "dx": 0, "dy": 0}

    def on_mouse(x: int, y: int) -> None:
        state["x"] = x
        state["y"] = y

    tracker.add_callback(on_mouse)

    # Approximate Nibi's center on screen (Nibi is rendered centered)
    try:
        term_width = console.size.width
    except AttributeError:
        term_width = console.size.columns  # type: ignore[attr-defined]
    nibi_center_x = term_width // 2
    # Nibi's eyes are ~7 rows from the top of Nibi. After the title + tagline
    # (~10 rows), Nibi's eyes land at screen row ~17.
    nibi_eye_screen_row = 17

    muted = theme.get("muted", "dim white")

    def build() -> Group:
        nibi = render_nibi(theme, state["dx"], state["dy"])
        hint = Text(
            "  move your mouse — Nibi is watching  ·  press any key to continue",
            style=f"italic {muted}",
        )
        return Group(Align.center(nibi), Align.center(hint))

    started = False
    try:
        with Live(
            build(),
            console=console,
            refresh_per_second=15,
            transient=False,
        ) as live:
            if not tracker.start():
                # No mouse support — show static Nibi briefly
                time.sleep(2.0)
                return
            started = True

            start_time = time.time()
            while time.time() - start_time < duration:
                # User pressed any key → skip
                if tracker.has_keypress():
                    tracker.get_keypress()
                    if on_skip:
                        on_skip()
                    break

                # Update pupil offset based on last mouse position
                x, y = state["x"], state["y"]
                if x is not None:
                    rel_x = x - nibi_center_x
                    sens_x = max(5, nibi_center_x // 4)
                    dx = max(
                        PUPIL_DX_RANGE[0],
                        min(PUPIL_DX_RANGE[1], rel_x // sens_x),
                    )
                    rel_y = y - nibi_eye_screen_row
                    sens_y = 5
                    dy = max(
                        PUPIL_DY_RANGE[0],
                        min(PUPIL_DY_RANGE[1], rel_y // sens_y),
                    )
                    if dx != state["dx"] or dy != state["dy"]:
                        state["dx"] = dx
                        state["dy"] = dy
                        live.update(build())

                time.sleep(0.05)
    finally:
        if started:
            tracker.stop()


# ---------------------------------------------------------------------------
# Smoke test — run this file directly to preview Nibi
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from ngs_agent.config import load_config
    from ngs_agent.tui import THEMES

    cfg = load_config()
    theme_name = cfg.get("theme", "dark")
    theme = THEMES.get(theme_name, THEMES["dark"])

    console = Console(force_terminal=True, legacy_windows=False)
    console.clear()

    console.print()
    console.print(Text("NGS-Agent", style=f"bold {theme['accent']}"), justify="center")
    console.print()

    show_nibi_intro(console, theme, duration=15.0)

    console.print()
    console.print(Text("→ starting REPL...", style=theme.get("muted", "dim")))
