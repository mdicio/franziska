"""What a conversation keeps across restarts: the pinned mode, the last route (for follow-ups) and each route's recent
turns. Before this lived only in the adapter's memory (Telegram's chat_data), so a restart forgot the conversation.

Buttons don't survive a restart: what a pending button acts on (a proposal, a message to route) holds Python
callables, so it stays in memory and an old button answers "expired".

Stored in turns.sqlite next to the turn log (the same text; never opened by the MCP server or the cloud path).
"""

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter

from franziska import db

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    chat_id    INTEGER NOT NULL,
    key        TEXT NOT NULL,      -- 'mode', 'last', 'history:<route>'
    value      TEXT NOT NULL,      -- JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
"""
LOADED = "_restored"  # set in a state dict once it was filled from the store


class Conversations:
    def __init__(self, path: Path, clock: Callable[[], datetime]):
        self.con, self.lock, self.clock = db.connect(path), db.lock(), clock
        self.con.executescript(SCHEMA)

    @db.locked
    def restore(self, chat_id: int, state: dict) -> dict:
        """Fill `state` from the store, once per process; what is already in it wins."""
        if state.get(LOADED):
            return state
        state[LOADED] = True
        for key, value in self.con.execute("SELECT key, value FROM conversations WHERE chat_id = ?", (chat_id,)):
            try:
                if key == "mode":
                    state.setdefault("mode", json.loads(value))
                elif key == "last":
                    state.setdefault("last", tuple(json.loads(value)))
                elif key.startswith("summary:"):
                    state.setdefault(("summary", key.removeprefix("summary:")), json.loads(value))
                elif key.startswith("history:"):
                    turns = [ModelMessagesTypeAdapter.validate_python(t) for t in json.loads(value)]
                    state.setdefault(("history", key.removeprefix("history:")), turns)
            except Exception:  # a pydantic-ai upgrade changed the message shape: start that part afresh
                log.warning("conversation %s: dropped unreadable %s", chat_id, key)
        return state

    @db.locked
    def save(self, chat_id: int, state: dict, route: str | None = None) -> None:
        """Write the mode, the last route and (with `route`) that route's turns."""
        rows = {"mode": state.get("mode"), "last": state.get("last")}
        if route is not None:
            turns = state.get(("history", route), [])
            rows[f"history:{route}"] = [ModelMessagesTypeAdapter.dump_python(t, mode="json") for t in turns]
            rows[f"summary:{route}"] = state.get(("summary", route)) or None
        now = self.clock().isoformat(timespec="minutes")
        for key, value in rows.items():
            if value is None:
                self.con.execute("DELETE FROM conversations WHERE chat_id = ? AND key = ?", (chat_id, key))
            else:
                self.con.execute("INSERT INTO conversations VALUES (?, ?, ?, ?) ON CONFLICT(chat_id, key) DO UPDATE"
                                 " SET value = excluded.value, updated_at = excluded.updated_at",
                                 (chat_id, key, json.dumps(value), now))
        self.con.commit()

    @db.locked
    def reset(self, chat_id: int, state: dict) -> None:
        """/new: forget the turns and summaries of every route and the last route; the pinned mode stays."""
        for key in [k for k in state if isinstance(k, tuple) and k[0] in ("history", "summary")] + ["last", "cloud_history", "context"]:
            state.pop(key, None)
        self.con.execute("DELETE FROM conversations WHERE chat_id = ? AND key != 'mode'", (chat_id,))
        self.con.commit()
