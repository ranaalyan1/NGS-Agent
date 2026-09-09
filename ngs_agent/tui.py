"""NGS-Agent REPL — Claude Code / OpenCode style interactive terminal.

Run by invoking ``ngsagent`` with no arguments (or ``ngsagent tui``).
All subcommands (watch / analyze / debate / config / doctor / plan) still
work directly from the shell without touching this module.

Features
--------
- Instant, non-blocking startup — no forced theme pick, no intro to skip
- Claude Code-style rounded input box with accent border
- Contextual Tab completion: subcommands, flags, file paths, slash commands
- Persistent history (arrows) + ghost-text auto-suggest
- Slash commands: /help /files /status /model /theme /doctor /nibi /clear /exit
- Shell escape: ``!ls -la`` (bare ``!`` spawns a subshell)
- In-process command execution — fast, no subprocess re-spawn
- Ctrl+C clears the input (press twice to exit), Ctrl+D exits
"""

from __future__ import annotations

import datetime
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ngs_agent import __version__
from ngs_agent.config import CONFIG_PATH, load_config, save_config
from ngs_agent.nibi import render_nibi_mini, show_nibi_intro

# prompt_toolkit is a core dependency, but import defensively so the REPL
# still degrades to a plain-input loop if it is missing in a broken install.
try:  # pragma: no cover - trivial import guard
    from prompt_toolkit.completion import Completer as _PTCompleter
    from prompt_toolkit.completion import Completion as _Completion

    _HAVE_PT = True
except ImportError:  # pragma: no cover
    _Completion = Any  # type: ignore[assignment,misc]

    class _PTCompleter:  # type: ignore[no-redef]
        """Placeholder so NGSCompleter can still be defined."""

        def get_completions(self, document: Any, complete_event: Any) -> Any:
            return iter(())

    _HAVE_PT = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "NGS-AGENT"
APP_TAGLINE = "Agentic bioinformatics CLI for wet-lab NGS teams"

NGS_GREEN = "#00FF9C"
NGS_GREEN_DIM = "#00805A"

HISTORY_PATH = Path.home() / ".ngsagent" / "history"

MAX_PATH_COMPLETIONS = 60

# ---------------------------------------------------------------------------
# Command catalogue — drives /help and Tab completion
# ---------------------------------------------------------------------------


@dataclass
class CommandSpec:
    name: str
    desc: str
    exts: tuple[str, ...] = ()          # preferred file extensions for completion
    flags: tuple[str, ...] = ()
    flag_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subcommands: tuple[str, ...] = ()


COMMANDS: dict[str, CommandSpec] = {
    "watch": CommandSpec(
        "watch", "Scan a pipeline log for failure signatures",
        exts=(".log", ".txt", ".out", ".err"),
        flags=("--tail", "--signatures"),
    ),
    "analyze": CommandSpec(
        "analyze", "Parse a VCF and render a variant/QC report",
        exts=(".vcf", ".vcf.gz", ".txt", ".tsv", ".csv"),
        flags=("--qc", "--html"),
    ),
    "debate": CommandSpec(
        "debate", "Three-persona LLM debate on VUS variants",
        exts=(".vcf", ".vcf.gz"),
        flags=("--gene", "--html"),
    ),
    "doctor": CommandSpec(
        "doctor", "Check environment, tools, and LLM readiness",
    ),
    "plan": CommandSpec(
        "plan", "Preview steps for an agentic workflow",
        flags=("--workflow",),
        flag_values={"--workflow": ("auto", "rnaseq", "wgs", "wes")},
    ),
    "config": CommandSpec(
        "config", "Manage ~/.ngsagent/config.yaml",
        subcommands=("show", "wizard", "set"),
    ),
    "tui": CommandSpec(
        "tui", "Start this interactive terminal",
    ),
}

SLASH_COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help",   "Show the command palette"),
    ("/files",  "Browse VCF / log / QC files in this directory"),
    ("/status", "Show config and LLM backend"),
    ("/model",  "Choose or reconfigure the LLM backend"),
    ("/theme",  "Switch color theme"),
    ("/doctor", "Run environment diagnostics"),
    ("/nibi",   "Meet Nibi, the mascot (live intro)"),
    ("/clear",  "Clear the screen"),
    ("/exit",   "Leave the terminal (same as Ctrl+D)"),
    ("/quit",   "Leave the terminal"),
)

SUBCOMMANDS = list(COMMANDS.keys())

FILE_EXTENSIONS = {".vcf", ".gz", ".log", ".txt", ".tsv", ".csv",
                   ".yaml", ".yml", ".out", ".err"}

CONFIG_KEYS = (
    "llm", "anthropic_model", "gemini_model", "openai_model",
    "ollama_model", "ollama_host", "openai_compat_base_url",
    "openai_compat_model", "theme",
)

# ---------------------------------------------------------------------------
# Themes  (name -> dict of rich style strings)
# ---------------------------------------------------------------------------

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "accent":      NGS_GREEN,
        "accent_dim":  NGS_GREEN_DIM,
        "title":       f"bold {NGS_GREEN}",
        "shadow":      NGS_GREEN_DIM,
        "border":      NGS_GREEN_DIM,
        "prompt":      f"bold {NGS_GREEN}",
        "panel_title": f"bold {NGS_GREEN}",
        "muted":       "dim white",
        "status_bg":   "on #0A0A0A",
        "llm_ok":      "green",
        "llm_none":    "red",
    },
    "light": {
        "accent":      "#007A4A",
        "accent_dim":  "#005533",
        "title":       "bold #007A4A",
        "shadow":      "#005533",
        "border":      "#007A4A",
        "prompt":      "bold #007A4A",
        "panel_title": "bold #007A4A",
        "muted":       "dim black",
        "status_bg":   "on #F0F0F0",
        "llm_ok":      "dark_green",
        "llm_none":    "dark_red",
    },
    "colorblind": {
        "accent":      "#0077BB",
        "accent_dim":  "#004477",
        "title":       "bold #0077BB",
        "shadow":      "#004477",
        "border":      "#0077BB",
        "prompt":      "bold #0077BB",
        "panel_title": "bold #0077BB",
        "muted":       "dim white",
        "status_bg":   "on #0A0A0A",
        "llm_ok":      "#0077BB",
        "llm_none":    "#EE7733",
    },
    "ansi": {
        "accent":      "bright_green",
        "accent_dim":  "green",
        "title":       "bold bright_green",
        "shadow":      "green",
        "border":      "green",
        "prompt":      "bold bright_green",
        "panel_title": "bold bright_green",
        "muted":       "dim",
        "status_bg":   "",
        "llm_ok":      "green",
        "llm_none":    "red",
    },
    "ansi-light": {
        "accent":      "green",
        "accent_dim":  "dark_green",
        "title":       "bold green",
        "shadow":      "dark_green",
        "border":      "green",
        "prompt":      "bold green",
        "panel_title": "bold green",
        "muted":       "dim",
        "status_bg":   "",
        "llm_ok":      "green",
        "llm_none":    "red",
    },
    "midnight": {
        "accent":      "#7B61FF",
        "accent_dim":  "#4A3BAA",
        "title":       "bold #7B61FF",
        "shadow":      "#4A3BAA",
        "border":      "#7B61FF",
        "prompt":      "bold #7B61FF",
        "panel_title": "bold #7B61FF",
        "muted":       "dim white",
        "status_bg":   "on #050510",
        "llm_ok":      "#7B61FF",
        "llm_none":    "#FF6B6B",
    },
}

THEME_NAMES = list(THEMES.keys())

# ---------------------------------------------------------------------------
# ASCII art for "NGS-AGENT"
# ---------------------------------------------------------------------------

# Built-in fallback — 6 rows, readable on any terminal width
_FALLBACK_LINES = [
    " ███╗   ██╗ ██████╗ ███████╗      █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    " ████╗  ██║██╔════╝ ██╔════╝     ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    " ██╔██╗ ██║██║  ███╗███████╗     ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    " ██║╚██╗██║██║   ██║╚════██║     ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    " ██║ ╚████║╚██████╔╝███████║     ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    " ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝     ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝  ",
]


def _render_title_lines() -> list[str]:
    """Return ASCII art lines for the app title (compact pyfiglet or fallback)."""
    try:
        import pyfiglet  # type: ignore[import]
        rendered = pyfiglet.figlet_format(APP_NAME, font="small")
        lines = [ln for ln in rendered.splitlines() if ln.strip()]
        if lines:
            return lines
    except Exception:
        pass
    return _FALLBACK_LINES


def render_title_clean(console: Console, theme: dict[str, str]) -> None:
    """Print ASCII title centered with a dim drop shadow below-right."""
    lines = _render_title_lines()
    n = len(lines)

    if console.is_terminal:
        for line in lines:
            padded = "  " + line  # 2-col right shift for shadow
            console.print(Text(padded, style=theme["shadow"]),
                          justify="center", highlight=False, overflow="crop")
        sys.stdout.write(f"\033[{n}A")
        sys.stdout.flush()
        for line in lines:
            console.print(Text(line, style=theme["title"]), justify="center",
                          highlight=False, overflow="crop")
    else:
        for line in lines:
            console.print(Text(line, style=theme["title"]), justify="center",
                          highlight=False, overflow="crop")


# ---------------------------------------------------------------------------
# Theme picker
# ---------------------------------------------------------------------------

def pick_theme(console: Console) -> str:
    """Show theme picker. Returns chosen theme name."""
    console.print()
    console.print(Text("Choose a color theme:", style="bold white"))
    console.print()
    for i, name in enumerate(THEME_NAMES, 1):
        t = THEMES[name]
        console.print(Text(f"  {i}. {name:<14}", style=t["accent"]))
    console.print()

    from rich.prompt import Prompt
    while True:
        raw = Prompt.ask("Theme number", default="1", console=console).strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(THEME_NAMES):
                return THEME_NAMES[idx]
        except ValueError:
            if raw in THEME_NAMES:
                return raw
        console.print(f"[red]Enter a number 1–{len(THEME_NAMES)}[/red]")


# ---------------------------------------------------------------------------
# Contextual completion — slash commands, subcommands, flags, file paths
# ---------------------------------------------------------------------------


def _iter_dir(base: Path) -> Iterable[Path]:
    try:
        return sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError:
        return []


def complete_line(text: str, cwd: Path | None = None) -> list[Any]:
    """Return Completions for ``text`` up to the cursor.

    Pure function (imports prompt_toolkit lazily) so it is unit-testable.
    """
    if not _HAVE_PT:  # pragma: no cover - broken installs only
        return []

    cwd = cwd or Path.cwd()

    # --- slash commands ----------------------------------------------------
    if text.startswith("/") and " " not in text:
        return [
            _Completion(name, start_position=-len(text), display=name, display_meta=desc)
            for name, desc in SLASH_COMMANDS
            if name.startswith(text)
        ]

    tokens = text.split()
    ends_with_space = text.endswith(" ") or not tokens

    # --- first token: subcommand --------------------------------------------
    if len(tokens) == 0 or (len(tokens) == 1 and not ends_with_space):
        word = tokens[0] if tokens else ""
        return [
            _Completion(name, start_position=-len(word), display=name, display_meta=spec.desc)
            for name, spec in COMMANDS.items()
            if name.startswith(word)
        ]

    cmd = tokens[0]
    spec = COMMANDS.get(cmd)

    # --- 'config' sub-subcommands --------------------------------------------
    if cmd == "config":
        if len(tokens) == 2 and not ends_with_space:
            word = tokens[1]
            return [
                _Completion(s, start_position=-len(word), display=s)
                for s in COMMANDS["config"].subcommands
                if s.startswith(word)
            ]
        if len(tokens) == 3 and tokens[1] == "set" and not ends_with_space:
            word = tokens[2]
            return [
                _Completion(k, start_position=-len(word), display=k)
                for k in CONFIG_KEYS
                if k.startswith(word)
            ]

    # --- flags ------------------------------------------------------------------
    last = tokens[-1]
    if last.startswith("-") and not ends_with_space and spec:
        return [
            _Completion(f, start_position=-len(last), display=f)
            for f in spec.flags
            if f.startswith(last)
        ]

    # --- flag values ---------------------------------------------------------------
    if spec:
        prev_token = tokens[-1] if ends_with_space else (tokens[-2] if len(tokens) >= 2 else None)
        if prev_token in spec.flag_values:
            prefix = "" if ends_with_space else last
            return [
                _Completion(v, start_position=-len(prefix), display=v)
                for v in spec.flag_values[prev_token]
                if v.startswith(prefix)
            ]

    # --- file paths -------------------------------------------------------------------
    fragment = "" if ends_with_space else last
    exts = spec.exts if (spec and spec.exts) else None
    return complete_path(fragment, cwd=cwd, exts=exts)


def complete_path(fragment: str, cwd: Path, exts: tuple[str, ...] | None) -> list[Any]:
    """Complete filesystem paths relative to ``cwd``.

    Directories are always offered (to drill into); files are filtered by
    ``exts`` when the command has a preferred type. Supports partial
    nested paths like ``demo_data/sam``.
    """
    if not _HAVE_PT:  # pragma: no cover
        return []

    # Bare "~" completes to the home directory shorthand
    if fragment == "~":
        return [_Completion("~/", display="~/", display_meta="home")]

    expanded = fragment
    if fragment.startswith("~/"):
        expanded = str(Path.home()) + fragment[1:]

    if expanded in ("", ".", "./"):
        base, prefix = Path("."), ""
    elif expanded.endswith(("/", os.sep)):
        base, prefix = Path(expanded), ""
    else:
        p = Path(expanded)
        base = p.parent if str(p.parent) not in ("", ".") else Path(".")
        prefix = p.name

    base_dir = base if base.is_absolute() else (cwd / base if str(base) != "." else cwd)

    results: list[Any] = []
    shown = 0
    for entry in _iter_dir(base_dir):
        name = entry.name
        if name.startswith(".") and not prefix.startswith("."):
            continue
        if prefix and not name.startswith(prefix):
            continue
        if entry.is_dir():
            results.append(_Completion(name + "/", start_position=-len(prefix),
                                       display=name + "/", display_meta="dir"))
        else:
            if exts is not None and not name.lower().endswith(tuple(exts)):
                continue
            try:
                meta = f"{entry.stat().st_size / 1024:.1f} KB"
            except OSError:
                meta = ""
            results.append(_Completion(name, start_position=-len(prefix),
                                       display=name, display_meta=meta))
        shown += 1
        if shown >= MAX_PATH_COMPLETIONS:
            break
    return results


class NGSCompleter(_PTCompleter):
    """Prompt-toolkit completer with NGS-aware context."""

    def __init__(self, cwd: Path | None = None) -> None:
        super().__init__()
        self.cwd = cwd or Path.cwd()

    def get_completions(self, document: Any, complete_event: Any) -> Any:
        yield from complete_line(document.text_before_cursor, cwd=self.cwd)


# ---------------------------------------------------------------------------
# Input box — Claude Code-style rounded frame via prompt_toolkit
# ---------------------------------------------------------------------------

DEFAULT_HINT = "  tab complete · ↑↓ history · /help · !shell · ctrl+c clear · ctrl+d exit"
EXIT_HINT = "  press ctrl+c again to exit"


def _pt_color(color: str) -> str:
    """Translate a rich color name/hex into a prompt_toolkit color token.

    Falls back to ansiwhite for names prompt_toolkit does not know,
    so a bad theme can never crash the input box.
    """
    if color.startswith("#"):
        return color
    from prompt_toolkit.styles.base import ANSI_COLOR_NAMES

    mapped = "ansi" + color.replace("_", "")
    return mapped if mapped in ANSI_COLOR_NAMES else "ansiwhite"


class _SyncLoadedHistory:
    """Marker that stands in for prompt_toolkit's async history-load task.

    prompt_toolkit only starts loading ``FileHistory`` when the buffer
    control is first rendered — but keys typed while the previous
    Application is tearing down are replayed as typeahead BEFORE that
    first render, so ↑-recall could hit an unloaded history. Priming the
    history synchronously and setting this marker skips the async load.
    """

    def cancel(self) -> bool:
        return False

    def done(self) -> bool:
        return True

    def cancelled(self) -> bool:
        return False


def _prime_history_synchronously(buf: Any, history: Any) -> None:
    """Load history entries into the buffer before the app starts."""
    try:
        entries = [e for e in history.load_history_strings() if e]
        for entry in entries:
            buf._working_lines.appendleft(entry)  # type: ignore[attr-defined]
            buf.working_index += 1
        buf._load_history_task = _SyncLoadedHistory()  # type: ignore[assignment]
    except Exception:  # noqa: BLE001, S110
        pass  # history recall is best-effort; never block the prompt


def _read_line_box(theme: dict[str, str], default: str = "") -> str | None:
    """Render the bordered input box and read one line.

    Returns the submitted text, or None when the user exits
    (Ctrl+D, or Ctrl+C twice on empty input).
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import (
        BufferControl,
        FormattedTextControl,
        HSplit,
        Layout,
        VSplit,
        Window,
    )
    from prompt_toolkit.styles import Style as PTStyle

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {"hint": DEFAULT_HINT, "last_ctrl_c": 0.0}

    def _on_text_changed(_: Buffer) -> None:
        state["last_ctrl_c"] = 0.0
        if state["hint"] != DEFAULT_HINT:
            state["hint"] = DEFAULT_HINT

    history = FileHistory(str(HISTORY_PATH))
    buf = Buffer(
        history=history,
        completer=NGSCompleter(),
        auto_suggest=AutoSuggestFromHistory(),
        complete_while_typing=True,
        multiline=False,
        on_text_changed=_on_text_changed,
        document=Document(default, len(default)) if default else None,
    )
    _prime_history_synchronously(buf, history)

    app_holder: dict[str, Any] = {}

    def _accept(buff: Buffer) -> bool:
        app = app_holder.get("app")
        if app is not None:
            app.exit(result=buff.text)
        return False  # reset the buffer for the next prompt

    buf.accept_handler = _accept

    kb = KeyBindings()

    @kb.add("tab")
    def _tab(event: Any) -> None:
        """Tab completion — computed synchronously to avoid prompt_toolkit's
        async double-tab race (second tab landing before completions load
        cancelled the insertion of the common part)."""
        from prompt_toolkit.completion import CompleteEvent, get_common_complete_suffix

        buf = event.current_buffer
        if buf.complete_state and buf.complete_state.completions:
            # Menu is open and loaded.
            state = buf.complete_state
            if len(state.completions) == 1:
                if state.complete_index is None:
                    buf.go_to_completion(0)  # select the single completion
                # else already inserted — cycling would wrap and deselect it.
                return
            buf.complete_next()
            return
        if not buf.completer:
            return

        document = buf.document
        completions = list(
            buf.completer.get_completions(document, CompleteEvent(completion_requested=True))
        )
        if not completions:
            return

        if len(completions) == 1:
            buf.apply_completion(completions[0])
            buf.complete_state = None
            return

        common_part = get_common_complete_suffix(document, completions)
        if common_part:
            buf.insert_text(common_part)
            remaining = [c.new_completion_from_position(len(common_part)) for c in completions]
            if len(remaining) > 1:
                buf._set_completions(completions=remaining)
            else:
                buf.complete_state = None
        else:
            # No common part — open the menu without preselecting.
            buf._set_completions(completions=completions)

    @kb.add("s-tab")
    def _shift_tab(event: Any) -> None:
        buf = event.current_buffer
        if buf.complete_state:
            buf.complete_previous()

    @kb.add("enter")
    def _enter(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("c-c")
    def _ctrl_c(event: Any) -> None:
        app = event.app
        if app.current_buffer.text:
            app.current_buffer.reset()
            state["hint"] = DEFAULT_HINT
        else:
            now = time.monotonic()
            if now - state["last_ctrl_c"] < 2.5:
                app.exit(result=None)
                return
            state["last_ctrl_c"] = now
            state["hint"] = EXIT_HINT
        app.invalidate()

    @kb.add("c-d")
    def _ctrl_d(event: Any) -> None:
        event.app.exit(result=None)

    body = VSplit([
        Window(
            FormattedTextControl(FormattedText([("class:prompt", " ❯ ")])),
            width=3,
            dont_extend_width=True,
            style="class:prompt",
        ),
        Window(BufferControl(buffer=buf), wrap_lines=True),
    ])

    layout = Layout(
        HSplit([
            _rounded_frame(body),
            Window(FormattedTextControl(lambda: FormattedText([("class:hint", state["hint"])])),
                   height=1, style="class:hint", wrap_lines=False, dont_extend_height=True),
        ]),
        focused_element=buf,
    )

    accent = _pt_color(theme["accent"])
    pt_style = PTStyle.from_dict({
        "frame":            f"fg:{accent}",
        "prompt":           f"fg:{accent} bold",
        "hint":             "fg:ansibrightblack italic",
        "auto-sugestion":   "fg:ansibrightblack",
        "completion-menu":  "bg:ansibrightblack fg:ansidefault",
        "completion-menu.completion": "bg:ansibrightblack fg:ansidefault",
        "completion-menu.completion.current": f"bg:{accent} fg:ansiwhite",
        "completion-menu.meta.completion": "bg:ansibrightblack fg:ansiwhite",
        "completion-menu.meta.completion.current": f"bg:{accent} fg:ansiwhite",
    })

    app: Any = Application(
        layout=layout,
        key_bindings=kb,
        style=pt_style,
        full_screen=False,
    )
    app_holder["app"] = app
    try:
        return app.run()
    except KeyboardInterrupt:  # pragma: no cover - hard SIGINT during input
        return None


def _rounded_frame(body: Any) -> Any:
    """Wrap ``body`` in a rounded box-drawing frame (╭─╮ │ ╰─╯)."""
    from prompt_toolkit.layout import HSplit, VSplit, Window

    def bar(left: str, right: str) -> Any:
        return VSplit([
            Window(width=1, height=1, char=left, style="class:frame"),
            Window(height=1, char="─", style="class:frame"),
            Window(width=1, height=1, char=right, style="class:frame"),
        ])

    return HSplit([
        bar("╭", "╮"),
        VSplit([
            Window(width=1, char="│", style="class:frame"),
            body,
            Window(width=1, char="│", style="class:frame"),
        ]),
        bar("╰", "╯"),
    ])


def _read_line_fallback(theme: dict[str, str]) -> str | None:
    """Plain prompt when prompt_toolkit is unavailable or stdin is not a TTY."""
    try:
        return input("❯ ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None


# ---------------------------------------------------------------------------
# Welcome banner
# ---------------------------------------------------------------------------

def _demo_hint() -> str:
    """Return a working example command if bundled demo data is present."""
    if (Path.cwd() / "demo_data" / "sample.vcf").exists():
        return "analyze demo_data/sample.vcf"
    if (Path.cwd() / "demo_data" / "sample.log").exists():
        return "watch demo_data/sample.log"
    return "analyze variants.vcf"


def render_banner(console: Console, theme: dict[str, str], cfg: dict[str, Any]) -> None:
    """Render the compact welcome banner (non-blocking, no animation)."""
    render_title_clean(console, theme)
    console.print()

    tagline = Text(APP_TAGLINE, style=f"italic {theme['muted']}")
    version_bits = Text(f"v{__version__}  ·  ", style=theme["muted"])
    for cmd in ("watch", "analyze", "debate", "doctor"):
        version_bits.append(cmd + "  ", style=theme["accent"])
    console.print(tagline, justify="center")
    console.print(version_bits, justify="center")
    console.print()

    nibi_panel = Panel(
        render_nibi_mini(theme, "happy"),
        title=f"[{theme['panel_title']}]Nibi[/{theme['panel_title']}]",
        border_style=theme["border"],
        padding=(0, 2),
    )

    demo = _demo_hint()
    quick_lines = "\n".join([
        f"[{theme['muted']}]{demo}[/{theme['muted']}]",
        f"[{theme['muted']}]watch pipeline.log --tail[/{theme['muted']}]",
        f"[{theme['muted']}]debate variants.vcf --gene BRCA2[/{theme['muted']}]",
        "",
        (f"[{theme['accent']}]/help[/{theme['accent']}] all commands"
         f"    [{theme['accent']}]!ls[/{theme['accent']}] shell escape"),
        "",
        "",
    ])
    quickstart = Panel(
        Text.from_markup(quick_lines),
        title=f"[{theme['panel_title']}]Quick start[/{theme['panel_title']}]",
        border_style=theme["border"],
        padding=(1, 2),
    )

    console.print(Columns([nibi_panel, quickstart], equal=True, expand=True))
    console.print()
    render_status_bar(console, theme, cfg)


def render_status_bar(console: Console, theme: dict[str, str], cfg: dict[str, Any]) -> None:
    llm = cfg.get("llm", "none")
    status = Text()
    if llm and llm != "none":
        model_label = (
            cfg.get(f"{llm}_model")
            or cfg.get("anthropic_model")
            or cfg.get("ollama_model")
            or llm
        )
        status.append("● ", style=theme["llm_ok"])
        status.append(f"{llm} · {model_label}", style=theme["llm_ok"])
    else:
        status.append("● no LLM", style=theme["llm_none"])
        status.append("  ·  watch & analyze need none  ·  /model to add", style=theme["muted"])

    line = Text()
    line.append(status)
    line.append(f"   📁 {Path.cwd()}", style=theme["muted"])
    line.append(f"   {datetime.datetime.now().astimezone().strftime('%H:%M')}", style=theme["muted"])
    console.print(line)
    console.print()


# ---------------------------------------------------------------------------
# /help palette
# ---------------------------------------------------------------------------

def show_help(console: Console, theme: dict[str, str]) -> None:
    console.print()
    console.print(Text("Command palette", style=theme["panel_title"]))
    console.print()

    console.print(Text("Subcommands — also work directly from the shell:",
                       style=theme["muted"]))
    t1 = Table(show_header=False, box=None, padding=(0, 2))
    t1.add_column(style=theme["accent"], no_wrap=True)
    t1.add_column(style="white")
    for name, spec in COMMANDS.items():
        t1.add_row(name, spec.desc)
    console.print(t1)

    console.print()
    console.print(Text("Slash commands — interactive terminal only:",
                       style=theme["muted"]))
    t2 = Table(show_header=False, box=None, padding=(0, 2))
    t2.add_column(style=theme["accent"], no_wrap=True)
    t2.add_column(style="white")
    for name, desc in SLASH_COMMANDS:
        t2.add_row(name, desc)
    console.print(t2)

    console.print()
    console.print(Text("Input shortcuts:", style=theme["muted"]))
    for tip in [
        "Tab completes commands, flags, and file paths",
        "↑ / ↓ walk through history · right-arrow accepts ghost text",
        "!<command> runs a shell command (! alone spawns a subshell)",
        "Ctrl+C clears the input (press twice to exit) · Ctrl+D exits",
    ]:
        console.print(f"  [{theme['muted']}]• {tip}[/{theme['muted']}]")
    console.print()


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

def show_status(console: Console, theme: dict[str, str], cfg: dict[str, Any]) -> None:
    console.print()
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style=theme["accent"])
    t.add_column(style="white")
    for key, val in cfg.items():
        t.add_row(key, str(val))
    console.print(
        Panel(t,
              title=f"[{theme['panel_title']}]{CONFIG_PATH}[/{theme['panel_title']}]",
              border_style=theme["border"])
    )
    console.print()


# ---------------------------------------------------------------------------
# /files picker
# ---------------------------------------------------------------------------

def show_files(console: Console, theme: dict[str, str]) -> str | None:
    """List relevant files in cwd. Returns a suggested command string or None."""
    cwd = Path.cwd()
    files = sorted(
        p for p in cwd.iterdir()
        if p.is_file() and p.suffix.lower() in FILE_EXTENSIONS
    )

    console.print()
    if not files:
        console.print(f"  [{theme['muted']}]No VCF / log / QC files found in {cwd}[/{theme['muted']}]")
        console.print()
        return None

    console.print(
        Panel(
            "\n".join(
                f"  [{theme['accent']}]{i}[/{theme['accent']}]  "
                f"[white]{f.name:<40}[/white]  "
                f"[{theme['muted']}]{f.stat().st_size / 1024:.1f} KB[/{theme['muted']}]"
                for i, f in enumerate(files, 1)
            ),
            title=f"[{theme['panel_title']}]Files in {cwd}[/{theme['panel_title']}]",
            border_style=theme["border"],
        )
    )
    console.print()

    from rich.prompt import Prompt
    raw = Prompt.ask(
        "  Pick a number to prefill (Enter to cancel)",
        default="",
        console=console,
        show_default=False,
    ).strip()

    if not raw:
        console.print()
        return None

    try:
        idx = int(raw) - 1
    except ValueError:
        return None
    if not (0 <= idx < len(files)):
        return None

    chosen = files[idx]
    if chosen.suffix.lower() in (".log", ".out", ".err"):
        suggestion = f"watch {chosen.name}"
    else:
        suggestion = f"analyze {chosen.name}"
    console.print(f"  [{theme['muted']}]Prefilling:[/{theme['muted']}] "
                  f"[{theme['accent']}]{suggestion}[/{theme['accent']}]")
    console.print()
    return suggestion


# ---------------------------------------------------------------------------
# In-process subcommand dispatch
# ---------------------------------------------------------------------------

def run_subcommand(tokens: list[str], console: Console, theme: dict[str, str]) -> int:
    """Run a CLI subcommand in-process. Returns the exit code."""
    from ngs_agent import cli as cli_module

    try:
        rc = cli_module.main(tokens, prog_name="ngsagent", standalone_mode=False)
        return 0 if rc is None else int(rc)
    except click.exceptions.UsageError as exc:
        console.print(f"[red]✗ {exc.format_message()}[/red]")
        console.print(f"[{theme['muted']}]Try /help for the command list.[/{theme['muted']}]")
        return 2
    except click.ClickException as exc:
        console.print(f"[red]✗ {exc.format_message()}[/red]")
        return 1
    except click.exceptions.Abort:
        console.print(f"[{theme['muted']}]Aborted.[/{theme['muted']}]")
        return 130
    except SystemExit as exc:
        code = exc.code
        return 0 if code in (None, 0) else int(code)
    except KeyboardInterrupt:
        console.print(f"\n[{theme['muted']}]^C interrupted[/{theme['muted']}]")
        return 130
    except Exception as exc:  # unexpected — surface cleanly, never crash the REPL
        console.print(Panel(
            f"[bold red]{type(exc).__name__}[/bold red]: {exc}\n\n"
            f"[dim]Run [bold]ngsagent doctor[/bold] for environment diagnostics.[/dim]",
            title="Unexpected error", border_style="red",
        ))
        return 1


def run_shell(tokens: list[str], console: Console, theme: dict[str, str]) -> int:
    """Run a shell command (``!ls -la``) with output streamed live."""
    if not tokens:
        shell = os.environ.get("SHELL") or "/bin/sh"
        console.print(f"[{theme['muted']}]$ spawning {shell} — 'exit' to return[/{theme['muted']}]")
        try:
            return subprocess.call([shell])
        except KeyboardInterrupt:
            return 130
        except OSError as exc:
            console.print(f"[red]✗ {exc}[/red]")
            return 1

    console.print(f"[{theme['muted']}]$ {' '.join(tokens)}[/{theme['muted']}]")
    try:
        return subprocess.call(tokens)
    except KeyboardInterrupt:
        console.print(f"\n[{theme['muted']}]^C interrupted[/{theme['muted']}]")
        return 130
    except FileNotFoundError:
        console.print(f"[red]✗ command not found: {tokens[0]}[/red]")
        return 127
    except OSError as exc:
        console.print(f"[red]✗ {exc}[/red]")
        return 1


def dispatch(line: str, console: Console, theme: dict[str, str]) -> int:
    """Parse and execute one REPL line. Returns the command exit code."""
    stripped = line.strip()

    # shell escape: "!ls -la" or bare "!"
    if stripped.startswith("!"):
        rest = stripped[1:].strip()
        if not rest:
            return run_shell([], console, theme)
        try:
            tokens = shlex.split(rest)
        except ValueError as exc:
            console.print(f"[red]✗ parse error: {exc}[/red]")
            return 1
        return run_shell(tokens, console, theme)

    try:
        tokens = shlex.split(stripped)
    except ValueError as exc:
        console.print(f"[red]✗ parse error: {exc}[/red]")
        return 1

    if not tokens:
        return 0

    if tokens[0].lower() == "tui":
        console.print(f"[{theme['muted']}]Already in the interactive terminal.[/{theme['muted']}]")
        return 0

    start = time.monotonic()
    rc = run_subcommand(tokens, console, theme)
    elapsed = time.monotonic() - start

    if rc == 0:
        console.print(f"[dim]✓ {elapsed:.1f}s[/dim]")
    else:
        console.print(f"[red]✗ exit {rc}[/red] [dim]({elapsed:.1f}s)[/dim]")
    console.print()
    return rc


# ---------------------------------------------------------------------------
# Slash command dispatcher
# ---------------------------------------------------------------------------

def handle_slash(
    cmd: str,
    console: Console,
    theme: dict[str, str],
    cfg: dict[str, Any],
) -> tuple[bool, str | None, dict[str, str], dict[str, Any]]:
    """Handle a slash command.

    Returns ``(should_continue, prefill, theme, cfg)``.
    ``prefill`` is a command string to pre-fill in the next input box.
    """
    parts = cmd.strip().split()
    name = parts[0].lower()

    if name in ("/exit", "/quit"):
        return False, None, theme, cfg

    if name == "/help":
        show_help(console, theme)
        return True, None, theme, cfg

    if name == "/theme":
        new_name = parts[1].lower() if len(parts) > 1 else None
        if new_name not in THEMES:
            if new_name is not None:
                console.print(f"[red]Unknown theme '{new_name}'.[/red] "
                              f"Available: {', '.join(THEME_NAMES)}")
            new_name = pick_theme(console)
        theme = THEMES[new_name]
        cfg["theme"] = new_name
        save_config(cfg)
        console.print(f"  [{theme['accent']}]Theme set to {new_name} "
                      f"— /clear to refresh the banner.[/{theme['accent']}]")
        console.print()
        return True, None, theme, cfg

    if name == "/files":
        suggestion = show_files(console, theme)
        return True, suggestion, theme, cfg

    if name == "/status":
        show_status(console, theme, cfg)
        return True, None, theme, cfg

    if name == "/model":
        from ngs_agent.config import run_wizard
        console.print(f"[{theme['muted']}]Current backend: "
                      f"{cfg.get('llm', 'none')}[/{theme['muted']}]\n")
        run_wizard()
        cfg = load_config()
        console.print(f"\n[{theme['accent']}]Backend saved:[/{theme['accent']}] "
                      f"{cfg.get('llm', 'none')}")
        console.print()
        return True, None, theme, cfg

    if name == "/doctor":
        from ngs_agent.doctor import print_diagnostics, run_diagnostics
        checks = run_diagnostics(console=console)
        print_diagnostics(checks, console=console)
        return True, None, theme, cfg

    if name == "/nibi":
        show_nibi_intro(console, theme, duration=8.0)
        console.print()
        return True, None, theme, cfg

    console.print(f"  [{theme['muted']}]Unknown slash command: {name}[/{theme['muted']}]")
    console.print(f"  [{theme['muted']}]Type /help to see all commands.[/{theme['muted']}]")
    return True, None, theme, cfg


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_tui() -> None:
    console = Console(legacy_windows=False)
    cfg = load_config()

    # Resolve theme silently — default to "dark", change with /theme
    theme_name = cfg.get("theme", "")  # type: ignore[arg-type]
    if theme_name not in THEMES:
        theme_name = "dark"
        cfg["theme"] = theme_name
        save_config(cfg)
    theme = THEMES[theme_name]

    interactive = (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and _HAVE_PT
    )

    # Set the terminal window title (cosmetic; ignored when piped)
    if sys.stdout.isatty():
        try:
            sys.stdout.write("\x1b]0;NGS-Agent — interactive\x07")
            sys.stdout.flush()
        except OSError:
            pass

    render_banner(console, theme, cfg)

    if not _HAVE_PT:
        console.print(f"[{theme['muted']}]prompt_toolkit not installed — using plain input."
                      f"[/{theme['muted']}] Run: pip install 'ngs-agent[tui]'\n")

    prefill: str | None = None

    while True:
        if interactive:
            line = _read_line_box(theme, default=prefill or "")
        else:
            line = _read_line_fallback(theme)
        prefill = None

        if line is None:  # Ctrl+D / double Ctrl+C / EOF
            console.print(f"\n[{theme['muted']}]Goodbye.[/{theme['muted']}]")
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            name = line.split()[0].lower()
            if name == "/clear":
                console.clear()
                render_banner(console, theme, cfg)
                continue
            should_continue, prefill, theme, cfg = handle_slash(line, console, theme, cfg)
            if not should_continue:
                console.print(f"[{theme['muted']}]Goodbye.[/{theme['muted']}]")
                break
            continue

        dispatch(line, console, theme)


if __name__ == "__main__":  # pragma: no cover
    run_tui()
