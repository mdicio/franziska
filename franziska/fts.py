"""Word matching shared by the stores that search text: notes, the turn log, memory names (DESIGN §6).

No stemmer and no embeddings: accents are folded, short and stop words dropped, and words of 5+ letters lose their
last letter, so Italian singular and plural meet ('scarpa' finds 'scarpe', 'dentista' meets 'dentist').
"""

import re
import unicodedata

# Words that match almost everything; 1-2 letter words are dropped anyway.
STOP = frozenset({"del", "della", "dei", "degli", "delle", "gli", "che", "per", "con", "una", "uno", "nel", "nella",
                  "the", "and", "for", "you"})


def fold(text: str) -> str:
    """'Città' → 'citta': accents off, lowercase."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def words(text: str) -> list[str]:
    """The searchable words of `text`, folded and trimmed, in order, without repeats."""
    out = [w[:-1] if len(w) >= 5 else w for w in re.findall(r"\w+", fold(text)) if len(w) > 2 and w not in STOP]
    return list(dict.fromkeys(out))


def match(text: str) -> str | None:
    """An FTS5 query matching any word of `text` as a prefix; None when no word is left."""
    found = words(text)
    return " OR ".join(f'"{w}"*' for w in found) if found else None  # quoted: user text can't inject FTS5 syntax
