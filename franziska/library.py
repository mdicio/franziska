"""The document library (DESIGN decision 39, PROPOSALS Part 2): every document the user sends or an agent reads, kept
on this machine and searchable across documents, by words and by meaning.

**Store**, `library.sqlite`:
- `documents`: one row per distinct text (sha256 of it, so a bill read from mail and sent again on Telegram is one
  document), its source (`telegram`, `web`, `mail:<message id>`), a **privacy** class and whether the user kept it.
- `pages`: the text by page. A PDF's pages are its own (pdftotext and OCR end each one with a form feed); a file
  without pages (.docx, .xlsx, .txt) is cut into parts of PART_CHARS, so `read_document` always answers in bounded
  pieces and a citation 'p. N' points at something the agent read.
- `chunks`: passages of ~400 tokens within a page, cut at paragraphs, overlapping by ~15%, with the section heading
  above them carried along. A chunk never crosses a page, so a hit's page is exact.
- `chunks_fts` (FTS5 over title, heading, text) and `vectors` (one float32 vector per chunk, normalised, tagged with
  the embedding model that made it).
The original file is kept under `data_dir/library/files/<sha256>` (up to MAX_FILE), so a better parser can read it
again later. The nightly backup copies library.sqlite, which holds every text; the originals are not backed up.

**Hybrid search** (`search`), the usual recipe for a corpus this size:
1. Words: FTS5 BM25 over the chunks (fts.match folds accents and Italian plurals) catches codes, names, amounts and
   exact terms, which embeddings blur.
2. Meaning: the query's embedding against every chunk's, brute force in numpy. At personal scale (thousands to tens
   of thousands of chunks) that is milliseconds, and the vectors live in a plain BLOB column: this Python's sqlite3
   cannot load extensions, so sqlite-vec is out, and nothing else here needs a vector index.
   A chunk only meaning found, below the model's floor (FLOORS), is dropped: the nearest passages are not always near.
3. Reciprocal Rank Fusion (k=60) of the two top-30 lists: ranks only, so the two scores never need calibrating.
   The words list counts half (WORDS_WEIGHT) and its query drops Italian and English function words (QUERY_STOP):
   'posso tenere un gatto?' matched every chunk with 'posso' in it, and with RRF a chunk on both lists, however
   low, outranks the best chunk by meaning (evals/library.py, 2026-10-31: r@5 0.89 → 0.97 on the synthetic set).
4. A cross-encoder reranker over the fused top 20, when FRANZISKA_RERANK_URL is set (evals/library.py decides if it
   pays for its CPU time).
5. At most MAX_PER_DOC passages per document, so a question across documents sees more than one; adjacent hits of
   one page are merged into one passage.
Each chunk is embedded with its document's title and heading in front (a contextual chunk header: 'Art. 7' alone
says nothing, 'Polizza Generali › Art. 7 Recesso' does), with the prompt format the embedding model expects.

**Privacy is a filter in the SQL**, by route (privacy.SEES): web sees `public` only; money its own documents, the
personal ones and the mail ones; life and mail the personal and mail ones. Chat has no tools. A document gets its class when it comes
in (privacy.HOLDS: the route that handled it) and keeps it.

**Vectors are filled in the background** (`fill`, the bot's library job): a document is searchable by words the
moment it is added, and by meaning once the CPU embedding server (deploy/franziska-embed.service) has caught up,
which it does while the chat is quiet so it doesn't slow a turn. Without FRANZISKA_EMBED_URL the library is words
only; with the server down, a search falls back to words and says nothing.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
from pydantic_ai import FunctionToolset, ModelRetry

from franziska import db, fts, modules
from franziska.privacy import CLASSES, sees

log = logging.getLogger(__name__)

# The routes with document tools. What each may search and read, and the class a document gets from the route it came
# in on, is privacy.py's one table (SEES, HOLDS).
ROUTES = modules.names("documents")  # modules.py: Module.documents

CHUNK_CHARS = 1300  # ~400 tokens at budget.CHARS_PER_TOKEN
OVERLAP = 200  # ~15% of a chunk, repeated at the start of the next
PART_CHARS = 4000  # a 'page' of a file that has none
MAX_FILE = 25 * 1024 * 1024  # bigger originals are not kept (their text is)
KEEP_DAYS = 180  # documents nobody used for this long go, unless the user kept them
CANDIDATES = 30  # per list, before fusion
RRF_K = 60
WORDS_WEIGHT = 0.5  # of the words list in the fusion; meaning counts 1
# Words a question is made of rather than about: left out of the words query only (notes keep fts.STOP).
QUERY_STOP = frozenset("""
al allo ai agli alla alle col coi dal dallo dai dagli dalla dalle dello nello nei negli nelle sul sullo sui sugli
sulla sulle tra fra non come dove quando quanto quanta quanti quante quale quali cosa chi cui mio mia miei mie tuo
tua suo sua nostro vostro loro questo questa questi queste quello quella quelli quelle sono sei siamo siete hai
abbiamo avete hanno posso puoi puo possiamo devo deve devono dobbiamo vengono viene essere avere fare faccio fanno
piu anche gia ancora sempre solo molto poco tutto tutti tutta tutte ogni what when where which who how much many
does can could should would have has had are was were this that these those there about with from into
""".split())
RERANK = 20  # fused candidates the reranker sees
PASSAGES = 5
MAX_PER_DOC = 3
LISTED = 15
DESCRIBED = " — "  # between a document's file name and what it is about (Library.describe)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT NOT NULL,
    source    TEXT NOT NULL,     -- telegram | web | web:<url> | mail:<message id>
    sha256    TEXT NOT NULL UNIQUE,
    mime      TEXT NOT NULL,
    pages     INTEGER NOT NULL,
    chars     INTEGER NOT NULL,
    privacy   TEXT NOT NULL,     -- public | personal | mail | money (privacy.CLASSES)
    kept      INTEGER NOT NULL DEFAULT 0,
    file      INTEGER NOT NULL DEFAULT 0,  -- the original is under files/<sha256>
    added     TEXT NOT NULL,
    last_used TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    doc  INTEGER NOT NULL,
    n    INTEGER NOT NULL,       -- from 1
    text TEXT NOT NULL,
    PRIMARY KEY (doc, n)
);
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused: the vector cache keys on it
    doc     INTEGER NOT NULL,
    page    INTEGER NOT NULL,
    start   INTEGER NOT NULL,    -- offsets in the page's text
    end     INTEGER NOT NULL,
    title   TEXT NOT NULL,       -- the document's, repeated so FTS can weigh it
    heading TEXT NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc, page, start);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, heading, text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, heading, text) VALUES (new.id, new.title, new.heading, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, heading, text)
    VALUES ('delete', old.id, old.title, old.heading, old.text);
END;
CREATE TABLE IF NOT EXISTS vectors (
    chunk INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    vec   BLOB NOT NULL          -- float32, unit length
);
"""


# ---- documents in, as pages and chunks ------------------------------------------------------------------------
def pages_of(text: str) -> list[str]:
    """The text by page: a PDF's own pages, else parts of about PART_CHARS cut at a paragraph or line end."""
    if "\f" in text:
        return [p.strip() for p in text.split("\f") if p.strip()] or [text.strip()]
    from franziska.doccache import chunks

    return [piece for _, piece in chunks(text, PART_CHARS)] or [text.strip()]


_NUMBERED = re.compile(r"^(art(icolo)?\.?\s*\d+|sezione\s+\w+|capitolo\s+\w+|section\s+\w+|\d+(\.\d+)*\.?\s+\S)", re.I)


def heading(line: str) -> bool:
    """A line that names a section: '## Sheet' (xlsx), 'Art. 7 - Recesso', '3.2 Franchigie', or a short line in
    capitals ('CONDIZIONI GENERALI'). Layout, not meaning: it only labels chunks, never decides anything."""
    line = line.strip()
    if not 3 <= len(line) <= 90 or line.endswith((",", ";")):
        return False
    if line.startswith("#"):
        return True
    letters = [c for c in line if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return True
    return bool(_NUMBERED.match(line)) and len(line.split()) <= 12 and not line.endswith(".")


def split(page: str, carried: str = "") -> tuple[list[tuple[int, int, str]], str]:
    """A page's chunks as (start, end, heading), cut at paragraphs, each overlapping the previous by about OVERLAP;
    and the heading in force at the page's end, carried to the next page."""
    marks, current, offset = [], carried, 0  # (offset, heading in force from there)
    for line in page.splitlines(keepends=True):
        if heading(line):
            current = line.strip().lstrip("#").strip()
            marks.append((offset, current))
        offset += len(line)

    def heading_at(pos: int) -> str:
        found = carried
        for at, h in marks:
            if at > pos:
                break
            found = h
        return found

    out, start = [], 0
    while start < len(page):
        end = min(start + CHUNK_CHARS, len(page))
        if end < len(page):
            floor = start + CHUNK_CHARS // 2
            cut = max(page.rfind("\n\n", floor, end), page.rfind("\n", floor, end), page.rfind(". ", floor, end))
            end = cut + 1 if cut > 0 else end
        if page[start:end].strip():
            out.append((start, end, heading_at(start + 1)))
        if end >= len(page):
            break
        nxt = max(end - OVERLAP, start + 1)
        space = page.find(" ", nxt, end)  # begin the overlap on a word
        start = space + 1 if space != -1 else nxt
    return out, current


# ---- embeddings and reranking (llama-server on the CPU) ---------------------------------------------------------
# How each embedding family wants its inputs (model cards). Query instructions in English, as Qwen recommends.
PROMPTS: dict[str, tuple[str, str]] = {
    "qwen3-embedding": ("Instruct: Given a question, retrieve the passages of the user's documents that answer it\n"
                        "Query: {q}", "{title}\n{text}"),
    "embeddinggemma": ("task: search result | query: {q}", "title: {title} | text: {text}"),
    "e5": ("query: {q}", "passage: {title}\n{text}"),
}
PLAIN = ("{q}", "{title}\n{text}")
# The lowest cosine at which a passage found by meaning alone (no word of the query in it) is still a hit. The
# meaning list always has CANDIDATES chunks, however far off, so without it a question about something the library
# doesn't hold got the nearest unrelated passages as 'the best' (money on the motorbike insurance, which only life
# sees: a cover letter and a photo, 2026-11-01). Cosines scale differently per model, so the floor is per model, and
# a model not measured here has none. embeddinggemma, on evals/library_corpus.py with 10 off-topic questions:
# 0.30 keeps doc@5 at 1.00, r@5 0.95 → 0.92 (a weak paraphrase, 'i termosifoni restano freddi'); 0.40 costs doc@5.
# It can't be an absolute cut on every hit: right passages score from 0.19 and off-topic ones up to 0.44.
FLOORS: dict[str, float] = {"embeddinggemma": 0.30}
DOWN_S = 60.0  # after a failed request, the server is not asked again for this long


class Embedder:
    """An OpenAI-compatible /v1/embeddings server (llama-server --embedding). The model is the server's own: its file
    name picks the prompt format and tags every vector, so switching models re-embeds instead of mixing spaces."""

    def __init__(self, url: str, timeout: float = 60.0):
        self.url, self.timeout = url.removesuffix("/").removesuffix("/v1"), timeout
        self._model: str | None = None
        self._down_until = 0.0

    @property
    def up(self) -> bool:
        return time.monotonic() >= self._down_until

    def model(self) -> str | None:
        """The server's model, as a short name ('Qwen3-Embedding-0.6B-Q8_0'); None while it doesn't answer."""
        if self._model is None and self.up:
            try:
                data = httpx.get(self.url + "/v1/models", timeout=3).json()["data"]
                self._model = Path(data[0]["id"]).stem
            except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
                self._failed(e)
        return self._model

    def prompts(self) -> tuple[str, str]:
        name = (self.model() or "").lower()
        return next((p for key, p in PROMPTS.items() if key in name), PLAIN)

    def floor(self) -> float:
        name = (self.model() or "").lower()
        return next((f for key, f in FLOORS.items() if key in name), 0.0)

    def embed(self, texts: list[str], timeout: float | None = None) -> np.ndarray | None:
        """Unit vectors, one row per text; None when the server is down (it then rests for DOWN_S)."""
        if not texts or not self.up:
            return None
        try:
            r = httpx.post(self.url + "/v1/embeddings", json={"input": texts, "model": self.model() or ""},
                           timeout=timeout or self.timeout)
            r.raise_for_status()
            rows = sorted(r.json()["data"], key=lambda d: d["index"])
        except (httpx.HTTPError, ValueError, KeyError) as e:
            self._failed(e)
            return None
        m = np.asarray([d["embedding"] for d in rows], dtype=np.float32)
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)

    def query(self, q: str) -> np.ndarray | None:
        got = self.embed([self.prompts()[0].format(q=q)], timeout=5)  # in a turn: words alone beat waiting
        return None if got is None else got[0]

    def document(self, title: str, text: str) -> str:
        return self.prompts()[1].format(title=title or "none", text=text)

    def _failed(self, e: Exception) -> None:
        log.warning("embedding server %s: %s", self.url, e)
        self._down_until, self._model = time.monotonic() + DOWN_S, None


class Reranker:
    """llama-server --reranking (/v1/rerank): a cross-encoder scores each passage against the query."""

    def __init__(self, url: str):
        self.url = url.removesuffix("/").removesuffix("/v1")
        self._down_until = 0.0

    def scores(self, query: str, texts: list[str]) -> list[float] | None:
        if time.monotonic() < self._down_until:
            return None
        try:
            r = httpx.post(self.url + "/v1/rerank", json={"query": query, "documents": texts}, timeout=10)
            r.raise_for_status()
            out = [0.0] * len(texts)
            for res in r.json()["results"]:
                out[res["index"]] = res["relevance_score"]
            return out
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            log.warning("rerank server %s: %s", self.url, e)
            self._down_until = time.monotonic() + DOWN_S
            return None


# ---- the store --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Document:
    id: int
    title: str
    source: str
    privacy: str
    pages: int
    kept: bool
    added: str
    last_used: str

    def line(self) -> str:
        where = self.source.split(":", 1)[0]
        return (f"#{self.id} {self.title} ({self.pages} page{'s' if self.pages != 1 else ''}, from {where}, "
                f"added {self.added[:10]}{', kept' if self.kept else ''})")


@dataclass(frozen=True)
class Passage:
    doc: Document
    page: int
    heading: str
    text: str
    via: str  # words | meaning | both: which search found it (evals, traces)

    def block(self) -> str:
        where = f"#{self.doc.id} {self.doc.title}, p. {self.page}" + (f" · {self.heading}" if self.heading else "")
        return f"[{where}]\n{self.text}"


class Library:
    def __init__(self, path: Path, files: Path, clock: Callable[[], datetime], embedder: Embedder | None = None,
                 reranker: Reranker | None = None):
        self.files, self.clock, self.embedder, self.reranker = files, clock, embedder, reranker
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)
        self._cache: tuple[tuple, np.ndarray, np.ndarray, np.ndarray] | None = None  # key, chunk ids, docs, matrix
        self._cache_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings, clock: Callable[[], datetime]) -> "Library":
        return cls(settings.library_db, settings.data_dir / "library" / "files", clock,
                   Embedder(settings.embed_url) if settings.embed_url else None,
                   Reranker(settings.rerank_url) if settings.rerank_url else None)

    def _now(self) -> str:
        return self.clock().isoformat(timespec="minutes")

    _COLS = "id, title, source, privacy, pages, kept, added, last_used"

    def _doc(self, row) -> Document:
        return Document(row[0], row[1], row[2], row[3], row[4], bool(row[5]), row[6], row[7])

    # ---- in ----
    def add(self, title: str, text: str, source: str, privacy: str, mime: str = "", data: bytes | None = None,
            kept: bool = False) -> Document | None:
        """Keep a document (None when it has no text). The same text again is the same document: it is touched, kept
        if asked, and keeps the privacy it came in with."""
        text = text.replace("\r\n", "\n").strip()
        if not text:
            return None
        assert privacy in CLASSES, privacy
        sha = hashlib.sha256(text.encode()).hexdigest()
        with self.lock:
            row = self.con.execute(f"SELECT {self._COLS} FROM documents WHERE sha256 = ?", (sha,)).fetchone()
            if row:
                self.con.execute("UPDATE documents SET last_used = ?, kept = max(kept, ?) WHERE id = ?",
                                 (self._now(), int(kept), row[0]))
                self.con.commit()
                return self.get(row[0])
            pages = pages_of(text)
            stored = data is not None and len(data) <= MAX_FILE
            now = self._now()
            cur = self.con.execute(
                "INSERT INTO documents (title, source, sha256, mime, pages, chars, privacy, kept, file, added,"
                " last_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title.strip() or "untitled", source, sha, mime, len(pages), len(text), privacy, int(kept),
                 int(stored), now, now))
            doc = cur.lastrowid
            carried = ""
            for n, page in enumerate(pages, 1):
                self.con.execute("INSERT INTO pages (doc, n, text) VALUES (?, ?, ?)", (doc, n, page))
                spans, carried = split(page, carried)
                self.con.executemany(
                    "INSERT INTO chunks (doc, page, start, end, title, heading, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(doc, n, s, e, title, h, page[s:e].strip()) for s, e, h in spans])
            self.con.commit()
        if stored:
            try:
                self.files.mkdir(parents=True, exist_ok=True)
                (self.files / sha).write_bytes(data)
            except OSError:
                log.exception("library: could not keep the original of %s", title)
        log.info("library: #%d %s, %d pages, %s", doc, title, len(pages), privacy)
        return self.get(doc)

    def describe(self, doc_id: int, topic: str) -> Document | None:
        """Add what the document is about to its title ('gym.pdf — Contratto abbonamento palestra FitLife: palestra,
        iscrizione, disdetta'): every chunk carries the title in FTS and in its embedding header, so a search in the
        user's words finds a text that never uses them ('palestra' → a contract that says 'FitLife, sala pesi').
        Once per document; its vectors are made again by `fill`."""
        topic = " ".join(topic.split())[:200].strip(" —")
        if not topic:
            return None
        with self.lock:
            row = self.con.execute("SELECT title FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if not row or DESCRIBED in row[0]:
                return None
            title = f"{row[0]}{DESCRIBED}{topic}"
            chunks = self.con.execute("SELECT id, title, heading, text FROM chunks WHERE doc = ?", (doc_id,)).fetchall()
            self.con.executemany("INSERT INTO chunks_fts(chunks_fts, rowid, title, heading, text) VALUES "
                                 "('delete', ?, ?, ?, ?)", chunks)
            self.con.execute("UPDATE chunks SET title = ? WHERE doc = ?", (title, doc_id))
            self.con.executemany("INSERT INTO chunks_fts(rowid, title, heading, text) VALUES (?, ?, ?, ?)",
                                 [(c[0], title, c[2], c[3]) for c in chunks])
            self.con.execute("UPDATE documents SET title = ? WHERE id = ?", (title, doc_id))
            self.con.execute(f"DELETE FROM vectors WHERE chunk IN ({_marks(chunks)})", [c[0] for c in chunks])
            self.con.commit()
        with self._cache_lock:
            self._cache = None
        log.info("library: #%d described: %s", doc_id, topic)
        return self.get(doc_id)

    @db.locked
    def undescribed(self, limit: int, chars: int, skip: list[int] = ()) -> list[tuple[Document, str]]:
        """Documents with no description in their title yet, oldest first, but not those in `skip`, each with its
        first `chars` characters (Core.describe_new)."""
        rows = self.con.execute(f"SELECT {self._COLS} FROM documents WHERE instr(title, ?) = 0"
                                f" AND id NOT IN ({_marks(skip)}) ORDER BY id LIMIT ?", (DESCRIBED, *skip, limit))
        out = []
        for row in rows.fetchall():
            text = ""
            for (page,) in self.con.execute("SELECT text FROM pages WHERE doc = ? ORDER BY n", (row[0],)):
                text += page + "\n\n"
                if len(text) >= chars:
                    break
            out.append((self._doc(row), text[:chars]))
        return out

    @db.locked
    def keep(self, doc_id: int) -> Document | None:
        self.con.execute("UPDATE documents SET kept = 1 WHERE id = ?", (doc_id,))
        self.con.commit()
        return self.get(doc_id)

    # ---- out ----
    @db.locked
    def get(self, doc_id: int, privacy: tuple[str, ...] | None = None) -> Document | None:
        row = self.con.execute(f"SELECT {self._COLS} FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc(row) if row and (privacy is None or row[3] in privacy) else None

    @db.locked
    def documents(self, privacy: tuple[str, ...], limit: int = LISTED) -> list[Document]:
        """The most recently used documents this route may see."""
        rows = self.con.execute(f"SELECT {self._COLS} FROM documents WHERE privacy IN ({_marks(privacy)})"
                                " ORDER BY last_used DESC, id DESC LIMIT ?", (*privacy, limit))
        return [self._doc(r) for r in rows]

    @db.locked
    def original(self, doc_id: int, privacy: tuple[str, ...]) -> tuple[str, bytes | None, str] | None:
        """(title, the original file or None, the text) of a document `privacy` may see: the sandbox's input
        (sandbox.run_python's `documents`)."""
        row = self.con.execute("SELECT title, sha256, file, privacy FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not row or row[3] not in privacy:
            return None
        text = "\n\n".join(r[0] for r in self.con.execute("SELECT text FROM pages WHERE doc = ? ORDER BY n", (doc_id,)))
        path = self.files / row[1]
        data = path.read_bytes() if row[2] and path.is_file() else None
        self._touch([doc_id])
        self.con.commit()
        return row[0], data, text

    @db.locked
    def page(self, doc_id: int, n: int) -> str | None:
        row = self.con.execute("SELECT text FROM pages WHERE doc = ? AND n = ?", (doc_id, n)).fetchone()
        if row:
            self._touch([doc_id])
        return row[0] if row else None

    def _touch(self, docs) -> None:
        self.con.executemany("UPDATE documents SET last_used = ? WHERE id = ?", [(self._now(), d) for d in set(docs)])
        self.con.commit()

    # ---- search ----
    def search(self, query: str, privacy: tuple[str, ...], document: int | None = None, since: str | None = None,
               limit: int = PASSAGES, mode: str = "hybrid") -> list[Passage]:
        """The best passages for `query` among the documents `privacy` allows (optionally one document, or those
        added since an ISO date). `mode`: hybrid, words or meaning (evals compare them)."""
        allowed = self._allowed(privacy, document, since)
        if not allowed:
            return []
        words = self._words(query, allowed) if mode in ("hybrid", "words") else []
        meaning = self._meaning(query, allowed, set(words)) if mode in ("hybrid", "meaning") else []
        fused: dict[int, float] = {}
        for weight, ranked in ((WORDS_WEIGHT if meaning else 1.0, words), (1.0, meaning)):
            for rank, chunk in enumerate(ranked):
                fused[chunk] = fused.get(chunk, 0.0) + weight / (RRF_K + rank + 1)
        order = sorted(fused, key=fused.get, reverse=True)
        rows = self._chunks(order[:max(RERANK, limit * MAX_PER_DOC)])
        if self.reranker and len(order) > 1:
            head = [c for c in order[:RERANK] if c in rows]
            got = self.reranker.scores(query, [f"{rows[c][4]}\n{rows[c][5]}" for c in head])
            if got:
                order = [c for _, c in sorted(zip(got, head), key=lambda t: -t[0])] + order[RERANK:]
        picked, per_doc = [], {}
        for c in order:
            if c not in rows:
                continue
            doc = rows[c][0]
            if document is None and per_doc.get(doc, 0) >= MAX_PER_DOC:
                continue
            per_doc[doc] = per_doc.get(doc, 0) + 1
            picked.append(c)
            if len(picked) >= limit:
                break
        via = {c: "both" if c in words and c in meaning else "words" if c in words else "meaning" for c in picked}
        return self._passages(picked, rows, via)

    @db.locked
    def _allowed(self, privacy, document, since) -> list[int]:
        sql, args = f"SELECT id FROM documents WHERE privacy IN ({_marks(privacy)})", list(privacy)
        if document is not None:
            sql, args = sql + " AND id = ?", args + [document]
        if since:
            sql, args = sql + " AND added >= ?", args + [since]
        return [r[0] for r in self.con.execute(sql, args)]

    @db.locked
    def _words(self, query: str, allowed: list[int]) -> list[int]:
        match = fts.match(" ".join(w for w in re.findall(r"\w+", query) if fts.fold(w) not in QUERY_STOP))
        if not match:
            return []
        rows = self.con.execute(
            "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid"
            f" WHERE chunks_fts MATCH ? AND c.doc IN ({_marks(allowed)})"
            " ORDER BY bm25(chunks_fts, 2.0, 2.0, 1.0) LIMIT ?", (match, *allowed, CANDIDATES))
        return [r[0] for r in rows]

    def _meaning(self, query: str, allowed: list[int], words: set[int] = frozenset()) -> list[int]:
        """The nearest chunks by meaning, best first; one below the model's floor only if `words` found it too."""
        if not self.embedder or (model := self.embedder.model()) is None:
            return []
        ids, docs, matrix = self._vectors(model)
        if not len(ids):
            return []
        q = self.embedder.query(query)
        if q is None:
            return []
        mask = np.isin(docs, allowed)
        ids, matrix = ids[mask], matrix[mask]
        if not len(ids):
            return []
        scores, floor = matrix @ q, self.embedder.floor()
        top = np.argpartition(-scores, min(CANDIDATES, len(scores) - 1))[:CANDIDATES]
        return [int(ids[i]) for i in top[np.argsort(-scores[top])] if scores[i] >= floor or int(ids[i]) in words]

    def _vectors(self, model: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Every vector of `model` in memory, with its chunk and document: read again only when the table changed
        (chunk ids are never reused, so count and max id tell; the bot and the web UI each keep their own)."""
        with self.lock:
            key = (model, *self.con.execute("SELECT count(*), max(chunk) FROM vectors WHERE model = ?",
                                            (model,)).fetchone())
            with self._cache_lock:
                if self._cache and self._cache[0] == key:
                    return self._cache[1:]
            rows = self.con.execute("SELECT v.chunk, c.doc, v.vec FROM vectors v JOIN chunks c ON c.id = v.chunk"
                                    " WHERE v.model = ? ORDER BY v.chunk", (model,)).fetchall()
        ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
        docs = np.fromiter((r[1] for r in rows), dtype=np.int64, count=len(rows))
        matrix = (np.vstack([np.frombuffer(r[2], dtype=np.float32) for r in rows]) if rows
                  else np.zeros((0, 1), np.float32))
        with self._cache_lock:
            self._cache = (key, ids, docs, matrix)
        return ids, docs, matrix

    @db.locked
    def _chunks(self, ids: list[int]) -> dict[int, tuple]:
        """chunk id → (doc, page, start, end, heading, text)."""
        if not ids:
            return {}
        rows = self.con.execute(f"SELECT id, doc, page, start, end, heading, text FROM chunks WHERE id IN"
                                f" ({_marks(ids)})", ids)
        return {r[0]: r[1:] for r in rows}

    @db.locked
    def _passages(self, picked: list[int], rows: dict[int, tuple], via: dict[int, str]) -> list[Passage]:
        """Hits as passages, best first; two hits that touch or overlap on one page become one passage."""
        spans: list[list] = []  # [doc, page, start, end, heading, via, first rank]
        for rank, c in enumerate(picked):
            doc, page, start, end, head, _ = rows[c]
            near = next((s for s in spans if s[0] == doc and s[1] == page and start <= s[3] and end >= s[2]), None)
            if near:
                near[2], near[3] = min(near[2], start), max(near[3], end)
                near[5] = near[5] if near[5] == via[c] else "both"
            else:
                spans.append([doc, page, start, end, head, via[c], rank])
        docs = {d: self.get(d) for d in {s[0] for s in spans}}
        texts = {(d, n): t for d, n, t in self.con.execute(
            f"SELECT doc, n, text FROM pages WHERE doc IN ({_marks(docs)})", list(docs))} if docs else {}
        self._touch(docs)
        return [Passage(docs[d], n, head, texts[(d, n)][s:e].strip(), v) for d, n, s, e, head, v, _ in spans]

    # ---- background vectors ----
    @db.locked
    def pending(self) -> int:
        """Chunks without a vector from the embedding server's current model."""
        model = self.embedder and self.embedder.model()
        if not model:
            return 0
        return self.con.execute("SELECT count(*) FROM chunks c LEFT JOIN vectors v ON v.chunk = c.id AND v.model = ?"
                                " WHERE v.chunk IS NULL", (model,)).fetchone()[0]

    def fill(self, batch: int = 16, max_s: float = 30.0, quiet: Callable[[], bool] = lambda: True) -> int:
        """Embed chunks that have no vector yet, oldest first, for up to `max_s` seconds and while `quiet()` says
        the chat is idle; returns how many. Each batch commits, so a stop loses nothing."""
        if not self.embedder:
            return 0
        done, started = 0, time.monotonic()
        while time.monotonic() - started < max_s and quiet():
            model = self.embedder.model()
            if not model:
                break
            with self.lock:
                rows = self.con.execute(
                    "SELECT c.id, c.title, c.heading, c.text FROM chunks c LEFT JOIN vectors v ON v.chunk = c.id"
                    " AND v.model = ? WHERE v.chunk IS NULL ORDER BY c.id LIMIT ?", (model, batch)).fetchall()
            if not rows:
                break
            got = self.embedder.embed([self.embedder.document(" › ".join(filter(None, (t, h))), text)
                                       for _, t, h, text in rows])
            if got is None:
                break
            with self.lock:
                self.con.executemany("INSERT OR REPLACE INTO vectors (chunk, model, vec) VALUES (?, ?, ?)",
                                     [(r[0], model, v.tobytes()) for r, v in zip(rows, got)])
                self.con.commit()
            done += len(rows)
        if done:
            log.info("library: %d chunks embedded in %.1f s", done, time.monotonic() - started)
        return done

    # ---- nightly ----
    def expire(self) -> tuple[list[str], int]:
        """Drop documents nobody kept or used for KEEP_DAYS; (their titles, bytes of originals freed)."""
        cutoff = (self.clock() - timedelta(days=KEEP_DAYS)).isoformat(timespec="minutes")
        with self.lock:
            old = self.con.execute("SELECT id, title, sha256, file FROM documents WHERE kept = 0 AND last_used < ?",
                                   (cutoff,)).fetchall()
            for doc, *_ in old:
                self._delete(doc)
            self.con.commit()
        freed = 0
        for _, _, sha, file in old:
            path = self.files / sha
            if file and path.exists():
                freed += path.stat().st_size
                path.unlink()
        return [t for _, t, _, _ in old], freed

    def _delete(self, doc: int) -> None:
        self.con.execute("DELETE FROM vectors WHERE chunk IN (SELECT id FROM chunks WHERE doc = ?)", (doc,))
        self.con.execute("DELETE FROM chunks WHERE doc = ?", (doc,))  # the trigger clears the FTS rows
        self.con.execute("DELETE FROM pages WHERE doc = ?", (doc,))
        self.con.execute("DELETE FROM documents WHERE id = ?", (doc,))

    @db.locked
    def stats(self) -> str:
        docs, chunks = self.con.execute("SELECT count(*), (SELECT count(*) FROM chunks) FROM documents").fetchone()
        vectors = self.con.execute("SELECT count(*) FROM vectors").fetchone()[0]
        return f"{docs} documents, {chunks} passages, {vectors} with vectors"


def _marks(values) -> str:
    return ", ".join("?" * len(values))


# ---- tools ------------------------------------------------------------------------------------------------------
def toolset(library: Library, route: str, page_chars: int, classes: tuple[str, ...] | None = None) -> FunctionToolset:
    """search_documents, read_document, list_documents and keep_document over the documents `route` may see
    (privacy.SEES; `classes` narrows it for a run off this machine, privacy.where_sees). What they return was written
    by others, so a run that called one is tainted (guards.UNTRUSTED)."""
    privacy = sees(route) if classes is None else classes
    tools = FunctionToolset()

    def opened(document: int) -> Document:
        doc = library.get(document, privacy)
        if doc is None:
            raise ModelRetry(f"There is no document #{document} you can read. list_documents shows them.")
        return doc

    @tools.tool_plain
    def search_documents(query: str, document: int | None = None, since: str | None = None) -> str:
        """Search the documents the user sent or you read before (contracts, policies, bills, statements, manuals,
        scans), by words and by meaning, and get the best passages with document and page.

        Args:
            query: What you look for, as a question or a few words, in the documents' language ('entro quando posso
                disdire la polizza', 'IBAN', 'spese riscaldamento 2025').
            document: Only this document, by its number (#12 → 12).
            since: Only documents added on or after this date (YYYY-MM-DD).
        """
        if document is not None:
            opened(document)
        hits = library.search(query, privacy, document, since)
        if not hits:
            return f"Nothing in the documents matches {query!r}. Try other words, or list_documents."
        return ("\n\n".join(h.block() for h in hits)
                + "\n\n[Passages only: read_document for a whole page. Cite as (#N, p. P).]")

    @tools.tool_plain
    def read_document(document: int, page: int = 1) -> str:
        """Read one page of a saved document.

        Args:
            document: The document's number (#12 → 12).
            page: Which page, from 1; the answer says how many there are.
        """
        doc = opened(document)
        if not 1 <= page <= doc.pages:
            raise ModelRetry(f"#{doc.id} has {doc.pages} page(s); page {page} does not exist.")
        text = library.page(doc.id, page) or ""
        if len(text) > page_chars:
            text = text[:page_chars] + "\n[page cut here: search_documents finds the rest]"
        more = (f"\n[page {page} of {doc.pages}; read_document with page={page + 1} for the next]"
                if page < doc.pages else "")
        return f"[#{doc.id} {doc.title}, page {page}/{doc.pages}]\n{text}{more}"

    @tools.tool_plain
    def list_documents(query: str | None = None) -> str:
        """The saved documents, most recently used first; with `query`, the ones whose passages match it.

        Args:
            query: Words to look for ('polizza', 'condominio'); leave it out for the latest documents.
        """
        if query:
            docs = list(dict.fromkeys(h.doc for h in library.search(query, privacy, limit=LISTED)))
        else:
            docs = library.documents(privacy)
        if not docs:
            return "No saved documents" + (f" match {query!r}." if query else " yet.")
        return "\n".join(d.line() for d in docs)

    @tools.tool_plain
    def keep_document(document: int) -> str:
        """Keep a document for good when the user asks ('tienilo', 'conserva questo documento'). Unkept documents
        are forgotten after 180 days without use.

        Args:
            document: The document's number (#12 → 12).
        """
        doc = library.keep(opened(document).id)
        return f"Kept: {doc.line()}"

    return tools
