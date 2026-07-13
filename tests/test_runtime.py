"""Tests for the runtime: loop, context, compactor, session, permission."""
import asyncio
import json

from ngs_agent.backends.base import StubBackend
from ngs_agent.runtime.compactor import Compactor
from ngs_agent.runtime.context import (
    clear_session_override,
    context_window_for,
    estimate_tokens,
    set_session_override,
    supports_1m,
)
from ngs_agent.runtime.file_tracker import FileTracker
from ngs_agent.runtime.loop import RunOptions
from ngs_agent.runtime.loop import run as agent_run
from ngs_agent.runtime.messages import Message, StreamEvent, ToolCall, ToolResult
from ngs_agent.runtime.permission import PermissionPolicy
from ngs_agent.runtime.session import SessionStore
from ngs_agent.tools.base import BaseTool, ToolInfo, ToolResponse
from ngs_agent.tools.registry import Registry


# ---------- context ----------
def test_context_window_known_models():
    assert context_window_for("claude-sonnet-4-20250514") == 200_000
    assert context_window_for("anthropic/claude-sonnet-4-20250514") == 200_000
    assert context_window_for("gpt-4o") == 128_000
    assert context_window_for("gemini-2.0-flash") == 1_000_000


def test_context_window_1m_beta():
    assert context_window_for("claude-sonnet-4", betas=["context-1m-2025-08-07"]) == 1_000_000
    assert supports_1m("claude-sonnet-4")
    assert not supports_1m("llama3.2")


def test_context_window_fallback():
    assert context_window_for("unknown-model-xyz") == 128_000


def test_context_window_session_override():
    set_session_override("custom-model", 50_000)
    assert context_window_for("custom-model") == 50_000
    clear_session_override("custom-model")
    assert context_window_for("custom-model") == 128_000


def test_context_window_env_override(monkeypatch):
    monkeypatch.setenv("NGSAGENT_MAX_CONTEXT_TOKENS", "999999")
    assert context_window_for("anything") == 999999


def test_estimate_tokens():
    assert estimate_tokens("hello world") >= 1
    assert estimate_tokens("") >= 1  # never zero


# ---------- permission ----------
def test_permission_yolo_allows_all():
    p = PermissionPolicy(mode="yolo")
    assert p.decide("bash", {"cmd": "rm -rf /"}).allow


def test_permission_plan_denies_all():
    p = PermissionPolicy(mode="plan")
    d = p.decide("vcf_parse", {})
    assert not d.allow
    assert "plan mode" in (d.deny_reason or "").lower()


def test_permission_auto_allows_read_only():
    p = PermissionPolicy(mode="auto")
    assert p.decide("vcf_parse", {}).allow
    assert p.decide("gnomad_query", {}).allow


def test_permission_auto_asks_for_bash():
    p = PermissionPolicy(mode="auto")
    d = p.decide("bash", {"cmd": "ls"})
    assert not d.allow
    assert d.ask_prompt


def test_permission_unknown_tool_defaults_ask():
    p = PermissionPolicy(mode="auto")
    d = p.decide("some_unknown_tool", {})
    assert not d.allow


# ---------- file_tracker ----------
def test_file_tracker_detects_changes(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    tracker = FileTracker()
    tracker.record_read(str(f))
    # No change → ok
    ok, _ = tracker.check_write(str(f))
    assert ok
    # Change the file
    f.write_text("changed content")
    ok, reason = tracker.check_write(str(f))
    assert not ok
    assert "Refusing to write" in reason


def test_file_tracker_untracked_file_ok(tmp_path):
    f = tmp_path / "untracked.txt"
    f.write_text("x")
    tracker = FileTracker()
    ok, _ = tracker.check_write(str(f))
    assert ok


# ---------- session store ----------
def test_session_store_create_load(tmp_path):
    store = SessionStore(tmp_path / "test_sessions.db")
    sid = store.create("interpreter", "claude-sonnet-4", "/tmp")
    assert sid.startswith("sess_")

    store.append_message(sid, Message.user("hello"))
    store.append_message(sid, Message.assistant("hi", [ToolCall.new("vcf_parse", {"path": "x.vcf"})]))

    msgs = store.load_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"
    assert msgs[1].role == "assistant"
    assert msgs[1].tool_calls[0].name == "vcf_parse"

    store.set_title(sid, "Test session")
    info = store.get(sid)
    assert info.title == "Test session"

    sessions = store.list()
    assert len(sessions) >= 1
    store.delete(sid)
    assert store.get(sid) is None


def test_session_store_tool_results_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "tr.db")
    sid = store.create("test", "m", "/")
    msg = Message.with_tool_results([
        ToolResult(tool_call_id="call_1", name="gnomad_query",
                   content="AF=0.001", is_error=False,
                   metadata={"gnomad": {"af": 0.001}})
    ])
    store.append_message(sid, msg)
    msgs = store.load_messages(sid)
    assert msgs[0].role == "tool"
    assert msgs[0].tool_results[0].name == "gnomad_query"
    assert msgs[0].tool_results[0].content == "AF=0.001"
    assert msgs[0].tool_results[0].metadata["gnomad"]["af"] == 0.001


# ---------- agent loop with stub backend ----------
class AddTool(BaseTool):
    def info(self) -> ToolInfo:
        return ToolInfo(
            name="add",
            description="Add two numbers",
            parameters={
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            required=["a", "b"],
        )

    async def run(self, params, ctx):
        return ToolResponse(content=f"Result: {params['a'] + params['b']}")


def test_agent_loop_simple_text():
    """Stub backend returns just text — loop should stop after 1 turn."""
    backend = StubBackend(turns=[[
        StreamEvent(kind="text", text="Hello, world."),
        StreamEvent(kind="done", finish_reason="end_turn"),
        StreamEvent(kind="usage", usage={"input_tokens": 10, "output_tokens": 5}),
    ]])
    registry = Registry()
    options = RunOptions(
        session_id="test", model="claude-sonnet-4",
        system_prompt="You are a test agent.",
    )
    result = asyncio.run(agent_run("hi", backend, registry, options))
    assert result.turns == 1
    assert result.finish_reason == "end_turn"
    assert result.total_input_tokens == 10


def test_agent_loop_with_tool_call():
    """Stub backend calls add(2, 3), then on second turn emits text."""
    backend = StubBackend(turns=[
        # Turn 1: tool call
        [
            StreamEvent(kind="tool_call_start", tool_call_id="c1", tool_call_name="add"),
            StreamEvent(kind="tool_call_delta", tool_call_id="c1", tool_call_arguments_delta=json.dumps({"a": 2, "b": 3})),
            StreamEvent(kind="tool_call_end", tool_call_id="c1", tool_call_name="add"),
            StreamEvent(kind="done", finish_reason="tool_use"),
            StreamEvent(kind="usage", usage={"input_tokens": 50, "output_tokens": 10}),
        ],
        # Turn 2: final text
        [
            StreamEvent(kind="text", text="The sum is 5."),
            StreamEvent(kind="done", finish_reason="end_turn"),
            StreamEvent(kind="usage", usage={"input_tokens": 60, "output_tokens": 8}),
        ],
    ])
    registry = Registry()
    registry.register(AddTool())
    options = RunOptions(
        session_id="test", model="claude-sonnet-4",
        system_prompt="You are a test agent.",
        permission_mode="yolo",
    )
    result = asyncio.run(agent_run("what is 2+3?", backend, registry, options))
    assert result.turns == 2
    assert "The sum is 5" in result.messages[-1].content
    # Check tool result was added
    tool_msgs = [m for m in result.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "Result: 5" in tool_msgs[0].tool_results[0].content


# ---------- registry partitioning ----------
def test_registry_partition_excludes_deferred():
    class EagerTool(BaseTool):
        def info(self):
            return ToolInfo(name="eager", description="eager", parameters={})
        async def run(self, p, c):
            return ToolResponse(content="ok")

    class DeferredTool(BaseTool):
        def info(self):
            return ToolInfo(name="deferred", description="deferred", parameters={}, deferred=True)
        async def run(self, p, c):
            return ToolResponse(content="ok")

    reg = Registry()
    reg.register(EagerTool())
    reg.register(DeferredTool())

    # Default partition: eager only
    exposed = reg.partition_for_run()
    assert len(exposed) == 1
    assert exposed[0].info().name == "eager"

    # With loaded: both
    exposed = reg.partition_for_run(loaded={"deferred"})
    assert len(exposed) == 2


# ---------- compactor ----------
def test_compactor_skips_short_sessions():
    """Don't compact sessions shorter than 8 messages."""
    backend = StubBackend()
    compactor = Compactor(backend, "claude-sonnet-4")
    msgs = [Message.user(f"msg {i}") for i in range(3)]
    out = asyncio.run(compactor.maybe_compact(msgs, []))
    assert out == msgs  # unchanged


def test_compactor_calibrate():
    backend = StubBackend()
    compactor = Compactor(backend, "claude-sonnet-4")
    compactor.calibrate(estimated=1000, real_input_tokens=1200)
    assert compactor._calibration == 1.2
