"""SQLite session store — ported from OpenCode's session.Service pattern.

Persists sessions, messages, and events. Enables:
  ngsagent --resume <id>
  ngsagent --continue
  ngsagent --fork <id> "try a different approach"
  ngsagent session list / show / export / delete
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .messages import Message, ToolCall, ToolResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    cwd TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    forked_from TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    tool_calls TEXT,           -- JSON
    tool_results TEXT,         -- JSON
    reasoning TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
"""


@dataclass
class SessionInfo:
    id: str
    title: str | None
    agent: str
    model: str
    cwd: str
    created_at: float
    updated_at: float
    forked_from: str | None


class SessionStore:
    def __init__(self, path: Path | None = None):
        self.path = path or Path.home() / ".ngsagent" / "sessions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---------- sessions ----------
    def create(
        self,
        agent: str,
        model: str,
        cwd: str,
        forked_from: str | None = None,
    ) -> str:
        sid = f"sess_{uuid.uuid4().hex[:16]}"
        now = time.time()
        self.conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
            (sid, None, agent, model, cwd, now, now, forked_from),
        )
        self.conn.commit()
        return sid

    def set_title(self, sid: str, title: str) -> None:
        self.conn.execute(
            "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
            (title[:200], time.time(), sid),
        )
        self.conn.commit()

    def get(self, sid: str) -> SessionInfo | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if not row:
            return None
        return SessionInfo(**dict(row))

    def list(self, limit: int = 50) -> list[SessionInfo]:
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [SessionInfo(**dict(r)) for r in rows]

    def delete(self, sid: str) -> None:
        self.conn.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        self.conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        self.conn.commit()

    # ---------- messages ----------
    def append_message(
        self,
        sid: str,
        msg: Message,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        mid = f"msg_{uuid.uuid4().hex[:12]}"
        tool_calls_json = json.dumps(
            [tc.__dict__ for tc in msg.tool_calls]
        ) if msg.tool_calls else None
        tool_results_json = json.dumps(
            [tr.__dict__ for tr in msg.tool_results]
        ) if msg.tool_results else None
        self.conn.execute(
            """INSERT INTO messages
               (id, session_id, role, content, tool_calls, tool_results, reasoning, tokens_in, tokens_out, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (mid, sid, msg.role, msg.content, tool_calls_json, tool_results_json,
             msg.reasoning, tokens_in, tokens_out, time.time()),
        )
        self.conn.execute(
            "UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), sid)
        )
        self.conn.commit()

    def load_messages(self, sid: str) -> list[Message]:
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at",
            (sid,),
        ).fetchall()
        messages: list[Message] = []
        for r in rows:
            tool_calls = []
            if r["tool_calls"]:
                for tc in json.loads(r["tool_calls"]):
                    tool_calls.append(ToolCall(**tc))
            tool_results = []
            if r["tool_results"]:
                for tr in json.loads(r["tool_results"]):
                    tool_results.append(ToolResult(**tr))
            messages.append(
                Message(
                    role=r["role"],
                    content=r["content"],
                    tool_calls=tool_calls,
                    tool_results=tool_results,
                    reasoning=r["reasoning"],
                )
            )
        return messages

    def most_recent(self, cwd: str | None = None) -> SessionInfo | None:
        if cwd:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE cwd=? ORDER BY updated_at DESC LIMIT 1",
                (cwd,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return SessionInfo(**dict(row)) if row else None
