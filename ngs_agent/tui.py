"""NGS-Agent TUI — Claude Code-style interactive terminal.

Run by invoking `ngsagent` with no arguments.
All existing subcommands (watch / analyze / debate / config) still work
directly from the shell without touching this module.
"""

from __future__ import annotations

import datetime
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.columns import Columns
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ngs_agent import __version__
from ngs_agent.config import CONFIG_PATH, load_config, save_config
from ngs_agent.nibi import Expression, render_nibi, show_nibi_intro, show_nibi_inline

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "NGS-AGENT"
APP_TAGLINE = "Agentic bioinformatics CLI for wet-lab NGS teams"

NGS_GREEN = "#00FF9C"
NGS_GREEN_DIM = "#00805A"
NGS_ACCENT = "#00CC7A"

SLASH_COMMANDS = ["/help", "/theme", "/files", "/status", "/clear", "/exit", "/quit"]

SUBCOMMANDS = ["watch", "analyze", "debate", "config"]

FILE_EXTENSIONS = {".vcf", ".log", ".txt", ".tsv", ".csv", ".yaml", ".yml"}

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
# One-line ASCII art for "NGS-AGENT"
# Rendered as a single row of tall characters using pyfiglet if available,
# otherwise falls back to a compact built-in 3-row block.
# ---------------------------------------------------------------------------

# Built-in fallback — 3 rows, readable on any terminal width
_FALLBACK_LINES = [
    " ███╗   ██╗ ██████╗ ███████╗      █████╗  ██████╗ ███████╗███╗   ██╗████████╗",
    " ████╗  ██║██╔════╝ ██╔════╝     ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝",
    " ██╔██╗ ██║██║  ███╗███████╗     ███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   ",
    " ██║╚██╗██║██║   ██║╚════██║     ██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   ",
    " ██║ ╚████║╚██████╔╝███████║     ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   ",
    " ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝     ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝  ",
]


def _render_title_lines() -> list[str]:
    """Return ASCII art lines for the app title."""
    try:
        import pyfiglet  # type: ignore[import]
        rendered = pyfiglet.figlet_format(APP_NAME, font="banner3")
        lines = [ln for ln in rendered.splitlines() if ln.strip()]
        if lines:
            return lines
    except Exception:
        pass
    return _FALLBACK_LINES


def render_title(console: Console, theme: dict[str, str]) -> None:
    """Print the ASCII art title with a dim shadow offset by 1 col."""
    lines = _render_title_lines()
    width = console.size.columns

    for i, line in enumerate(lines):
        # shadow: same line shifted right by 2 chars, dimmed
        shadow = Text(" " * 2 + line, style=theme["shadow"])
        # foreground line
        fg = Text(line, style=theme["title"])
        # center both — print shadow first, then overprint with fg
        console.print(shadow, justify="center", highlight=False, overflow="crop")

    # Re-print foreground on top by moving cursor back up (ANSI)
    # Rich doesn't support cursor movement, so we print fg centered separately
    # and rely on the contrast between shadow (dim) and fg (bright) rows.
    # For a true extruded look, re-print fg offset 1 row above shadow.
    # Since we can't move cursor, we print fg lines centered after shadow lines.
    # This gives a "double exposure" effect: shadow row then bright row.
    # The simplest readable approach: print shadow then fg side by side in one pass.

    # Reset: print clean fg block
    for line in lines:
        console.print(Text(line, style=theme["title"]), justify="center",
                      highlight=False, overflow="crop")


def render_title_clean(console: Console, theme: dict[str, str]) -> None:
    """Print ASCII title centered, shadow offset below-right by printing
    shadow first then overwriting via a second pass using ANSI cursor-up.
    Falls back to a single-pass bright title when ANSI is unavailable."""
    lines = _render_title_lines()
    n = len(lines)

    # Try ANSI cursor-up trick for real 3D extrusion
    if console.is_terminal:
        # Print dim shadow shifted 2 cols right
        for line in lines:
            padded = "  " + line  # 2-col right shift for shadow
            console.print(Text(padded, style=theme["shadow"]),
                          justify="center", highlight=False, overflow="crop")
        # Move cursor up n lines
        sys.stdout.write(f"\033[{n}A")
        sys.stdout.flush()
        # Print bright foreground on top
        for line in lines:
            console.print(Text(line, style=theme["title"]),
                          justify="center", highlight=False, overflow="crop")
    else:
        for line in lines:
            console.print(Text(line, style=theme["title"]),
                          justify="center", highlight=False, overflow="crop")


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
        swatch = Text(f"  {i}. {name:<14}", style=t["accent"])
        console.print(swatch)
    console.print()

    from rich.prompt import Prompt
    while True:
        raw = Prompt.ask(
            "Theme number",
            default="1",
            console=console,
        ).strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(THEME_NAMES):
                return THEME_NAMES[idx]
        except ValueError:
            if raw in THEME_NAMES:
                return raw
        console.print(f"[red]Enter a number 1–{len(THEME_NAMES)}[/red]")


# ---------------------------------------------------------------------------
# Welcome banner
# ---------------------------------------------------------------------------

def render_welcome(console: Console, theme: dict[str, str]) -> None:
    """Render the welcome screen with STATIC Nibi (no eye tracking).

    Used for /clear and other re-render paths. For the live eye-tracking
    intro, see run_tui() which calls show_nibi_intro() directly.
    """
    from rich.align import Align
    _render_welcome_top(console, theme)
    console.print(Align.center(render_nibi(theme, "happy", 0, 0)))
    console.print()
    _render_welcome_panels(console, theme)
    console.print()


def _render_welcome_top(console: Console, theme: dict[str, str]) -> None:
    """Title + tagline + version. Shared by render_welcome and the live intro."""
    console.clear()
    render_title_clean(console, theme)
    console.print()

    tagline = Text(APP_TAGLINE, style=f"italic {theme['muted']}")
    version_bits = Text(f"v{__version__}  ·  ", style=theme["muted"])
    for cmd in SUBCOMMANDS:
        version_bits.append(cmd + "  ", style=theme["accent"])
    console.print(tagline, justify="center")
    console.print(version_bits, justify="center")
    console.print()


def _render_welcome_panels(console: Console, theme: dict[str, str]) -> None:
    """Quickstart + slash-commands panels. Shared by render_welcome and the live intro."""
    quickstart = Panel(
        Text.from_markup(
            f"[{theme['muted']}]ngsagent watch pipeline.log[/{theme['muted']}]\n"
            f"[{theme['muted']}]ngsagent analyze variants.vcf --qc multiqc.txt[/{theme['muted']}]\n"
            f"[{theme['muted']}]ngsagent debate variants.vcf --gene BRCA2[/{theme['muted']}]\n"
            f"\n[{theme['muted']}]Or run a command here:[/{theme['muted']}]\n"
            f"[{theme['accent']}]> watch demo_data/sample.log[/{theme['accent']}]\n"
            f"[{theme['accent']}]> analyze demo_data/sample.vcf[/{theme['accent']}]"
        ),
        title=f"[{theme['panel_title']}]Quick start[/{theme['panel_title']}]",
        border_style=theme["border"],
        padding=(1, 2),
    )

    slash_lines = "\n".join(
        f"[{theme['accent']}]{cmd:<10}[/{theme['accent']}]  [{theme['muted']}]{desc}[/{theme['muted']}]"
        for cmd, desc in [
            ("/help",   "show command palette"),
            ("/theme",  "switch color theme"),
            ("/files",  "browse VCF / log / QC files"),
            ("/status", "show config + LLM backend"),
            ("/clear",  "clear screen"),
            ("/exit",   "leave the TUI"),
        ]
    )
    slash_panel = Panel(
        Text.from_markup(slash_lines),
        title=f"[{theme['panel_title']}]Slash commands[/{theme['panel_title']}]",
        border_style=theme["border"],
        padding=(1, 2),
    )

    console.print(Columns([quickstart, slash_panel], equal=True, expand=True))
    console.print()


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

def render_status_bar(console: Console, theme: dict[str, str], cfg: dict[str, Any]) -> None:
    llm = cfg.get("llm", "none")
    if llm and llm != "none":
        model_label = cfg.get("anthropic_model") or cfg.get("ollama_model") or llm
        dot = Text("● ", style=theme["llm_ok"])
        dot.append(llm, style=theme["llm_ok"])
        dot.append(f" · {model_label}", style=theme["muted"])
    else:
        dot = Text("● none", style=theme["llm_none"])
        dot.append(" · no LLM", style=theme["muted"])

    cwd_text = Text(f"  📁 {Path.cwd()}  ", style=theme["muted"])
    clock = Text(datetime.datetime.now().strftime("🕐 %H:%M"), style=theme["muted"])

    bar = Text()
    bar.append_text(dot)
    bar.append("  ")
    bar.append_text(cwd_text)
    bar.append_text(clock)

    console.print(Rule(style=theme["border"]))
    console.print(bar)


# ---------------------------------------------------------------------------
# /help palette
# ---------------------------------------------------------------------------

def show_help(console: Console, theme: dict[str, str]) -> None:
    console.print()
    console.print(
        Text("NGS-Agent TUI — command palette", style=theme["panel_title"])
    )
    console.print()

    console.print(Text("Subcommands (run as if from the shell):", style=theme["muted"]))
    for name, usage, desc in [
        ("watch",   "watch <logfile> [--tail] [--signatures DIR]",
         "Scan a pipeline log against failure signatures."),
        ("analyze", "analyze <vcffile> [--qc <qcfile>]",
         "Parse VCF + optional QC summary; render colour-coded report."),
        ("debate",  "debate <vcffile> [--gene <GENE>]",
         "Run three-persona LLM debate on every VUS. Requires LLM."),
        ("config",  "config wizard | show | set <key> <value>",
         "Inspect or modify ~/.ngsagent/config.yaml."),
    ]:
        console.print(f"  [{theme['accent']}]{usage}[/{theme['accent']}]")
        console.print(f"    [{theme['muted']}]{desc}[/{theme['muted']}]")

    console.print()
    console.print(Text("Slash commands (TUI only):", style=theme["muted"]))
    for cmd, desc in [
        ("/help",   "Show this palette."),
        ("/theme",  "Switch color theme."),
        ("/files",  "Browse VCF / log / QC files in cwd."),
        ("/status", "Show config + LLM backend."),
        ("/clear",  "Clear the screen."),
        ("/exit",   "Leave the TUI. (Ctrl+D also works.)"),
    ]:
        console.print(f"  [{theme['accent']}]{cmd:<10}[/{theme['accent']}]  "
                      f"[{theme['muted']}]{desc}[/{theme['muted']}]")

    console.print()
    console.print(Text("Tips:", style=theme["muted"]))
    for tip in [
        "Up/Down arrows cycle through command history.",
        "Tab autocompletes subcommand + slash names.",
        "Any subcommand streams its output live in this window.",
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
              title=f"[{theme['panel_title']}]~/.ngsagent/config.yaml[/{theme['panel_title']}]",
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
        f"  [{theme['accent']}]Pick [][/{theme['accent']}]",
        default="",
        console=console,
    ).strip()

    if not raw:
        return None

    try:
        idx = int(raw) - 1
        if 0 <= idx < len(files):
            chosen = files[idx]
            if chosen.suffix.lower() == ".vcf":
                suggestion = f"analyze {chosen.name}"
            elif chosen.suffix.lower() == ".log":
                suggestion = f"watch {chosen.name}"
            else:
                suggestion = f"analyze {chosen.name}"
            console.print(f"  [{theme['muted']}]Suggested: [/{theme['muted']}]"
                          f"[{theme['accent']}]{suggestion}[/{theme['accent']}]")
            console.print()
            return suggestion
    except ValueError:
        pass
    return None


# ---------------------------------------------------------------------------
# Slash command dispatcher
# ---------------------------------------------------------------------------

def handle_slash(
    cmd: str,
    console: Console,
    theme: dict[str, str],
    cfg: dict[str, Any],
) -> tuple[bool, bool, dict[str, str], dict[str, Any]]:
    """Handle a slash command.

    Returns (should_continue, should_clear, theme, cfg).
    """
    name = cmd.strip().lower().split()[0]

    if name in ("/exit", "/quit"):
        console.print(f"[{theme['muted']}]Goodbye.[/{theme['muted']}]")
        return False, False, theme, cfg

    if name == "/clear":
        return True, True, theme, cfg

    if name == "/help":
        show_help(console, theme)
        return True, False, theme, cfg

    if name == "/theme":
        new_name = pick_theme(console)
        theme = THEMES[new_name]
        cfg["theme"] = new_name
        save_config(cfg)
        console.print(f"  [{theme['accent']}]Theme set to {new_name}.[/{theme['accent']}]")
        console.print()
        return True, False, theme, cfg

    if name == "/files":
        show_files(console, theme)
        return True, False, theme, cfg

    if name == "/status":
        show_status(console, theme, cfg)
        return True, False, theme, cfg

    console.print(f"  [{theme['muted']}]Unknown slash command: {name}[/{theme['muted']}]")
    console.print(f"  [{theme['muted']}]Type /help to see all commands.[/{theme['muted']}]")
    return True, False, theme, cfg


# ---------------------------------------------------------------------------
# Subprocess dispatcher — streams output live
# ---------------------------------------------------------------------------

def _resolve_ngsagent() -> list[str]:
    """Return the argv prefix to invoke ngsagent."""
    import shutil
    if shutil.which("ngsagent"):
        return ["ngsagent"]
    return [sys.executable, "-m", "ngs_agent.cli"]


def dispatch_command(line: str, console: Console, theme: dict[str, str]) -> None:
    """Run a subcommand via subprocess and stream its output live.

    Nibi reacts to the command lifecycle:
      - Before running: "analyzing" for analyze/watch, "running" for others
      - On success:     "success"
      - On error:       "error"
      - On interrupt:   "thinking"
    """
    from rich.align import Align

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        console.print(f"  [red]Parse error: {exc}[/red]")
        return

    if not tokens:
        return

    # Pick Nibi's pre-run expression based on subcommand
    subcmd = tokens[0].lower()
    pre_expr: Expression = (
        "analyzing" if subcmd in ("analyze", "watch") else
        "curious"   if subcmd == "debate" else
        "thinking"  if subcmd == "config" else
        "running"
    )

    cmd_prefix = _resolve_ngsagent()
    full_cmd = cmd_prefix + tokens

    console.print(
        f"  [{theme['muted']}]$ {' '.join(full_cmd)}[/{theme['muted']}]"
    )
    # Show Nibi with pre-run expression
    console.print(Align.center(render_nibi(theme, pre_expr)))
    console.print()

    exit_code = 0
    try:
        proc = subprocess.Popen(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            text=True,
            env={**os.environ, "FORCE_COLOR": "1"},
        )
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            console.print(raw_line, end="", highlight=False, markup=False)
        proc.wait()
        exit_code = proc.returncode

        post_expr: Expression = "success" if exit_code == 0 else "error"
        console.print()
        console.print(Align.center(render_nibi(theme, post_expr)))
        if exit_code == 0:
            console.print(f"\n  [{theme['accent']}]✓ done[/{theme['accent']}]")
        else:
            console.print(f"\n  [red]✗ exit code {exit_code}[/red]")

    except FileNotFoundError:
        console.print(
            f"  [red]Command not found: {full_cmd[0]}[/red]\n"
            "  Make sure ngs-agent is installed: pip install ngs-agent"
        )
        console.print(Align.center(render_nibi(theme, "error")))
    except KeyboardInterrupt:
        console.print()
        console.print(Align.center(render_nibi(theme, "thinking")))
        console.print(f"\n  [{theme['muted']}]Interrupted.[/{theme['muted']}]")

    console.print()


# ---------------------------------------------------------------------------
# REPL prompt
# ---------------------------------------------------------------------------

def _make_prompt_session(theme: dict[str, str]) -> Any:
    """Build a prompt_toolkit PromptSession with history + autocomplete."""
    try:
        from prompt_toolkit import PromptSession  # type: ignore[import]
        from prompt_toolkit.history import FileHistory  # type: ignore[import]
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory  # type: ignore[import]
        from prompt_toolkit.completion import WordCompleter  # type: ignore[import]

        history_path = Path.home() / ".ngsagent" / "history"
        history_path.parent.mkdir(parents=True, exist_ok=True)

        completer = WordCompleter(
            SUBCOMMANDS + SLASH_COMMANDS,
            ignore_case=True,
            sentence=True,
        )
        return PromptSession(
            history=FileHistory(str(history_path)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
        )
    except ImportError:
        return None


def read_line(
    prompt_session: Any,
    theme: dict[str, str],
    console: Console,
) -> str | None:
    """Read one line from the user. Returns None on EOF / Ctrl+D."""
    prompt_str = f"> "

    if prompt_session is not None:
        try:
            from prompt_toolkit.styles import Style  # type: ignore[import]
            pt_style = Style.from_dict({"prompt": theme["accent"].lstrip("#")})
            return prompt_session.prompt(prompt_str)
        except (EOFError, KeyboardInterrupt):
            return None
        except Exception:
            pass  # fall through to rich prompt

    # Fallback: rich / plain input
    try:
        from rich.prompt import Prompt
        return Prompt.ask(
            f"[{theme['prompt']}]>[/{theme['prompt']}]",
            console=console,
            default="",
        )
    except (EOFError, KeyboardInterrupt):
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_tui() -> None:
    console = Console(force_terminal=True, legacy_windows=False)
    cfg = load_config()

    # Resolve theme
    theme_name = cfg.get("theme", "")  # type: ignore[arg-type]
    if theme_name not in THEMES:
        theme_name = pick_theme(console)
        cfg["theme"] = theme_name
        save_config(cfg)

    theme = THEMES[theme_name]

    # Welcome screen: title first, then live Nibi eye-tracking intro,
    # then quickstart panels. Falls back to static Nibi on non-TTY / Windows.
    _render_welcome_top(console, theme)
    show_nibi_intro(console, theme, duration=8.0)
    console.print()
    _render_welcome_panels(console, theme)

    render_status_bar(console, theme, cfg)
    console.print()

    prompt_session = _make_prompt_session(theme)

    while True:
        # Refresh cfg so status bar picks up live changes
        cfg = load_config()
        render_status_bar(console, theme, cfg)

        line = read_line(prompt_session, theme, console)

        if line is None:  # EOF / Ctrl+D
            console.print(f"\n[{theme['muted']}]Goodbye.[/{theme['muted']}]")
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            should_continue, should_clear, theme, cfg = handle_slash(
                line, console, theme, cfg
            )
            if should_clear:
                render_welcome(console, theme)
            if not should_continue:
                break
            continue

        dispatch_command(line, console, theme)
