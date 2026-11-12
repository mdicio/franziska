"""SQLite as the stores open it (tasks, notes, calendar mirror, mail triage, turn log).

Each store keeps one connection for the life of the process, and the bot uses it from several threads at once: agent
tools, the jobs, workflows, button taps. Python's sqlite3 objects break when two threads use one connection together
(seen: "bad parameter or other API misuse" on a model's parallel tool calls), so every store method that touches the
connection holds the store's own lock. The ledger is different: it opens a connection per call (finance/ledger.py).
"""

import functools
import sqlite3
import threading
from pathlib import Path


def connect(path: Path) -> sqlite3.Connection:
    """WAL and a busy timeout: the bot, the MCP server and the CLIs may open the same file at once."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def lock() -> threading.RLock:
    """A store's lock. Reentrant, so a locked method may call another (complete → get)."""
    return threading.RLock()


def locked(method):
    """The method runs alone on the store's connection (`self.lock`)."""
    @functools.wraps(method)
    def run(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return run
