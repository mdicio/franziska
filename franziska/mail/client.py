"""Email triage: which new messages need the user, decided on the local model only.

Pipeline (each step a job on the box; triage waits for the desktop, it never goes to the cloud):
1. fetch: new Gmail headers (sender, subject, Google's ~200-char snippet) into `mail`. No bodies are stored.
2. triage: AnyJev decisions per message on the local model: what kind of mail it is, and whether it clearly needs
   the user (`urgent_p`, three narrow questions, below). Read as probabilities, like the router; nothing is generated.
3. push: only the clearly urgent ones, one message each with ✅ / 🔇 buttons (🔇 mutes the sender's pushes). The
   rest is browsable with /mail; nothing else is sent. The user's own mail is never pushed.

Next step (not built): for `bill` and `appointment` mail, extract date/amount with the local agent and propose a task
or calendar event with Add/Ignore buttons. Never added silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from franziska import db
from franziska.google.gmail import DEFAULT_QUERY, SKIP, Gmail, MailHeader

log = logging.getLogger(__name__)

KINDS = {
    "action": "action: someone expects a reply, a decision or something done",
    "bill": "bill: a payment request, invoice, or due date for money",
    "appointment": "appointment: a booking, meeting, visit or event with a date",
    "receipt": "receipt: a confirmation, order, shipping or account notice, nothing to do",
    "newsletter": "newsletter: news, marketing or automated updates",
    "personal": "personal: a personal message to read, no action needed",
}
NEEDS_USER = ("action", "bill", "appointment")
FETCH_LIMIT = 500  # new messages per fetch; paged, so a day or two of the box being off is caught up in one run
OVERLAP = timedelta(days=1)  # fetch from a day before the newest stored message: late-indexed mail is not missed

SCHEMA = """
CREATE TABLE IF NOT EXISTS mail (
    id          TEXT PRIMARY KEY,           -- Gmail message id
    thread_id   TEXT,
    sender      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    received_at TEXT NOT NULL,
    snippet     TEXT NOT NULL,
    kind        TEXT,                       -- a KINDS key, NULL until triaged
    kind_p      REAL,
    urgent_p    REAL,
    triaged_at  TEXT,
    reported    INTEGER NOT NULL DEFAULT 0, -- pushed (or shown in a digest) already
    address     TEXT NOT NULL DEFAULT ''    -- the sender's address, lowercase ('' for mail stored before it was kept)
);
CREATE TABLE IF NOT EXISTS mail_muted (sender TEXT PRIMARY KEY, at TEXT NOT NULL);  -- 🔇: never pushed again
"""
PUSH_P = 0.5  # urgent_p from which a message is pushed


@dataclass(frozen=True)
class Mail:
    id: str
    sender: str
    subject: str
    received_at: str
    snippet: str
    kind: str | None
    kind_p: float | None
    urgent_p: float | None
    address: str = ""

    @property
    def who(self) -> str:
        """What 🔇 mutes: the address, or the display name for mail stored before addresses were kept."""
        return self.address or self.sender

    def line(self) -> str:
        flag = "❗" if (self.urgent_p or 0) >= PUSH_P else "•"
        return f"{flag} {self.sender}: {self.subject} ({self.kind}, {self.received_at[5:10].replace('-', '/')})"


class MailStore:
    def __init__(self, path: Path):
        self.con, self.lock = db.connect(path), db.lock()
        if (self.con.execute("SELECT name FROM sqlite_master WHERE name = 'mail'").fetchone()
                and "address" not in {r[1] for r in self.con.execute("PRAGMA table_info(mail)")}):
            self.con.execute("ALTER TABLE mail ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        self.con.executescript(SCHEMA)

    _COLS = "id, sender, subject, received_at, snippet, kind, kind_p, urgent_p, address"

    @db.locked
    def add(self, h: MailHeader) -> None:
        self.con.execute("INSERT OR IGNORE INTO mail (id, thread_id, sender, subject, received_at, snippet, address)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (h.id, h.thread_id, h.sender, h.subject, h.received_at, h.snippet, h.address))
        self.con.commit()

    @db.locked
    def known(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        return {r[0] for r in self.con.execute(f"SELECT id FROM mail WHERE id IN ({marks})", ids)}

    def untriaged(self, limit: int = 50) -> list[Mail]:
        return self._rows("kind IS NULL ORDER BY received_at LIMIT ?", (limit,))

    @db.locked
    def set_triage(self, mail_id: str, kind: str, kind_p: float, urgent_p: float, now: str) -> None:
        self.con.execute("UPDATE mail SET kind = ?, kind_p = ?, urgent_p = ?, triaged_at = ? WHERE id = ?",
                         (kind, kind_p, urgent_p, now, mail_id))
        self.con.commit()

    def needing_user(self, unreported_only: bool = False, since: str | None = None) -> list[Mail]:
        marks = ",".join("?" * len(NEEDS_USER))
        where = f"kind IN ({marks})" + (" AND reported = 0" if unreported_only else "")
        args: tuple = NEEDS_USER
        if since:
            where, args = where + " AND received_at >= ?", (*args, since)
        return self._rows(where + " ORDER BY urgent_p DESC, received_at DESC", args)

    def urgent(self, unreported_only: bool = False, since: str | None = None) -> list[Mail]:
        """Mail that clearly needs the user (urgent_p ≥ PUSH_P) from a sender not muted, oldest first."""
        where = ("urgent_p >= ? AND (CASE address WHEN '' THEN sender ELSE address END)"
                 " NOT IN (SELECT sender FROM mail_muted)") + (" AND reported = 0" if unreported_only else "")
        args: tuple = (PUSH_P,)
        if since:
            where, args = where + " AND received_at >= ?", (*args, since)
        return self._rows(where + " ORDER BY received_at", args)

    def get(self, mail_id: str) -> Mail | None:
        rows = self._rows("id = ?", (mail_id,))
        return rows[0] if rows else None

    @db.locked
    def mute(self, who: str, now: str) -> None:
        self.con.execute("INSERT OR REPLACE INTO mail_muted (sender, at) VALUES (?, ?)", (who, now))
        self.con.commit()

    @db.locked
    def threads(self, kinds: tuple[str, ...], since: str) -> list[str]:
        """Conversations with a message triaged as one of `kinds` received since `since` (local ISO), newest first."""
        marks = ",".join("?" * len(kinds))
        return [r[0] for r in self.con.execute(
            f"SELECT thread_id FROM mail WHERE kind IN ({marks}) AND received_at >= ? AND thread_id IS NOT NULL"
            " GROUP BY thread_id ORDER BY MAX(received_at) DESC", (*kinds, since))]

    @db.locked
    def newest(self) -> str | None:
        """received_at of the newest stored message (naive local ISO), or None when the store is empty."""
        return self.con.execute("SELECT MAX(received_at) FROM mail").fetchone()[0]

    @db.locked
    def expire(self, keep_days: int, now: datetime) -> int:
        """Triaged mail older than `keep_days` goes; mail still waiting for the model stays."""
        cutoff = (now - timedelta(days=keep_days)).isoformat(timespec="minutes")
        n = self.con.execute("DELETE FROM mail WHERE kind IS NOT NULL AND received_at < ?", (cutoff,)).rowcount
        self.con.commit()
        return n

    @db.locked
    def pending_count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM mail WHERE kind IS NULL").fetchone()[0]

    @db.locked
    def mark_reported(self, ids: list[str]) -> None:
        self.con.executemany("UPDATE mail SET reported = 1 WHERE id = ?", [(i,) for i in ids])
        self.con.commit()

    @db.locked
    def _rows(self, where: str, args: tuple) -> list[Mail]:
        return [Mail(*r) for r in self.con.execute(f"SELECT {self._COLS} FROM mail WHERE {where}", args)]


def fetch_query(store: MailStore, gmail: Gmail) -> str:
    """Everything since the newest stored message (less OVERLAP), not a fixed window: after the box was off for days,
    the gap is fetched too. An empty store starts with the last two days."""
    newest = store.newest()
    if newest is None:
        return DEFAULT_QUERY
    since = datetime.fromisoformat(newest).replace(tzinfo=gmail.tz) - OVERLAP
    return f"after:{int(since.timestamp())} {SKIP}"


def fetch_new(gmail: Gmail, store: MailStore, limit: int = FETCH_LIMIT) -> int:
    """Store headers of messages not seen before; returns how many were new."""
    ids = gmail.recent_ids(fetch_query(store, gmail), limit=limit)
    if len(ids) >= limit:
        log.warning("mail: %d+ messages since the last fetch; only the newest %d are fetched", limit, limit)
    known = store.known(ids)
    new = [i for i in ids if i not in known]
    for message_id in new:
        store.add(gmail.header(message_id))
    return len(new)


PUSH_QUESTIONS = {
    "sender": "Is this email written by a person or an organisation the recipient actually deals with (their bank, "
              "building administrator, doctor, employer, landlord, a public office, a friend), rather than marketing, "
              "a newsletter, a promotion, a social-network or platform notification, or an automated digest?",
    "act": "Does this email require the recipient to do or acknowledge something themselves: pay, reply, confirm, be "
           "present, take note of a changed date, or check a security alert?",
    "cost": "Would ignoring this email for a week cost the recipient something real: money, a missed date or "
            "appointment, or access to their account?",
}


class Triage:
    """Two typed decisions per message on the local model (same AnyJev backend as the router)."""

    def __init__(self, backend):
        from anyjev import Decider, Question

        self.decider = Decider(backend, level="L0", prior="content_free", canonical_order=True)
        self.kind_q = Question.choice("What kind of email is this, for its recipient?", list(KINDS.values()),
                                      name="kind")
        # Whether to push: all three must hold (urgent_p is the lowest). One broad 'does it ask you to act soon?'
        # question scored marketing countdowns and LinkedIn messages 1.0 and a condo email moving a date 0.0; the
        # sender question carries the weight (every marketing mail says 'act now'). 9/10 on hand-picked mail,
        # 2026-11-10 (gemma4-26b); the miss was a heating notice with nothing to do.
        self.push_qs = [Question.noul(q, name=n) for n, q in PUSH_QUESTIONS.items()]

    def __call__(self, m: Mail, me: str = "") -> tuple[str, float, float]:
        state = f"From: {m.sender}\nSubject: {m.subject}\n\n{m.snippet}"
        own = bool(me) and m.address == me  # the user's own mail (sent to self, forwards): never pushed
        d = self.decider.decide(state, [self.kind_q] + ([] if own else self.push_qs))
        keys = {v: k for k, v in KINDS.items()}
        kind = keys[d["kind"].argmax]
        return kind, d["kind"].confidence, 0.0 if own else min(d[n].p_true for n in PUSH_QUESTIONS)


def triage_pending(store: MailStore, triage: Triage, now: str, limit: int = 50, me: str = "") -> int:
    done = 0
    for m in store.untriaged(limit):
        kind, kind_p, urgent_p = triage(m, me)
        store.set_triage(m.id, kind, kind_p, urgent_p, now)
        done += 1
    return done


def digest(store: MailStore, unreported_only: bool = True, mark: bool = True, since: str | None = None) -> str | None:
    """'📬 N emails need you' with one line each, or None when there are none."""
    mails = store.needing_user(unreported_only, since)
    if not mails:
        return None
    if mark:
        store.mark_reported([m.id for m in mails])
    return "\n".join([f"📬 {len(mails)} email{'s' if len(mails) > 1 else ''} need you:"] + [m.line() for m in mails])
