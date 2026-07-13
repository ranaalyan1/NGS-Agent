"""Slash commands — handle /help /clear /model /mode /tools /session /context /compact /resume /fork /agent /quit /doctor.

Each command returns a tuple (handled: bool, message: str). If handled=False,
the input is treated as a regular prompt and sent to the agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..agents.definitions import AGENTS
from ..runtime.context import context_window_for
from .widgets.input_bar import SLASH_COMMANDS


@dataclass
class CommandResult:
    handled: bool
    message: str = ""
    exit_app: bool = False
    clear_log: bool = False
    force_compact: bool = False
    new_model: str | None = None
    new_agent: str | None = None
    resume_session: str | None = None
    fork_session: str | None = None
    show_help: bool = False


def handle_slash(input_text: str, state: Any) -> CommandResult:
    """Parse and handle a slash command. Returns CommandResult."""
    text = input_text.strip()
    if not text.startswith("/"):
        return CommandResult(handled=False)

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/help":
        help_text = "\n".join(f"  {c:<12} {d}" for c, d in SLASH_COMMANDS)
        return CommandResult(
            handled=True,
            message=f"[bold]Slash commands:[/bold]\n{help_text}",
            show_help=True,
        )

    if cmd == "/clear":
        return CommandResult(handled=True, clear_log=True, message="Log cleared.")

    if cmd == "/quit" or cmd == "/exit":
        return CommandResult(handled=True, exit_app=True, message="Goodbye.")

    if cmd == "/model":
        if arg:
            return CommandResult(handled=True, new_model=arg, message=f"Model → {arg}")
        return CommandResult(
            handled=True,
            message=f"Current model: [cyan]{state.model}[/cyan]\n"
            f"Context window: {context_window_for(state.model):,} tokens\n"
            f"Use [bold]/model <name>[/bold] to switch.",
        )

    if cmd == "/mode":
        new_mode = state.cycle_permission_mode()
        return CommandResult(handled=True, message=f"Permission mode → [yellow]{new_mode}[/yellow]")

    if cmd == "/tools":
        # List tools from the agent's registry
        agent_def = AGENTS.get(state.agent)
        if agent_def:
            tools_list = "\n".join(f"  • {t}" for t in agent_def.tools)
            return CommandResult(
                handled=True,
                message=f"[bold]Tools for agent '{state.agent}':[/bold]\n{tools_list}",
            )
        return CommandResult(handled=True, message=f"Unknown agent: {state.agent}")

    if cmd == "/session":
        return CommandResult(
            handled=True,
            message=(
                f"Session ID: [cyan]{state.session_id or 'none'}[/cyan]\n"
                f"Agent: [bold]{state.agent}[/bold]\n"
                f"Model: [cyan]{state.model}[/cyan]\n"
                f"Turns: {state.turns_completed}\n"
                f"Tokens in: {state.total_input_tokens} | out: {state.total_output_tokens}"
            ),
        )

    if cmd == "/context":
        if not state.context_budget:
            return CommandResult(handled=True, message="No context budget yet — run a turn first.")
        b = state.context_budget
        return CommandResult(
            handled=True,
            message=(
                f"[bold]Context window:[/bold] {b.context_window:,} tokens\n"
                f"  System:     {b.system_tokens:,}\n"
                f"  Messages:   {b.message_tokens:,}\n"
                f"  Tool defs:  {b.tool_def_tokens:,}\n"
                f"  Reserved:   {b.reserved_tokens:,}\n"
                f"  Used:       {b.used_ratio * 100:.1f}%\n"
                f"  Status:     {'⚠ compact soon' if b.should_compact else 'ok'}"
            ),
        )

    if cmd == "/compact":
        return CommandResult(handled=True, force_compact=True, message="Forcing compaction on next turn…")

    if cmd == "/resume":
        if not arg:
            return CommandResult(handled=True, message="Usage: /resume <session-id>")
        return CommandResult(handled=True, resume_session=arg, message=f"Resuming session {arg}…")

    if cmd == "/fork":
        if not arg:
            return CommandResult(handled=True, message="Usage: /fork <session-id>")
        return CommandResult(handled=True, fork_session=arg, message=f"Forking session {arg}…")

    if cmd == "/agent":
        if arg:
            if arg in AGENTS:
                return CommandResult(handled=True, new_agent=arg, message=f"Agent → {arg}")
            return CommandResult(
                handled=True,
                message=f"Unknown agent: {arg}. Available: {', '.join(AGENTS.keys())}",
            )
        return CommandResult(
            handled=True,
            message=f"Current agent: [bold]{state.agent}[/bold]\n"
            f"Available: {', '.join(AGENTS.keys())}",
        )

    if cmd == "/doctor":
        return CommandResult(
            handled=True,
            message=(
                f"[bold]NGS-Agent doctor[/bold]\n"
                f"  Python: {__import__('sys').version.split()[0]}\n"
                f"  Agent: {state.agent}\n"
                f"  Model: {state.model}\n"
                f"  Session: {state.session_id or 'none'}\n"
                f"  Mode: {state.permission_mode}\n"
                f"  Turns: {state.turns_completed}\n"
                f"  Tokens: in={state.total_input_tokens} out={state.total_output_tokens}"
            ),
        )

    # Unknown slash command
    return CommandResult(
        handled=True,
        message=f"Unknown command: {cmd}. Try /help.",
    )
