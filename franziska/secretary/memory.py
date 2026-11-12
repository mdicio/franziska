"""Notes the user wants kept: facts about their life ("gate code 4471", "Luca wears 43", "passport expires 2031-03").

SQLite FTS5 over subject + body, prefix-matched, so recall needs no embedding model. Accents are folded
('città' finds 'citta'). Lives in the same file as the tasks.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from franziska import db, fts

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    subject, body, content='notes', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, subject, body) VALUES (new.id, new.subject, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, subject, body) VALUES ('delete', old.id, old.subject, old.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, subject, body) VALUES ('delete', old.id, old.subject, old.body);
    INSERT INTO notes_fts(rowid, subject, body) VALUES (new.id, new.subject, new.body);
END;
"""


@dataclass(frozen=True)
class Note:
    id: int
    subject: str
    body: str
    updated_at: str

    def line(self) -> str:
        return f"[{self.id}] {self.subject}: {self.body} (saved {self.updated_at[:10]})"


class NoteStore:
    def __init__(self, path: Path):
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)

    _COLS = "id, subject, body, updated_at"

    @db.locked
    def add(self, subject: str, body: str, now: datetime) -> Note:
        ts = now.isoformat(timespec="minutes")
        cur = self.con.execute("INSERT INTO notes (subject, body, created_at, updated_at) VALUES (?, ?, ?, ?)",
                               (subject.strip(), body.strip(), ts, ts))
        self.con.commit()
        return self.get(cur.lastrowid)

    @db.locked
    def get(self, note_id: int) -> Note | None:
        row = self.con.execute(f"SELECT {self._COLS} FROM notes WHERE id = ?", (note_id,)).fetchone()
        return Note(*row) if row else None

    @db.locked
    def update(self, note_id: int, body: str, now: datetime) -> Note | None:
        self.con.execute("UPDATE notes SET body = ?, updated_at = ? WHERE id = ?",
                         (body.strip(), now.isoformat(timespec="minutes"), note_id))
        self.con.commit()
        return self.get(note_id)

    @db.locked
    def delete(self, note_id: int) -> Note | None:
        note = self.get(note_id)
        if note:
            self.con.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            self.con.commit()
        return note

    @db.locked
    def same_subject(self, subject: str, body: str) -> tuple[Note | None, list[Note]]:
        """(the note with this subject and body, if one exists; the other notes on this subject). Compared with
        accents, case and spacing folded. A note is on this subject too when every word of its subject (two or more)
        is in the new subject or body: 'codice portone' for 'gate code: Il codice del portone è 2207C' (realmonth 2026-10-30)."""
        key = " ".join(fts.fold(subject).split())
        said = set(re.findall(r"\w+", fts.fold(f"{subject} {body}")))
        rows = [Note(*r) for r in self.con.execute(f"SELECT {self._COLS} FROM notes ORDER BY id")]
        hits = [n for n in rows if " ".join(fts.fold(n.subject).split()) == key
                or len(words := {w for w in re.findall(r"\w+", fts.fold(n.subject)) if len(w) > 2}) > 1
                and words <= said]  # one word ('Luca') would name every note about him
        text = " ".join(fts.fold(body).split())
        same = next((n for n in hits if " ".join(fts.fold(n.body).split()) == text), None)
        return same, [n for n in hits if n is not same]

    @db.locked
    def search(self, query: str, limit: int = 8) -> list[Note]:
        """Notes matching any word of `query` (prefix match, fts.words: 'scarpa' finds 'scarpe'), best first."""
        match = fts.match(query)
        if not match:
            return self.recent(limit)
        rows = self.con.execute(
            f"SELECT {', '.join('n.' + c.strip() for c in self._COLS.split(','))} FROM notes_fts f"
            " JOIN notes n ON n.id = f.rowid WHERE notes_fts MATCH ? ORDER BY bm25(notes_fts, 2.0, 1.0) LIMIT ?",
            (match, limit),
        ).fetchall()
        return [Note(*r) for r in rows]

    @db.locked
    def recent(self, limit: int = 8) -> list[Note]:
        rows = self.con.execute(f"SELECT {self._COLS} FROM notes ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        return [Note(*r) for r in rows]
