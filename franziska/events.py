"""Domain events between the assistants (ROADMAP "Agent-to-agent communication", option 2): a fact one domain states,
others act on, kept as a row in an outbox.

A producer calls `emit(type, payload)`: the row is written, then every handler of that type runs at once, in the
same process. A handler that raises leaves the row pending with its error; `dispatch()` (the nightly job) runs
pending rows again, and `failures()` lists them in the brief until one succeeds, so a missed follow-up is seen. So handlers must be idempotent: running one twice on the same event changes nothing the
second time. Handlers are plain code (rung 1), never a model.

Payloads carry ids and dates, not figures: a `money` event says which bill and which task, not how much. The privacy
class travels with the event for a future handler that might send something out.

Events so far:
- bill.paid {bill_id, task_id, due, paid_on}: finance found the payment of a tracked bill; the secretary completes
  its "Pay X" task (a repeating one moves to the next due date).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from franziska import db, obs

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    source     TEXT NOT NULL,          -- the domain that stated it: finance, secretary, mail, ...
    type       TEXT NOT NULL,          -- 'bill.paid'
    payload    TEXT NOT NULL,          -- JSON: ids and dates
    privacy    TEXT NOT NULL,          -- 'local' | 'money' | 'mail' | 'health': where its content may go
    handled_at TEXT,                   -- NULL: a handler still has to run
    error      TEXT                    -- the last handler failure
);
CREATE INDEX IF NOT EXISTS events_pending ON events (handled_at) WHERE handled_at IS NULL;
"""
KEEP_DAYS = 365

Handler = Callable[[dict], str | None]  # returns a line for the user, or None


class Events:
    def __init__(self, con: sqlite3.Connection, clock: Callable[[], datetime]):
        self.con, self.lock, self.clock = con, db.lock(), clock
        self.con.executescript(SCHEMA)
        self.handlers: dict[str, list[Handler]] = {}

    @classmethod
    def open(cls, path: Path, clock: Callable[[], datetime]) -> Events:
        return cls(db.connect(path), clock)

    @classmethod
    def memory(cls, clock: Callable[[], datetime]) -> Events:
        """A bus with no file, for a CLI or script that has no outbox of its own."""
        return cls(sqlite3.connect(":memory:", check_same_thread=False), clock)

    def on(self, type_: str, handler: Handler) -> None:
        self.handlers.setdefault(type_, []).append(handler)

    def emit(self, type_: str, payload: dict, source: str, privacy: str = "local") -> list[str]:
        """Record the event and handle it now; returns the handlers' lines."""
        with self.lock:
            cur = self.con.execute("INSERT INTO events (at, source, type, payload, privacy) VALUES (?, ?, ?, ?, ?)",
                                   (self._now(), source, type_, json.dumps(payload, sort_keys=True), privacy))
            self.con.commit()
        return self._handle(cur.lastrowid, type_, payload)

    def dispatch(self) -> int:
        """Run the handlers of every pending event again (a handler failed, or none was registered yet); returns how
        many are handled now."""
        with self.lock:
            rows = self.con.execute("SELECT id, type, payload FROM events WHERE handled_at IS NULL ORDER BY id").fetchall()
        done = 0
        for event_id, type_, payload in rows:
            self._handle(event_id, type_, json.loads(payload))
            with self.lock:
                done += self.con.execute("SELECT handled_at IS NOT NULL FROM events WHERE id = ?",
                                         (event_id,)).fetchone()[0]
        return done

    def failures(self) -> list[str]:
        """Pending events a handler failed on, one line each for the brief: the follow-up didn't happen, and the
        nightly retry hasn't fixed it yet. (An event waiting only for a handler to be registered is not listed.)"""
        with self.lock:
            rows = self.con.execute("SELECT id, at, type, error FROM events WHERE handled_at IS NULL"
                                    " AND error IS NOT NULL ORDER BY id").fetchall()
        return [f"⚠️ {type_} (event {event_id}, {at[:10]}) not handled: {error[:120]}; retried nightly"
                for event_id, at, type_, error in rows]

    def expire(self) -> int:
        """Handled events older than KEEP_DAYS go."""
        with self.lock:
            cur = self.con.execute("DELETE FROM events WHERE handled_at IS NOT NULL AND at < datetime(?, ?)",
                                   (self._now(), f"-{KEEP_DAYS} days"))
            self.con.commit()
        return cur.rowcount

    def _handle(self, event_id: int, type_: str, payload: dict) -> list[str]:
        handlers = self.handlers.get(type_, [])
        if not handlers:
            return []  # stays pending until a handler exists
        out, error = [], None
        with obs.span(f"event {type_}", event_id=event_id, payload=payload):
            for handler in handlers:
                try:
                    if line := handler(payload):
                        out.append(line)
                except Exception as e:
                    log.exception("event %s (%s): handler %s failed", event_id, type_,
                                  getattr(handler, "__name__", "?"))
                    obs.failed(e)
                    error = f"{type(e).__name__}: {e}"
        with self.lock:
            self.con.execute("UPDATE events SET handled_at = ?, error = ? WHERE id = ?",
                             (None if error else self._now(), error, event_id))
            self.con.commit()
        return out

    def _now(self) -> str:
        return self.clock().isoformat(timespec="seconds")
