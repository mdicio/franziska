"""Spending groups: what a payment was for, at the level of a monthly review (food & drink, travel, ...).

Two answers per movement, kept apart:
- `category` is authoritative and code never overwrites it: a fact the bank's own fields state (salary, interest, an
  ATM withdrawal: `category_by` 'bank'), a cash entry's ('cash'), or the user's word for the payee ('user').
- `proposed_category` is the local model's guess where there is no authoritative one. Reports count it like a
  category (the `effective_category` of `movements`); the user can confirm or correct it, which makes it 'user'.

Both are decided per payee (`payee`: merchants.key, the counterparty without branch or city), never per payment, and
the decisions live in `payees`. So a new payment of a known payee gets its group on import with no model call, and
Money.categorise asks the model only about payees it has never seen: one AnyJev choice each, in batches.

Measured 2026-10-28 on gemma4-26b-a4b (AnyJev L0, adaptive shifts): 49/50 payees right against the old substring
rules' labels, ~2 s per payee. Its probability saturates near 1.00 right or wrong, so it is stored, never used as a
threshold: what to review is ordered by money instead (Money.review).
"""

import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import datetime

from franziska.finance import merchants

# What the model chooses from: the key, then a short description (as router.ROUTES: bare labels get confused).
GROUPS = {
    "food & drink": "food & drink: supermarkets, food shops, bakeries, restaurants, cafés, bars, pubs, food delivery",
    "tobacco": "tobacco: tabaccherie, cigarettes, vaping",
    "transport": "transport: taxi, ride hailing, scooter or bike sharing, public transport, fuel, parking, tolls",
    "travel": "travel: flights, trains, ferries, hotels, holiday rentals, travel agencies, ski passes, trips abroad",
    "shopping": "shopping: clothes, shoes, electronics, online shops, household goods",
    "home & bills": "home & bills: rent, condominium, repairs, electricity, gas, water, phone, internet, insurance",
    "subscriptions": "subscriptions: streaming, music, software, cloud, AI tools, apps, memberships",
    "health & care": "health & care: doctors, dentists, clinics, pharmacies, hairdresser, barber, beauty",
    "leisure": "leisure: clubs, discos, events and tickets, concerts, cinema, museums, sport, gyms, beach clubs",
    "people & gifts": "people & gifts: money sent to a friend or another person, presents, donations",
    "taxes & fees": "taxes & fees: taxes, PagoPA, stamp duty, fines, bank fees",
}
QUESTION = "Which spending group does this payment belong to?"
_BY_OPTION = {v: k for k, v in GROUPS.items()}

# Read from the bank's own fields, so never a guess. Checked in this order.
FACTS = {
    "interest": "kind = 'interest'",
    "cash withdrawal": "merchant = 'ATM' OR upper(description) LIKE 'PRELIEVO%' OR bank_category = 'Prelievi'",
    "salary": "amount_cents > 0 AND (instr(upper(description || ' ' || COALESCE(details, '')), 'STIPENDIO') > 0"
              " OR bank_category = 'Stipendi e pensioni')",
}

_CARD = re.compile(r"Carta\s*N?\.?\s*\d{4}.*?(?:COD\.?\s*[\d/]+|$)|MEDIANTE LA CARTA [\dX ]+|ABI\s*:?\s*\d+", re.I)
_PAYPAL_PERSON = re.compile(r"^PAYPAL \*[A-Z]+\d{1,4}$")  # 'PAYPAL *GIULIA88': a personal account, not a shop


def payee_of(merchant: str | None, description: str) -> str | None:
    """The key decisions are stored under: the merchant without branch or city, else the description's."""
    name = merchant or merchants.normalise(description)
    return merchants.key(name) if name else None


def apply(con: sqlite3.Connection) -> None:
    """Facts first, then the payees' decisions: the user's as `category`, the model's as `proposed_category`."""
    fact = " ".join(f"WHEN {cond} THEN '{name}'" for name, cond in FACTS.items())
    con.execute(f"UPDATE transactions SET category = CASE {fact} END, category_by = 'bank'"
                f" WHERE category_by IS NULL AND account != 'cash' AND (CASE {fact} END) IS NOT NULL")
    con.execute("""
        UPDATE transactions SET category = p.category, category_by = 'user' FROM payees p
        WHERE p.payee = transactions.payee AND p.by = 'user' AND account != 'cash'
          AND COALESCE(category_by, 'user') = 'user'""")
    con.execute("""
        UPDATE transactions SET proposed_category = (
            SELECT category FROM payees p WHERE p.payee = transactions.payee AND p.by = 'model')
        WHERE account != 'cash'""")


def pending(con: sqlite3.Connection) -> list[str]:
    """Payees of spending with no group at all and no decision yet, the most money first."""
    return [r[0] for r in con.execute(
        "SELECT payee FROM movements WHERE amount_cents < 0 AND account != 'cash' AND category IS NULL"
        " AND payee IS NOT NULL AND payee NOT IN (SELECT payee FROM payees)"
        " GROUP BY payee ORDER BY SUM(amount_cents)")]


def describe(con: sqlite3.Connection, payee: str) -> str:
    """What the model reads about one payee: the bank's text with card numbers removed, how often and how much,
    the hour, and the bank's own category when it gives one."""
    rows = con.execute(
        "SELECT COALESCE(merchant, description), description, details, account, amount_cents, kind, bank_category,"
        " substr(occurred_at, 12, 2) FROM movements WHERE payee = ? AND amount_cents < 0 ORDER BY booked_on DESC",
        (payee,)).fetchall()
    name, desc, details, account, *_ = rows[0]
    amounts = sorted(-r[4] / 100 for r in rows)
    hours = Counter(r[7] for r in rows if r[7])
    text = " ".join(_CARD.sub(" ", f"{desc} | {details or ''}").split())[:180]
    bank = "Revolut" if account.startswith("revolut") else "Intesa Sanpaolo"
    lines = [f"A payment out of the user's {bank} account (the user lives in Rome, Italy).",
             f"Payee: {name}", f"Bank text: {text}",
             f"Paid {len(rows)} time(s), {amounts[0]:.2f}–{amounts[-1]:.2f} EUR"
             + (f", usually around {hours.most_common(1)[0][0]}:00" if hours else "")]
    if rows[0][5] == "transfer_out":
        lines.append("It is a bank transfer to someone, not a card payment.")
    if _PAYPAL_PERSON.match(name):
        lines.append("The PayPal payee looks like a personal account (a name with digits), not a shop.")
    if hint := next((r[6] for r in rows if r[6]), None):
        lines.append(f"The bank's own category (often vague): {hint}")
    return "\n".join(lines)


def record(con: sqlite3.Connection, answers: Iterable[tuple[str, dict[str, float]]], model: str,
           now: datetime) -> None:
    """Store the model's choice per payee (its distribution over GROUPS' options) and apply it. A payee the user
    has decided meanwhile keeps the user's answer."""
    rows = []
    for payee, dist in answers:
        option = max(dist, key=dist.get)
        rows.append((payee, _BY_OPTION[option], round(dist[option], 3), model, now.isoformat(timespec="seconds")))
    con.executemany("INSERT OR IGNORE INTO payees VALUES (?, ?, 'model', ?, ?, ?)", rows)
    apply(con)


def decider(router) -> Callable[[list[str]], list[dict[str, float]]]:
    """Money.categorise's `decide` on the local model: QUESTION over GROUPS for each description."""
    return lambda texts: router.choose_many(texts, QUESTION, list(GROUPS.values()))
