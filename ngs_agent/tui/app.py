"""Main TUI App — Claude Code-style interface for NGS-Agent, powered by Nibi.

Layout (top to bottom):
  ┌─────────────────────────────────────────────────────────┐
  │ Header: ngsagent — Nibi                                 │
  ├─────────────────────────────────────────────────────────┤
  │                                                         │
  │  WelcomePanel (orange-bordered, Nibi + tips + recent)   │
  │   OR                                                    │
  │  MessageLog (chat history once session starts)          │
  │                                                         │
  ├─────────────────────────────────────────────────────────┤
  │ PermissionPrompt (only when needed)                     │
  ├─────────────────────────────────────────────────────────┤
  │ ❯ InputBar (Ask Nibi anything...)                       │
  ├─────────────────────────────────────────────────────────┤
  │ HintsBar (/help  shift+tab  ctrl+l  ...)                │
  ├─────────────────────────────────────────────────────────┤
  │ ( o.o ) StatusBar: model | session | mode | ctx         │
  └─────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from .. import __version__
from ..agents.definitions import AGENTS, get_agent
from ..backends.base import NoBackend
from ..backends.factory import get_backend
from ..config import load_config
from ..runtime.file_tracker import FileTracker
from ..runtime.loop import RunOptions, run as agent_run
from ..runtime.session import SessionStore
from ..tools.bundle import build_registry
from .slash_commands import CommandResult, handle_slash
from .state import AppState, PendingPermission
from .widgets.hints_bar import HintsBar
from .widgets.input_bar import InputBar
from .widgets.message_log import MessageLog
from .widgets.permission_prompt import PermissionPrompt
from .widgets.status_bar import StatusBar
from .widgets.welcome_panel import WelcomePanel


class NgsAgentTUI(App):
    """Claude Code-style TUI for NGS-Agent, powered by Nibi."""

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #welcome-panel {
        height: 1fr;
        layer: below;
    }
    #welcome-panel.hidden {
        display: none;
    }
    #msg-log {
        height: 1fr;
        border: solid #ff8c00;
        background: $surface;
        padding: 0 1;
    }
    #msg-log.hidden {
        display: none;
    }
    #input-bar {
        dock: bottom;
        height: 3;
        layer: above;
    }
    #hints-bar {
        dock: bottom;
        height: 1;
        layer: above;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        layer: above;
    }
    #permission-prompt {
        dock: bottom;
        height: auto;
        background: $warning 20%;
        border-top: solid $warning;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("shift+tab", "cycle_mode", "Cycle mode", show=True),
        Binding("ctrl+l", "clear_log", "Clear log", show=True),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.state = AppState()
        self._store = SessionStore()
        self._file_tracker = FileTracker()
        self._permission_event: asyncio.Event | None = None
        self._permission_response: bool = False
        self._prior_messages = []
        self._current_run_task: asyncio.Task | None = None
        # Track whether the user has started chatting (controls welcome panel visibility)
        self._chat_started = False

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield Header(name="ngsagent \u2014 Nibi", icon="\U0001F9EC")
        yield WelcomePanel(id="welcome-panel")
        yield MessageLog(id="msg-log", classes="hidden")
        yield PermissionPrompt(id="permission-prompt")
        yield InputBar(id="input-bar")
        yield HintsBar(id="hints-bar")
        yield StatusBar(self.state, id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        # Initial setup
        cfg = load_config()
        backend = get_backend(cfg.to_dict())

        if isinstance(backend, NoBackend):
            # Show error in the welcome panel itself (status line area)
            self.state.model = "(no backend)"
        else:
            self.state.model = cfg.anthropic_model if cfg.llm == "anthropic" else cfg.openai_model

        # Render the welcome panel with current state
        welcome = self.query_one("#welcome-panel", WelcomePanel)
        welcome.refresh_panel(
            version=__version__,
            model=self.state.model,
            agent=self.state.agent,
            cwd=os.getcwd(),
            expression="happy",
        )

        self.query_one("#status-bar", StatusBar).refresh_status()
        # Focus the input bar so the user can start typing immediately
        self.query_one("#input-bar", InputBar).focus()

    def _hide_welcome_show_log(self) -> None:
        """Hide the welcome panel and show the message log."""
        if self._chat_started:
            return
        self._chat_started = True
        welcome = self.query_one("#welcome-panel", WelcomePanel)
        log = self.query_one("#msg-log", MessageLog)
        welcome.add_class("hidden")
        log.remove_class("hidden")

        # Add an opening system message to the log
        from ..nibi import TAGLINE
        log.add_system(
            f"NGS-Agent v{__version__} \u2014 Nibi is ready to analyze your genomic data!"
        )
        log.add_system(f"{TAGLINE}")

        # If no backend was configured, surface the error in the log
        cfg = load_config()
        backend = get_backend(cfg.to_dict())
        if isinstance(backend, NoBackend):
            log.add_error(
                "No LLM backend configured. Run 'ngsagent config wizard' or set ANTHROPIC_API_KEY."
            )
            log.add_system(
                "The TUI will still work for slash commands, but you won't be able to "
                "send prompts until an LLM is configured."
            )

    # ---------- bindings ----------
    def action_cycle_mode(self) -> None:
        new_mode = self.state.cycle_permission_mode()
        # If chat hasn't started, also reflect the change in the welcome panel
        if not self._chat_started:
            welcome = self.query_one("#welcome-panel", WelcomePanel)
            welcome.refresh_panel(
                version=__version__,
                model=self.state.model,
                agent=self.state.agent,
                cwd=os.getcwd(),
                expression="thinking",
            )
        else:
            log = self.query_one("#msg-log", MessageLog)
            log.add_system(f"Permission mode \u2192 {new_mode}")
        self.query_one("#status-bar", StatusBar).refresh_status()

    def action_clear_log(self) -> None:
        # If chat hasn't started, no log to clear
        if not self._chat_started:
            return
        log = self.query_one("#msg-log", MessageLog)
        log.clear()
        log.add_system("Log cleared.")

    # ---------- input handling ----------
    def on_input_submitted(self, event: InputBar.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return

        input_bar = self.query_one("#input-bar", InputBar)
        input_bar.value = ""
        input_bar.push_history(text)

        # Handle slash command
        if text.startswith("/"):
            # Slash commands also need the message log visible
            self._hide_welcome_show_log()
            result = handle_slash(text, self.state)
            self._apply_command_result(result)
            return

        # Regular prompt — switch from welcome panel to chat log
        self._hide_welcome_show_log()
        log = self.query_one("#msg-log", MessageLog)
        log.add_user_message(text)
        self.state.is_running = True
        self.query_one("#status-bar", StatusBar).refresh_status()

        # Launch the agent run in the background
        self._run_agent(text)

    def _apply_command_result(self, result: CommandResult) -> None:
        log = self.query_one("#msg-log", MessageLog)
        if result.message:
            log.add_system(result.message)
        if result.clear_log:
            log.clear()
            log.add_system(result.message or "Log cleared.")
        if result.exit_app:
            self.exit()
        if result.new_model:
            self.state.model = result.new_model
        if result.new_agent:
            self.state.agent = result.new_agent
        if result.resume_session:
            self._resume_session(result.resume_session)
        if result.fork_session:
            self._fork_session(result.fork_session)
        self.query_one("#status-bar", StatusBar).refresh_status()

    # ---------- session management ----------
    def _resume_session(self, session_id: str) -> None:
        info = self._store.get(session_id)
        if not info:
            self.query_one("#msg-log", MessageLog).add_error(
                f"Session not found: {session_id}"
            )
            return
        self.state.session_id = session_id
        self.state.model = info.model
        self.state.agent = info.agent
        self._prior_messages = self._store.load_messages(session_id)
        log = self.query_one("#msg-log", MessageLog)
        log.add_system(
            f"Resumed session {session_id} ({len(self._prior_messages)} messages)."
        )
        # Replay messages to the log
        for m in self._prior_messages:
            if m.role == "user":
                log.add_user_message(m.content)
            elif m.role == "assistant" and m.content:
                log.add_assistant_chunk(m.content)
                log.add_assistant_done()
        self.query_one("#status-bar", StatusBar).refresh_status()

    def _fork_session(self, session_id: str) -> None:
        info = self._store.get(session_id)
        if not info:
            self.query_one("#msg-log", MessageLog).add_error(
                f"Session not found: {session_id}"
            )
            return
        new_id = self._store.create(info.agent, info.model, os.getcwd(), forked_from=session_id)
        self._prior_messages = self._store.load_messages(session_id)
        # Persist copies of the prior messages to the new session
        for m in self._prior_messages:
            self._store.append_message(new_id, m)
        self.state.session_id = new_id
        self.query_one("#msg-log", MessageLog).add_system(
            f"Forked session {session_id} → {new_id}"
        )
        self.query_one("#status-bar", StatusBar).refresh_status()

    # ---------- agent run ----------
    @work(exclusive=True, name="agent_run")
    async def _run_agent(self, prompt: str) -> None:
        """Run the agent loop in a background task, streaming events to the TUI."""
        agent_def = get_agent(self.state.agent)
        if agent_def is None:
            self.query_one("#msg-log", MessageLog).add_error(
                f"Unknown agent: {self.state.agent}"
            )
            self.state.is_running = False
            return

        cfg = load_config()
        backend = get_backend(cfg.to_dict())
        if isinstance(backend, NoBackend):
            self.query_one("#msg-log", MessageLog).add_error(
                "No LLM backend configured. Run 'ngsagent config wizard'."
            )
            self.state.is_running = False
            return

        # Create session if needed
        if not self.state.session_id:
            self.state.session_id = self._store.create(
                agent_def.name, self.state.model, os.getcwd()
            )
            log = self.query_one("#msg-log", MessageLog)
            log.add_session_info(self.state.session_id, self.state.model, agent_def.name)
            self.query_one("#status-bar", StatusBar).refresh_status()

        registry = build_registry(agent_def.tools)

        # Wire up callbacks
        log = self.query_one("#msg-log", MessageLog)

        def on_text(text: str) -> None:
            log.add_assistant_chunk(text)

        def on_tool_call_start(tc_id: str, name: str, args: dict) -> None:
            log.add_tool_call_start(tc_id, name, args)

        def on_tool_result(tc_id: str, content: str, is_error: bool) -> None:
            name = "tool"
            log.add_tool_result(tc_id, name, content, is_error)

        def on_context(budget) -> None:
            self.state.context_budget = budget
            self.query_one("#status-bar", StatusBar).refresh_status()

        def on_event(evt) -> None:
            if evt.type == "usage":
                self.state.total_input_tokens += evt.payload.get("input_tokens", 0)
                self.state.total_output_tokens += evt.payload.get("output_tokens", 0)
                log.add_usage(
                    evt.payload.get("input_tokens", 0),
                    evt.payload.get("output_tokens", 0),
                )
            elif evt.type == "compaction":
                log.add_compaction(evt.payload.get("reason", ""))

        async def permission_callback(session_id: str, tool: str, args: dict) -> bool:
            """Async — called from the agent loop (same event loop)."""
            self.state.pending_permission = PendingPermission(
                session_id=session_id, tool=tool, args=args,
            )
            future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
            self._permission_future = future

            # Show the prompt
            prompt_widget = self.query_one("#permission-prompt", PermissionPrompt)
            prompt_widget.show_request(tool, args)

            # Wait for the user's response (with 5-min timeout)
            try:
                response = await asyncio.wait_for(future, timeout=300)
                return response
            except TimeoutError:
                log.add_error("Permission timed out after 5 minutes.")
                return False
            finally:
                self.state.pending_permission = None
                prompt_widget.hide()

        options = RunOptions(
            session_id=self.state.session_id,
            model=self.state.model,
            system_prompt=agent_def.system_prompt,
            cwd=os.getcwd(),
            max_turns=agent_def.max_turns,
            permission_mode=self.state.permission_mode,
            betas=agent_def.betas,
            file_tracker=self._file_tracker,
            on_text=on_text,
            on_tool_call_start=on_tool_call_start,
            on_tool_result=on_tool_result,
            on_context=on_context,
            on_event=on_event,
            permission_callback=permission_callback,
        )

        # Run the agent
        try:
            result = await agent_run(
                prompt=prompt,
                backend=backend,
                registry=registry,
                options=options,
                prior_messages=self._prior_messages if self._prior_messages else None,
            )
        except Exception as e:
            log.add_error(f"Agent run failed: {e}")
            self.state.is_running = False
            self.query_one("#status-bar", StatusBar).refresh_status()
            return

        # Persist new messages
        new_messages = result.messages[len(self._prior_messages):]
        for m in new_messages:
            self._store.append_message(self.state.session_id, m)

        # Update state
        self.state.is_running = False
        self.state.turns_completed += result.turns
        self.state.total_input_tokens += result.total_input_tokens
        self.state.total_output_tokens += result.total_output_tokens
        self._prior_messages = result.messages

        # Finalize the log
        log.add_assistant_done()
        if result.error:
            log.add_error(result.error)
        log.add_system(
            f"Done. Turns={result.turns} | in={result.total_input_tokens} out={result.total_output_tokens} | {result.finish_reason}",
        )
        self.query_one("#status-bar", StatusBar).refresh_status()

    # ---------- permission prompt handling ----------
    def on_key(self, event) -> None:
        """Handle Y/N/A for permission prompts."""
        prompt = self.query_one("#permission-prompt", PermissionPrompt)
        if not prompt.is_visible:
            return
        if self.query_one("#input-bar", InputBar).has_focus:
            return  # don't intercept input-bar keys

        key = event.key.lower()
        if key in ("y", "n", "a", "escape"):
            event.prevent_default()
            response = key in ("y", "a") and key != "escape"
            future = getattr(self, "_permission_future", None)
            if future and not future.done():
                # Set the result from the main loop (we're already on the UI thread)
                future.set_result(response)
            prompt.hide()
