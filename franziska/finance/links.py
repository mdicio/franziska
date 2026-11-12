"""Money moved between the user's own accounts: find both sides and link them, so neither counts as income or spending.

Deterministic, no model. Two kinds:
- savings_move: Revolut current ↔ Revolut savings. Both sides are written in the same second with opposite
  amounts, and both texts name the savings account ('To Instant Access Savings'). Also a pair of that shape on
  the savings product alone (out and straight back in), which Revolut writes now and then.
- own_transfer: between banks (Intesa ↔ Revolut). Opposite amounts to the cent, the inflow 0–3 days after the
  outflow (instant transfers land the same day, ordinary ones in 1–2 working days, card top-ups at once), and a sign
  that the money is the user's own:
    · Revolut's inflow is a top-up from the user: 'Payment from <owner>', 'Apple Pay deposit', 'Open banking
      deposit', 'Top-up';
    · or Intesa's outflow names Revolut or the owner ('A favore di: <owner>', a card top-up 'REVOLUT');
    · or, for Revolut → Intesa, Revolut's outflow is 'To <owner>' or Intesa's inflow comes 'disposto da: <owner>'.
  "REVOLUT" in an Intesa *inflow* is no sign: friends paying from their own Revolut write the same.
Every link is one to one. When an outflow has two possible inflows (or the reverse), nothing is linked and the pair
is reported, so a wrong guess never hides real spending.

The signs above are bank wording, which changes. So a pair of that shape without a sign is not dropped silently
(it would count twice, as spending and as income): `candidates` lists it, and the user links it with a tap (`link`,
by 'user').
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime

MAX_DAYS = 3
SAVINGS = re.compile(r"savings", re.IGNORECASE)
TOPUP = re.compile(r"^(?:Apple Pay deposit|Google Pay deposit|Open banking deposit|Top-up|Card top-up)", re.IGNORECASE)


def owner_words(owner: str) -> set[str]:
    """The owner's name as the words a bank text must all contain ('Davide Moretti' → {'DAVIDE', 'MORETTI'})."""
    return {w for w in re.sub(r"[^A-Z ]", " ", owner.upper()).split() if len(w) >= 3}


def _names(text: str, words: set[str]) -> bool:
    found = set(re.sub(r"[^A-Z ]", " ", text.upper()).split())
    return bool(words) and words <= found


def _rows(con: sqlite3.Connection) -> list[tuple]:
    return con.execute(
        "SELECT id, account, booked_on, occurred_at, amount_cents, description || ' ' || COALESCE(details, '')"
        " FROM movements WHERE account != 'cash' ORDER BY booked_on, occurred_at").fetchall()


def _own_sign(out: tuple, inn: tuple, words: set[str]) -> str | None:
    """Why this outflow → inflow is the user's own money, or None."""
    (_, out_acc, *_, out_text), (_, in_acc, *_, in_text) = out, inn
    if in_acc.startswith("revolut"):
        if TOPUP.match(in_text) or (in_text.startswith("Payment from") and _names(in_text, words)):
            return f"revolut top-up: {in_text[:60]}"
        if out_acc == "intesa" and ("REVOLUT" in out_text.upper() or _names(out_text, words)):
            return "intesa outflow to revolut / the owner"
    if out_acc.startswith("revolut") and in_acc == "intesa":
        if _names(out_text, words) or _names(in_text, words):
            return "revolut → intesa, the owner on one side"
    return None


def link_all(con: sqlite3.Connection, owner: str, now: datetime) -> dict:
    """Link what can be linked among the unlinked movements. Returns counts and the ambiguous pairs."""
    words = owner_words(owner)
    rows = _rows(con)
    out_rows = [r for r in rows if r[4] < 0]
    in_rows = [r for r in rows if r[4] > 0]
    pairs: list[tuple[tuple, tuple, str, str]] = []
    for o in out_rows:
        for i in in_rows:
            if i[4] != -o[4]:
                continue
            same_second = o[3] and i[3] and abs((datetime.fromisoformat(o[3]) - datetime.fromisoformat(i[3]))
                                                .total_seconds()) <= 60
            both_savings = bool(SAVINGS.search(o[5]) and SAVINGS.search(i[5]))
            if i[1] == o[1]:
                # Revolut sometimes writes a savings move as out and back in on the savings product itself
                # (e.g. 250.00 € out and in within the same second): net zero, neither income nor spending.
                if o[1] == "revolut_savings" and same_second and both_savings:
                    pairs.append((o, i, "savings_move", "out and back in on the savings account, same second"))
                continue
            if {o[1], i[1]} == {"revolut", "revolut_savings"}:
                if same_second and both_savings:
                    pairs.append((o, i, "savings_move", "same second, both name the savings account"))
                continue
            lag = (date.fromisoformat(i[2]) - date.fromisoformat(o[2])).days
            if 0 <= lag <= MAX_DAYS and (why := _own_sign(o, i, words)):
                pairs.append((o, i, "own_transfer", f"{why}; {lag} day(s) apart"))
    by_out: dict[str, int] = {}
    by_in: dict[str, int] = {}
    for o, i, *_ in pairs:
        by_out[o[0]] = by_out.get(o[0], 0) + 1
        by_in[i[0]] = by_in.get(i[0], 0) + 1
    linked = {"savings_move": 0, "own_transfer": 0}
    ambiguous = []
    for o, i, kind, why in pairs:
        if by_out[o[0]] > 1 or by_in[i[0]] > 1:
            ambiguous.append(f"{o[2]} {o[1]} → {i[1]} {-o[4] / 100:.2f} €")
            continue
        con.execute("INSERT OR IGNORE INTO links VALUES (?, ?, ?, 'matcher', ?, ?)",
                    (o[0], i[0], kind, json.dumps({"why": why}), now.isoformat(timespec="seconds")))
        linked[kind] += 1
    con.commit()
    return {**linked, "ambiguous": sorted(set(ambiguous))}


def candidates(con: sqlite3.Connection, owner: str) -> list[tuple[tuple, tuple]]:
    """Unlinked (outflow, inflow) pairs between two of the user's bank accounts that look like one transfer (opposite
    amounts to the cent, the inflow 0–MAX_DAYS days later) but carry no sign that the money is the user's own. One to
    one only: an outflow with two such inflows (or the reverse) is left out. Rows: (id, account, booked_on, ...)."""
    words = owner_words(owner)
    rows = _rows(con)
    found = []
    for o in (r for r in rows if r[4] < 0):
        for i in (r for r in rows if r[4] == -o[4] and r[1] != o[1]):
            if o[1].startswith("intesa") == i[1].startswith("intesa"):
                continue  # within one bank: a savings move, matched above, or nothing
            lag = (date.fromisoformat(i[2]) - date.fromisoformat(o[2])).days
            if 0 <= lag <= MAX_DAYS and _own_sign(o, i, words) is None:
                found.append((o, i))
    outs, ins = Counter(o[0] for o, _ in found), Counter(i[0] for _, i in found)
    return [(o, i) for o, i in found if outs[o[0]] == 1 and ins[i[0]] == 1]


def link(con: sqlite3.Connection, out_id: str, in_id: str, now: datetime) -> bool:
    """Link two movements as the user's own transfer (the user's word). False when either is linked already."""
    cur = con.execute("INSERT OR IGNORE INTO links VALUES (?, ?, 'own_transfer', 'user', ?, ?)",
                      (out_id, in_id, json.dumps({"why": "confirmed by the user"}), now.isoformat(timespec="seconds")))
    con.commit()
    return cur.rowcount == 1


def unmatched_topups(con: sqlite3.Connection, owner: str) -> list[tuple[str, int]]:
    """Revolut inflows that are the user's own money with no Intesa side yet: (day, cents). Usually an Intesa gap."""
    words = owner_words(owner)
    return [(r[2], r[4]) for r in _rows(con) if r[1] == "revolut" and r[4] > 0
            and (TOPUP.match(r[5]) or (r[5].startswith("Payment from") and _names(r[5], words)))]
