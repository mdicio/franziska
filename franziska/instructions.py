"""The agents' standing rules, editable by the user (web UI, Memory → Rules).

Each agent's rules are its INSTRUCTIONS in code until the user edits them: the edit is saved as
`<data_dir>/instructions/<agent>.md` and wins from the next turn on, in the bot and the web UI alike (both read the
file each turn). Saving the code's text again, or an empty text, removes the file: the code's rules apply again,
including later changes to them. An edit remembers which built-in rules it started from (`<agent>.base`), so the
UI can say when those changed after it (`stale`). The per-turn parts (today's date, the memory index) are not here: they are facts,
not rules.

Only the bot and the web UI set DIR (Core); evals, the MCP server and the CLIs run on the code's rules.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

DIR: Path | None = None
DEFAULTS: dict[str, str] = {}  # agent name → the code's rules, registered by each agent module
TITLES = {"life": "📅 Life", "money": "💶 Money", "mail": "✉️ Mail", "chat": "💬 Chat", "web": "🌐 Web"}


def register(name: str, default: str) -> None:
    DEFAULTS[name] = default


def path(name: str) -> Path | None:
    return DIR / f"{name}.md" if DIR else None


def text(name: str) -> str:
    """The rules `name` runs on this turn: the user's edit if there is one, else the code's."""
    p = path(name)
    if p is not None:
        try:
            edited = p.read_text().strip()
        except FileNotFoundError:
            edited = ""
        if edited:
            return edited
    return DEFAULTS[name]


def edited(name: str) -> bool:
    p = path(name)
    return p is not None and p.exists()


def _hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()[:16]


def stale(name: str) -> bool:
    """The user's edit was made on built-in rules that have changed since: it runs without the newer ones (money ran
    on an old copy without search_documents and run_python, monthsim 2026-10-31). An edit saved before the base was
    recorded counts as stale. The web UI says so; nothing is merged by itself."""
    p = path(name)
    if p is None or not p.exists():
        return False
    try:
        return p.with_suffix(".base").read_text().strip() != _hash(DEFAULTS[name])
    except FileNotFoundError:
        return True


def save(name: str, new: str) -> str:
    """The user's edit; the code's text (or nothing) resets to it."""
    if name not in DEFAULTS:
        raise KeyError(name)
    p = path(name)
    if p is None:
        raise RuntimeError("instructions.DIR is not set")
    new = new.strip()
    if not new or new == DEFAULTS[name].strip():
        p.unlink(missing_ok=True)
        p.with_suffix(".base").unlink(missing_ok=True)
        log.info("instructions %s: back to the code's", name)
        return f"{TITLES.get(name, name)}: back to the built-in rules."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new + "\n")
    p.with_suffix(".base").write_text(_hash(DEFAULTS[name]) + "\n")  # which built-in rules it was edited from
    log.info("instructions %s: edited (%d chars)", name, len(new))
    return f"{TITLES.get(name, name)}: saved. It applies from the next message."
