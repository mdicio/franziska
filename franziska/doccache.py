"""Extracted document text, kept so a file is read once, and cut into pages and passages for the model.

Reading a scan is seconds of CPU (OCR), and a bill gets asked about more than once. The text of an attachment is kept
under data/cache/docs/<key>.txt, keyed by the message, file name and size (Gmail's attachment ids change between
calls, a message never does). Nothing is kept for a file that gave no text, so installing a missing reader fixes it.

The model sees a document in pages of about `Budget.attachment_chars` (read_attachment) or through `passages`, the best
few short excerpts for a question (search_attachment). That is word matching (fts.py), no embeddings: a contract of
thirty pages costs a few excerpts, not thirty pages.

Cache files are touched when read, so the nightly `expire` drops what wasn't used for KEEP_DAYS, then the least
recently used until the folder is under MAX_BYTES.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from franziska import fts

log = logging.getLogger(__name__)

KEEP_DAYS = 90
MAX_BYTES = 200 * 1024 * 1024
PASSAGES = 3  # excerpts a search returns


class DocCache:
    def __init__(self, root: Path, clock: Callable[[], datetime]):
        self.root, self.clock = root, clock

    def path(self, *parts: object) -> Path:
        return self.root / (hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32] + ".txt")

    def get(self, *parts: object) -> str | None:
        path = self.path(*parts)
        try:
            text = path.read_text(encoding="utf-8")
            os.utime(path)  # used just now: expiry goes by this
            return text
        except OSError:
            return None

    def put(self, text: str, *parts: object) -> None:
        if not text.strip():
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.path(*parts).write_text(text, encoding="utf-8")
        except OSError:
            log.exception("doc cache: could not write")

    def expire(self) -> int:
        """Delete files unused for KEEP_DAYS, then the oldest until the folder fits MAX_BYTES; returns how many."""
        if not self.root.is_dir():
            return 0
        cutoff = (self.clock() - timedelta(days=KEEP_DAYS)).timestamp()
        files = sorted(((f.stat().st_mtime, f.stat().st_size, f) for f in self.root.glob("*.txt")), key=lambda t: t[0])
        total, gone = sum(size for _, size, _ in files), 0
        for mtime, size, f in files:
            if mtime >= cutoff and total <= MAX_BYTES:
                break
            f.unlink(missing_ok=True)
            total, gone = total - size, gone + 1
        return gone


def chunks(text: str, size: int) -> list[tuple[int, str]]:
    """`text` in pieces of about `size` characters, cut at a paragraph or line end when there is one: (offset, piece)."""
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start + size // 2, end), text.rfind("\f", start + size // 2, end),
                      text.rfind("\n", start + size // 2, end))
            end = cut + 1 if cut > 0 else end
        piece = text[start:end].strip()
        if piece:
            out.append((start, piece))
        start = end
    return out


def passages(text: str, query: str, size: int, limit: int = PASSAGES) -> list[tuple[int, str]]:
    """The `limit` pieces of about `size` characters that share the most words with `query`, in reading order."""
    want = set(fts.words(query))
    if not want:
        return []
    scored = []
    for offset, piece in chunks(text, size):
        score = len(want & set(fts.words(piece)))
        if score:
            scored.append((score, offset, piece))
    best = sorted(scored, key=lambda t: (-t[0], t[1]))[:limit]
    return [(offset, piece) for _, offset, piece in sorted(best, key=lambda t: t[1])]


def key_of(message_id: str, filename: str, size: int) -> tuple[str, str, int]:
    return message_id, re.sub(r"\s+", " ", filename).strip().lower(), size
