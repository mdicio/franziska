"""The turn log: one row per answered message, in data/turns.sqlite (DESIGN §6, "Past conversations").

One record for three readers: evals and debugging (route, p, tool calls, guard stops, latency), and the agents,
which search it through memory's `past_conversations` and read the chat's latest turns as its thread
(Core._thread), each only the rows of the routes privacy.py lets it see. It holds money figures and mail content, so
it is its own file: the MCP server and the cloud path never open it. Turns older than KEEP_DAYS are deleted by the
nightly job.

**Search** (`search`) is the library's recipe on turns: words (FTS5, prefixes, accents folded) fused by rank with
meaning (one vector per turn from the CPU embedding server, filled in the background by `fill` like the library's),
so 'sore throat' finds 'mal di gola' and 'il lavandino' finds 'l'idraulico'. Without the server, words only.
"""

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from franziska import db, fts

log = logging.getLogger(__name__)

KEEP_DAYS = 365  # the most a turn is kept, digested or not
RAW_DAYS = 90  # a turn older than this goes once consolidate.py has summarised its week (a turn log without a model
               # up for a year still ages out at KEEP_DAYS)
RESULT_CHARS = 2500  # of each tool result kept in `tools`
HANDED_OFF = "hand_off"  # `stopped` of a run that passed the message to another assistant: 'hand_off → mail: …'

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,      -- local time, minutes
    chat_id INTEGER NOT NULL,
    route   TEXT NOT NULL,      -- life | money | mail | chat | cloud | <workflow name>
    p       REAL,               -- router probability; NULL when pinned or a button chose
    text    TEXT NOT NULL,
    reply   TEXT NOT NULL,
    tools   TEXT NOT NULL,      -- JSON [{name, args, ok, result}]
    stopped TEXT,               -- why a guard stopped the run, or 'error: <type>' / 'busy'
    ms      INTEGER NOT NULL,
    tokens  INTEGER,            -- the window as the server counted it after the turn (prompt + answer of the last call)
    parent  INTEGER,            -- the turn whose assistant asked for this one (delegation.py); NULL for the user's own
    said    TEXT,               -- the user's own words when `text` carries more (a document's text after the caption)
    doc     INTEGER             -- the library document the message brought (library.py), or NULL
);
CREATE INDEX IF NOT EXISTS turns_route_at ON turns(route, at);
CREATE TABLE IF NOT EXISTS digests (   -- one summary per ISO week and route, made when its turns age out
    week    TEXT NOT NULL,      -- '2026-W31'
    route   TEXT NOT NULL,
    text    TEXT NOT NULL,
    n       INTEGER NOT NULL,   -- turns it covers
    made_at TEXT NOT NULL,
    PRIMARY KEY (week, route)
);
CREATE TABLE IF NOT EXISTS marks (name TEXT PRIMARY KEY, value INTEGER NOT NULL);   -- where a job stopped reading
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    text, reply, content='turns', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, text, reply) VALUES (new.id, new.text, new.reply);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, text, reply) VALUES ('delete', old.id, old.text, old.reply);
END;
CREATE TABLE IF NOT EXISTS turn_vectors (   -- one per turn and embedding model (library.Embedder), unit float32
    turn  INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    vec   BLOB NOT NULL
);
CREATE TRIGGER IF NOT EXISTS turns_vd AFTER DELETE ON turns BEGIN
    DELETE FROM turn_vectors WHERE turn = old.id;
END;
"""
EMBED_CHARS = 1200  # of a turn's text and reply, each, in its vector
CANDIDATES = 30  # per list, before fusion
# A turn the user saw: not an assistant's answer to another (parent), not a run that passed the message on.
SEEN = f"t.parent IS NULL AND COALESCE(t.stopped, '') NOT LIKE '{HANDED_OFF}%' AND t.reply != ''"  # `turns t`
WEB_CHAT = 0  # the web UI's chat id (web/app.py); Telegram's are positive


@dataclass(frozen=True)
class Turn:
    at: str
    route: str
    text: str
    reply: str
    chat_id: int | None = None
    said: str | None = None
    doc: int | None = None
    id: int | None = None

    @property
    def channel(self) -> str:
        return "" if self.chat_id is None else "web UI" if self.chat_id == WEB_CHAT else "Telegram"

    def line(self, chars: int = 200, labels: dict[str, str] | None = None) -> str:
        """'[2026-10-31 23:49, Telegram, chat] user: … | assistant: …'."""
        where = ", ".join(filter(None, (self.at[:16].replace("T", " "), self.channel,
                                        (labels or {}).get(self.route, self.route))))
        return f"[{where}] user: {cut(self.said or self.text, chars)} | assistant: {cut(self.reply, chars)}"


def cut(s: str, chars: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= chars else s[:chars - 1] + "…"


class TurnLog:
    def __init__(self, path: Path, clock: Callable[[], datetime]):
        self.clock = clock
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)
        columns = {r[1] for r in self.con.execute("PRAGMA table_info(turns)")}
        if "tokens" not in columns:  # stores from before v3
            self.con.execute("ALTER TABLE turns ADD COLUMN tokens INTEGER")
        if "parent" not in columns:  # stores from before delegation (2026-10-31)
            self.con.execute("ALTER TABLE turns ADD COLUMN parent INTEGER")
        if "said" not in columns:  # stores from before the chat thread (2026-11-01)
            self.con.execute("ALTER TABLE turns ADD COLUMN said TEXT")
            self.con.execute("ALTER TABLE turns ADD COLUMN doc INTEGER")
        self.embedder = None  # library.Embedder, set by Core: search by meaning too

    @db.locked
    def add(self, chat_id: int, route: str, text: str, reply: str, *, p: float | None = None,
            tools: Iterable[dict] = (), stopped: str | None = None, ms: int = 0, tokens: int | None = None,
            at: datetime | None = None, parent: int | None = None, said: str | None = None,
            doc: int | None = None) -> int | None:
        """Record one turn; its id. Never raises: a failing log must not break the reply (None then)."""
        try:
            calls = [{**c, "result": str(c.get("result", ""))[:RESULT_CHARS]} for c in tools]
            cur = self.con.execute(
                "INSERT INTO turns (at, chat_id, route, p, text, reply, tools, stopped, ms, tokens, parent, said, doc)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((at or self.clock()).isoformat(timespec="minutes"), chat_id, route, p, text, reply,
                 json.dumps(calls, ensure_ascii=False, default=str), stopped, ms, tokens, parent,
                 said if said is not None and said != text else None, doc))
            self.con.commit()
            return cur.lastrowid
        except Exception:
            log.exception("turn log: write failed (route=%s)", route)
            return None

    def search(self, routes: Iterable[str], query: str, limit: int = 5) -> list[Turn]:
        """These routes' turns the user saw, best first: by words and, with the embedding server, by meaning, fused
        by rank (library.py's recipe). Then the weekly digests of turns that are gone, by words."""
        from franziska.library import RRF_K, WORDS_WEIGHT

        routes = list(routes)
        if not routes or not query.strip():
            return []
        words, meaning = self._words(routes, query), self._meaning(routes, query)
        fused: dict[int, float] = {}
        for weight, ranked in ((WORDS_WEIGHT if meaning else 1.0, words), (1.0, meaning)):
            for rank, turn in enumerate(ranked):
                fused[turn] = fused.get(turn, 0.0) + weight / (RRF_K + rank + 1)
        found = self._turns(sorted(fused, key=fused.get, reverse=True)[:limit])
        if len(found) < limit:
            found += self._digests(routes, query, limit - len(found))
        return found

    @db.locked
    def size(self, routes: Iterable[str]) -> tuple[int, str | None]:
        """How many turns of these routes the user saw, and the date of the oldest: what a search looked through."""
        routes = list(routes)
        if not routes:
            return 0, None
        n, first = self.con.execute(f"SELECT count(*), min(at) FROM turns t WHERE {SEEN} AND route IN ({_marks(routes)})",
                                    routes).fetchone()
        return n, first

    @db.locked
    def _words(self, routes: list[str], query: str) -> list[int]:
        match = fts.match(query)
        if not match:
            return []
        return [r[0] for r in self.con.execute(
            "SELECT t.id FROM turns_fts f JOIN turns t ON t.id = f.rowid"
            f" WHERE turns_fts MATCH ? AND {SEEN} AND t.route IN ({_marks(routes)})"
            " ORDER BY bm25(turns_fts, 2.0, 1.0) LIMIT ?", (match, *routes, CANDIDATES))]

    def _meaning(self, routes: list[str], query: str) -> list[int]:
        if not self.embedder or (model := self.embedder.model()) is None:
            return []
        with self.lock:
            rows = self.con.execute(
                f"SELECT v.turn, v.vec FROM turn_vectors v JOIN turns t ON t.id = v.turn WHERE v.model = ? AND {SEEN}"
                f" AND t.route IN ({_marks(routes)})", (model, *routes)).fetchall()
        if not rows:
            return []
        q = self.embedder.query(query)
        if q is None:
            return []
        import numpy as np

        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        scores = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows]) @ q
        top = np.argsort(-scores)[:CANDIDATES]
        return [int(ids[i]) for i in top]

    @db.locked
    def _turns(self, ids: list[int]) -> list[Turn]:
        if not ids:
            return []
        rows = {r[0]: r for r in self.con.execute(
            f"SELECT id, at, route, text, reply, chat_id, said, doc FROM turns WHERE id IN ({_marks(ids)})", ids)}
        return [Turn(at, route, text, reply, chat_id, said, doc, i)
                for i, at, route, text, reply, chat_id, said, doc in (rows[i] for i in ids if i in rows)]

    @db.locked
    def _digests(self, routes: list[str], query: str, limit: int) -> list[Turn]:
        words = fts.words(query)
        if not words:
            return []
        clause = " OR ".join("text LIKE ?" for _ in words)
        return [Turn(week, route, "(week summary)", text) for week, route, text in self.con.execute(
            f"SELECT week, route, text FROM digests WHERE route IN ({_marks(routes)}) AND ({clause})"
            " ORDER BY week DESC LIMIT ?", (*routes, *(f"%{w}%" for w in words), limit))]

    @db.locked
    def thread(self, chat_id: int, limit: int, since: datetime) -> list[tuple[Turn, tuple[str, ...]]]:
        """One chat's latest turns the user saw since `since` and its last clear, oldest first, each with the routes
        whose data it holds: its own, or for the planner's turn the routes of its steps."""
        rows = self.con.execute(
            f"SELECT id, at, route, text, reply, chat_id, said, doc FROM turns t WHERE chat_id = ? AND {SEEN} AND at >= ?"
            " AND id > COALESCE((SELECT value FROM marks WHERE name = ?), 0) ORDER BY id DESC LIMIT ?",
            (chat_id, since.isoformat(timespec="minutes"), f"cleared:{chat_id}", limit)).fetchall()
        out = []
        for i, at, route, text, reply, chat, said, doc in reversed(rows):
            steps = tuple(r for (r,) in self.con.execute("SELECT DISTINCT route FROM turns WHERE parent = ?", (i,)))
            out.append((Turn(at, route, text, reply, chat, said, doc, i), steps if route == "plan" else (route,)))
        return out

    def fill(self, batch: int = 16, max_s: float = 30.0, quiet: Callable[[], bool] = lambda: True) -> int:
        """Embed the turns the user saw that have no vector from the server's current model, newest first, while
        `quiet()`; returns how many (the bot's library job, after the library's own passages)."""
        if not self.embedder:
            return 0
        done, started = 0, time.monotonic()
        while time.monotonic() - started < max_s and quiet():
            model = self.embedder.model()
            if not model:
                break
            with self.lock:
                rows = self.con.execute(
                    f"SELECT t.id, t.at, t.route, COALESCE(t.said, t.text), t.reply FROM turns t LEFT JOIN turn_vectors v"
                    f" ON v.turn = t.id AND v.model = ? WHERE v.turn IS NULL AND {SEEN} ORDER BY t.id DESC LIMIT ?",
                    (model, batch)).fetchall()
            if not rows:
                break
            got = self.embedder.embed([self.embedder.document(
                f"conversation, {at[:10]}", f"user: {text[:EMBED_CHARS]}\nassistant: {reply[:EMBED_CHARS]}")
                for _, at, _, text, reply in rows])
            if got is None:
                break
            with self.lock:
                self.con.executemany("INSERT OR REPLACE INTO turn_vectors (turn, model, vec) VALUES (?, ?, ?)",
                                     [(r[0], model, v.tobytes()) for r, v in zip(rows, got)])
                self.con.commit()
            done += len(rows)
        if done:
            log.info("turns: %d embedded in %.1f s", done, time.monotonic() - started)
        return done

    @db.locked
    def recent(self, chat_id: int, limit: int = 30) -> list[Turn]:
        """One chat's latest turns since its last clear, oldest first: the web UI's conversation after a reload.
        Without what the user didn't see: another assistant's answer to an ask, a run that handed the message on."""
        rows = self.con.execute(
            "SELECT at, route, text, reply FROM turns WHERE chat_id = ? AND parent IS NULL"
            f" AND COALESCE(stopped, '') NOT LIKE '{HANDED_OFF}%'"
            " AND id > COALESCE((SELECT value FROM marks WHERE name = ?), 0) ORDER BY id DESC LIMIT ?",
            (chat_id, f"cleared:{chat_id}", limit)).fetchall()
        return [Turn(*r) for r in reversed(rows)]

    @db.locked
    def clear(self, chat_id: int) -> None:
        """Hide this chat's turns so far from `recent` (the web chat's /clear); the log itself keeps them."""
        last = self.con.execute("SELECT COALESCE(MAX(id), 0) FROM turns").fetchone()[0]
        self.con.execute("INSERT INTO marks VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                         (f"cleared:{chat_id}", last))
        self.con.commit()

    @db.locked
    def runs(self, route: str, limit: int = 5) -> list[dict]:
        """One route's latest turns, all chats, newest first, with how long they took and why they stopped: a
        workflow's recent runs in the web UI."""
        rows = self.con.execute("SELECT at, text, reply, stopped, ms FROM turns WHERE route = ? ORDER BY id DESC LIMIT ?",
                                (route, limit)).fetchall()
        return [{"at": at, "text": text, "reply": reply, "stopped": stopped, "ms": ms}
                for at, text, reply, stopped, ms in rows]

    @db.locked
    def counts(self, days: int) -> dict[str, int]:
        """Turns per route over the last `days`, all chats: how busy each assistant has been (the web's world)."""
        since = (self.clock() - timedelta(days=days)).isoformat(timespec="minutes")
        return dict(self.con.execute("SELECT route, COUNT(*) FROM turns WHERE at >= ? GROUP BY route",
                                     (since,)).fetchall())

    @db.locked
    def expire(self) -> int:
        """Delete turns older than RAW_DAYS whose week is digested, and any older than KEEP_DAYS; returns how many."""
        raw = (self.clock() - timedelta(days=RAW_DAYS)).isoformat(timespec="minutes")
        hard = (self.clock() - timedelta(days=KEEP_DAYS)).isoformat(timespec="minutes")
        gone = 0
        for turn_id, at, route in self.con.execute("SELECT id, at, route FROM turns WHERE at < ?", (raw,)).fetchall():
            if at < hard or self.con.execute("SELECT 1 FROM digests WHERE week = ? AND route = ?",
                                             (week_of(at), route)).fetchone():
                gone += self.con.execute("DELETE FROM turns WHERE id = ?", (turn_id,)).rowcount
        self.con.commit()
        return gone

    # ---- for consolidate.py -------------------------------------------------------------------------
    @db.locked
    def mark(self, name: str) -> int:
        row = self.con.execute("SELECT value FROM marks WHERE name = ?", (name,)).fetchone()
        return row[0] if row else 0

    @db.locked
    def set_mark(self, name: str, value: int) -> None:
        self.con.execute("INSERT INTO marks VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                         (name, value))
        self.con.commit()

    @db.locked
    def after(self, turn_id: int, routes: Iterable[str]) -> list[tuple[int, str, str, str]]:
        """(id, route, text, reply) of turns newer than `turn_id` on these routes, oldest first."""
        routes = list(routes)
        return self.con.execute(
            f"SELECT id, route, text, reply FROM turns WHERE id > ? AND stopped IS NULL AND route IN"
            f" ({','.join('?' * len(routes))}) ORDER BY id", (turn_id, *routes)).fetchall()

    @db.locked
    def full_after(self, turn_id: int, route: str) -> list[tuple[int, str, str, list[dict]]]:
        """(id, text, reply, tool calls) of this route's finished turns newer than `turn_id`, oldest first."""
        return [(i, t, r, json.loads(tools or "[]")) for i, t, r, tools in self.con.execute(
            "SELECT id, text, reply, tools FROM turns WHERE id > ? AND route = ? AND stopped IS NULL ORDER BY id",
            (turn_id, route))]

    @db.locked
    def undigested(self) -> dict[tuple[str, str], list[Turn]]:
        """The turns older than RAW_DAYS whose (week, route) has no digest yet, by (week, route)."""
        raw = (self.clock() - timedelta(days=RAW_DAYS)).isoformat(timespec="minutes")
        out: dict[tuple[str, str], list[Turn]] = {}
        for at, route, text, reply in self.con.execute(
                "SELECT at, route, text, reply FROM turns WHERE at < ? AND stopped IS NULL ORDER BY id", (raw,)):
            key = (week_of(at), route)
            if not self.con.execute("SELECT 1 FROM digests WHERE week = ? AND route = ?", key).fetchone():
                out.setdefault(key, []).append(Turn(at, route, text, reply))
        return out

    @db.locked
    def add_digest(self, week: str, route: str, text: str, n: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO digests VALUES (?, ?, ?, ?, ?)",
                         (week, route, text, n, self.clock().isoformat(timespec="minutes")))
        self.con.commit()


def _marks(values) -> str:
    return ", ".join("?" * len(values))


def week_of(at: str) -> str:
    """'2026-10-30T14:05' → '2026-W44'."""
    year, week, _ = datetime.fromisoformat(at).isocalendar()
    return f"{year}-W{week:02d}"
