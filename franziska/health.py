"""What is failing right now, said where the user would otherwise trust stale or partial data.

A component (the router, the calendar sync, the mail fetch, the event handlers) reports `ok` after it worked and
`failed` when it didn't. While it is failing, `problems` gives one line for it ('⚠️ calendar: not synced since 14:05
(ConnectError …)'), which the brief, /today and every reply that relied on it show. Nothing is retried or hidden here:
the job that failed keeps its own schedule; this only makes the failure visible until the next success clears it.

One small table in data/health.sqlite, so the bot, the web UI and the CLIs see the same state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from franziska import db, obs

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS health (
    component TEXT PRIMARY KEY,
    last_ok   TEXT,             -- local time of the last success
    failing   TEXT,             -- local time of the first failure since then; NULL while it works
    error     TEXT              -- the last failure, short
);
"""
# What each component's failure means for the user, so the line says what to distrust.
MEANS = {
    "router": "you pick the assistant for each message",
    "calendar": "appointments are from the last sync",
    "mail": "no new mail is fetched",
    "backup": "last night's backup failed",
    "housekeeping": "old data is not being cleaned up",
    "consolidate": "nothing new is being learned from conversations",
}


class Health:
    def __init__(self, path: Path, clock: Callable[[], datetime]):
        self.con, self.lock, self.clock = db.connect(path), db.lock(), clock
        self.con.executescript(SCHEMA)

    @db.locked
    def ok(self, component: str) -> None:
        was = self.con.execute("SELECT failing FROM health WHERE component = ?", (component,)).fetchone()
        if was and was[0]:
            log.info("health: %s works again (failing since %s)", component, was[0])
        self.con.execute("INSERT INTO health (component, last_ok) VALUES (?, ?) ON CONFLICT(component) DO UPDATE"
                         " SET last_ok = excluded.last_ok, failing = NULL, error = NULL", (component, self._now()))
        self.con.commit()

    @db.locked
    def failed(self, component: str, error: BaseException | str) -> None:
        text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        obs.failed(f"{component}: {text}")
        self.con.execute("INSERT INTO health (component, failing, error) VALUES (?, ?, ?) ON CONFLICT(component)"
                         " DO UPDATE SET failing = COALESCE(health.failing, excluded.failing), error = excluded.error",
                         (component, self._now(), " ".join(text.split())[:160]))
        self.con.commit()

    @db.locked
    def problems(self, components: Iterable[str] | None = None) -> list[str]:
        """One line per failing component (all, or those given), oldest failure first."""
        rows = self.con.execute("SELECT component, last_ok, failing, error FROM health WHERE failing IS NOT NULL"
                                " ORDER BY failing").fetchall()
        wanted = set(components) if components is not None else None
        out = []
        for component, last_ok, failing, error in rows:
            if wanted is not None and component not in wanted:
                continue
            since = f"last worked {_when(last_ok)}" if last_ok else f"failing since {_when(failing)}"
            means = f"; {MEANS[component]}" if component in MEANS else ""
            out.append(f"⚠️ {component}: {since}{means} ({error})")
        return out

    def _now(self) -> str:
        return self.clock().isoformat(timespec="minutes")


def _when(stamp: str) -> str:
    """'2026-10-28T14:05' → '28/10 14:05'."""
    return f"{stamp[8:10]}/{stamp[5:7]} {stamp[11:16]}"


def footer(lines: list[str]) -> str:
    """The lines to append under a reply, or ''."""
    return "\n\n" + "\n".join(lines) if lines else ""
