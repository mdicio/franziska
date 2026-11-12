"""market.sqlite: public data only (closes, articles, the model's reports). No quantity, weight or amount of the
user's goes in here (DESIGN decision 27), so this is the one store a cloud model could read later."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol   TEXT NOT NULL,      -- Yahoo symbol, 'FRED:<id>' or 'ECB:<key>'
    day      TEXT NOT NULL,
    close    REAL NOT NULL,
    currency TEXT NOT NULL,      -- '' for a rate or an index level
    PRIMARY KEY (symbol, day)
);
CREATE TABLE IF NOT EXISTS items (
    id        TEXT PRIMARY KEY,  -- hash of the url
    url       TEXT NOT NULL,
    title     TEXT NOT NULL,
    source    TEXT NOT NULL,     -- the publisher, e.g. 'CNBC', 'Il Sole 24 Ore'
    published TEXT NOT NULL,     -- ISO, UTC
    summary   TEXT NOT NULL,     -- the feed's description ('' when it has none)
    feed      TEXT NOT NULL,     -- the feed or query that found it
    text      TEXT,              -- the article's text (trafilatura), capped; NULL when not read or unreadable
    digest    TEXT               -- JSON: the model's typed extraction (brief.Digest)
);
CREATE TABLE IF NOT EXISTS events (
    day    TEXT NOT NULL,
    kind   TEXT NOT NULL,        -- 'central bank' | 'earnings'
    title  TEXT NOT NULL,
    source TEXT NOT NULL,        -- the page it was read from
    PRIMARY KEY (day, kind, title)
);
CREATE TABLE IF NOT EXISTS reports (
    id         INTEGER PRIMARY KEY,
    day        TEXT NOT NULL,
    edition    TEXT NOT NULL,    -- 'daily' | 'weekly'
    created_at TEXT NOT NULL,
    report     TEXT NOT NULL,    -- JSON: the model's report (brief.Written), or '' when degraded
    items      TEXT NOT NULL,    -- JSON list of item ids it cites
    degraded   TEXT NOT NULL,    -- '' or which step failed
    text       TEXT NOT NULL DEFAULT '',  -- the whole report as plain text (brief.as_text), what agents read back
    timings    TEXT NOT NULL DEFAULT ''   -- JSON: seconds per step (figures, news, pick, articles, digest, write, total)
);
"""


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=10)
    con.executescript(SCHEMA)
    columns = {r[1] for r in con.execute("PRAGMA table_info(reports)")}
    if "text" not in columns:  # reports before 2026-10-29
        con.execute("ALTER TABLE reports ADD COLUMN text TEXT NOT NULL DEFAULT ''")
    if "timings" not in columns:  # reports before 2026-10-30
        con.execute("ALTER TABLE reports ADD COLUMN timings TEXT NOT NULL DEFAULT ''")
    return con


def item_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def save_prices(db: Path, symbol: str, currency: str, rows: list[tuple[date, float]]) -> None:
    with closing(connect(db)) as con, con:
        con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?)",
                        [(symbol, d.isoformat(), v, currency) for d, v in rows])


def prices(db: Path, symbol: str, since: date) -> list[tuple[date, float]]:
    with closing(connect(db)) as con:
        rows = con.execute("SELECT day, close FROM prices WHERE symbol = ? AND day >= ? ORDER BY day",
                           (symbol, since.isoformat())).fetchall()
    return [(date.fromisoformat(d), v) for d, v in rows]


def save_events(db: Path, kind: str, source: str, rows: list[tuple[date, str]]) -> None:
    """The latest read of a calendar replaces the earlier one for the days it covers."""
    if not rows:
        return
    with closing(connect(db)) as con, con:
        con.execute("DELETE FROM events WHERE kind = ? AND source = ? AND day >= ?",
                    (kind, source, min(d for d, _ in rows).isoformat()))
        con.executemany("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?)",
                        [(d.isoformat(), kind, t, source) for d, t in rows])


def events(db: Path, start: date, end: date) -> list[tuple[date, str, str, str]]:
    with closing(connect(db)) as con:
        rows = con.execute("SELECT day, kind, title, source FROM events WHERE day >= ? AND day < ? ORDER BY day, kind",
                           (start.isoformat(), end.isoformat())).fetchall()
    return [(date.fromisoformat(d), k, t, s) for d, k, t, s in rows]


def save_report(db: Path, day: date, edition: str, created_at: str, report: dict | None, items: list[str],
                degraded: str, text: str = "", timings: dict | None = None) -> int:
    with closing(connect(db)) as con, con:
        cur = con.execute("INSERT INTO reports (day, edition, created_at, report, items, degraded, text, timings)"
                          " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (day.isoformat(), edition, created_at,
                                                               json.dumps(report) if report else "", json.dumps(items),
                                                               degraded, text, json.dumps(timings or {})))
        return cur.lastrowid


def report_text(db: Path, edition: str, day: date) -> tuple[date, str, str] | None:
    """The latest stored report of `edition` for `day` or the nearest day before it: (day, created_at, text).
    A report rerun the same day replaces the earlier one."""
    with closing(connect(db)) as con:
        row = con.execute("SELECT day, created_at, text FROM reports WHERE edition = ? AND day <= ? AND text != ''"
                          " ORDER BY day DESC, id DESC LIMIT 1", (edition, day.isoformat())).fetchone()
    return (date.fromisoformat(row[0]), row[1], row[2]) if row else None


def report_days(db: Path, edition: str, limit: int = 10) -> list[date]:
    """The most recent days with a stored report of `edition`, newest first."""
    with closing(connect(db)) as con:
        rows = con.execute("SELECT DISTINCT day FROM reports WHERE edition = ? AND text != '' ORDER BY day DESC"
                           " LIMIT ?", (edition, limit)).fetchall()
    return [date.fromisoformat(d) for d, in rows]


def read_report(db: Path, edition: str, day: date) -> str:
    """What an agent gets back for 'the market report of <day>': the stored text, or which days there are."""
    if found := report_text(db, edition, day):
        made_on, created_at, text = found
        return text if made_on == day else f"(No {edition} report of {day}; the latest before it:)\n{text}"
    days = ", ".join(f"{d}" for d in report_days(db, edition)) or "none yet"
    return f"No {edition} market report stored on or before {day}. Stored {edition} reports: {days}."
