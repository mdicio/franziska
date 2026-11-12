"""The ledger: one SQLite file, amounts in integer cents; categories per payee (categories.py).

The LLM never computes totals itself. It writes SELECTs against the `tx` view and reads the result;
those SELECTs run on a read-only connection with an authorizer that only permits reads.
"""

import hashlib
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from franziska.finance import categories, merchants

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT PRIMARY KEY,    -- content hash, so re-importing an overlapping export is a no-op
    account      TEXT NOT NULL,       -- ACCOUNTS below
    booked_on    TEXT NOT NULL,       -- ISO date
    value_on     TEXT,
    amount_cents INTEGER NOT NULL,    -- > 0 income, < 0 expense; Revolut: amount − fee, the effect on the balance
    description  TEXT NOT NULL,
    details      TEXT,
    category     TEXT,                -- authoritative (categories.py): a bank fact, a cash entry's, or the user's
    source       TEXT NOT NULL,       -- import file name, or 'chat'
    merchant     TEXT,                -- counterparty, normalised (merchants.py); NULL if unknown
    occurred_at  TEXT,                -- when it happened, if the bank says (Revolut's Started Date)
    kind         TEXT,                -- card, refund, interest, topup, transfer_in, transfer_out; NULL: not known
    fee_cents    INTEGER NOT NULL DEFAULT 0,
    balance_after_cents INTEGER,      -- the bank's running balance after this movement (Revolut)
    source_format TEXT,               -- importer.INTESA_OPS / INTESA / REVOLUT; NULL for cash
    bank_category TEXT,               -- the bank's own category (Intesa), a hint for ours, never the answer
    payee        TEXT,                -- categories.payee_of: the unit categories are decided for
    category_by  TEXT,                -- who set `category`: 'bank', 'cash' or 'user'; NULL while it is NULL
    proposed_category TEXT            -- the local model's guess for the payee (categories.GROUPS)
);
-- One row per imported file, with what was checked: which periods each account's data covers.
CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY,
    file        TEXT NOT NULL,
    format      TEXT NOT NULL,
    account     TEXT NOT NULL,
    period_from TEXT NOT NULL,
    period_to   TEXT NOT NULL,
    rows        INTEGER NOT NULL,
    new         INTEGER NOT NULL,
    checks      TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
-- The balance the bank states for an account at the end of a day: the truth computed figures are checked against.
CREATE TABLE IF NOT EXISTS balances (
    account TEXT NOT NULL,
    on_date TEXT NOT NULL,
    cents   INTEGER NOT NULL,
    source  TEXT NOT NULL,
    PRIMARY KEY (account, on_date)
);
-- Two movements that are one: money moved between the user's own accounts. Linked rows are neither income nor
-- spending (they leave the `tx` view and appear in `transfers`). `by`: 'matcher' (links.py) or 'user'.
CREATE TABLE IF NOT EXISTS links (
    out_tx     TEXT PRIMARY KEY REFERENCES transactions(id),
    in_tx      TEXT NOT NULL UNIQUE REFERENCES transactions(id),
    kind       TEXT NOT NULL,         -- 'savings_move' (Revolut ↔ its savings) or 'own_transfer' (between banks)
    by         TEXT NOT NULL,
    evidence   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- What each payee is: the local model's guess ('model') or the user's word ('user', authoritative).
CREATE TABLE IF NOT EXISTS payees (
    payee      TEXT PRIMARY KEY,
    category   TEXT NOT NULL,         -- a key of categories.GROUPS
    by         TEXT NOT NULL,         -- 'model' | 'user'
    p          REAL,                  -- the model's probability: saturated near 1, not a confidence
    model      TEXT,
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budgets (
    category      TEXT PRIMARY KEY,
    monthly_cents INTEGER NOT NULL    -- spending limit per calendar month, > 0
);
-- A payment the user expects to make. Its due date and reminder live in the secretary's task (life.sqlite);
-- the amount stays here, so cloud clients of the life MCP server never see it.
CREATE TABLE IF NOT EXISTS bills (
    id           INTEGER PRIMARY KEY,
    payee        TEXT NOT NULL,
    pattern      TEXT NOT NULL,       -- substring of the bank text that identifies the payment
    amount_cents INTEGER NOT NULL,    -- expected, > 0
    task_id      INTEGER NOT NULL,    -- tasks.id in life.sqlite
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bill_payments (
    tx_id   TEXT PRIMARY KEY REFERENCES transactions(id),  -- a payment settles at most one bill occurrence
    bill_id INTEGER NOT NULL REFERENCES bills(id),
    due_on  TEXT NOT NULL
);
-- Movements that are real income or spending: every row except the two sides of a link. `effective_category` is
-- what reports count: the authoritative category, else the model's guess.
DROP VIEW IF EXISTS movements;
CREATE VIEW movements AS
    SELECT *, COALESCE(category, proposed_category) AS effective_category FROM transactions
    WHERE id NOT IN (SELECT out_tx FROM links) AND id NOT IN (SELECT in_tx FROM links);
DROP VIEW IF EXISTS tx;
CREATE VIEW tx AS
    SELECT booked_on, account, amount_cents / 100.0 AS amount_eur, COALESCE(merchant, '') AS merchant,
           description, details, COALESCE(effective_category, 'uncategorised') AS category,
           category IS NULL AND proposed_category IS NOT NULL AS guessed, COALESCE(kind, '') AS kind
    FROM movements;
DROP VIEW IF EXISTS transfers;
CREATE VIEW transfers AS
    SELECT o.booked_on, l.kind, o.account AS from_account, i.account AS to_account, -o.amount_cents / 100.0 AS amount_eur
    FROM links l JOIN transactions o ON o.id = l.out_tx JOIN transactions i ON i.id = l.in_tx;
"""

# Where money sits. 'bank' was the single account before Revolut (renamed on open; ids keep hashing as 'bank').
ACCOUNTS = {"intesa": "Intesa Sanpaolo current account", "intesa_card": "Intesa prepaid card (XME Card)",
            "revolut": "Revolut current account",
            "revolut_savings": "Revolut Instant Access Savings", "cash": "cash"}
LEGACY_ACCOUNT = {"intesa": "bank"}

# What the model is told about the data. Keep in sync with the `tx` view above.
TX_VIEW_DOC = """\
View tx(booked_on TEXT 'YYYY-MM-DD', account TEXT 'intesa'|'intesa_card'|'revolut'|'revolut_savings'|'cash',
        amount_eur REAL (>0 income, <0 expense), merchant TEXT (upper case, e.g. 'DELIVEROO MILANO', 'WIND TRE';
        '' if unknown), description TEXT, details TEXT, category TEXT (a spending group such as 'food & drink',
        or 'salary' / 'interest' / 'cash withdrawal'; 'uncategorised' when there is none), guessed INTEGER (1: the
        category is the model's guess for the payee, not confirmed by the user),
        kind TEXT ('card', 'refund', 'interest', 'topup', 'transfer_in', 'transfer_out', '' if unknown)).
Cash the user reported spending or receiving is account = 'cash' (kind says nothing about cash).
tx is real income and spending only: money moved between the user's own accounts is not in it.
View transfers(booked_on, kind 'own_transfer'|'savings_move', from_account, to_account, amount_eur) holds those.
Intesa descriptions are Italian bank text ('PAGAMENTO TRAMITE POS ...', 'BONIFICO ...'), Revolut's are the
merchant or 'To <name>' / 'Payment from <name>'. Filter merchants with LIKE '%DELIVEROO%' rather than equality."""

MAX_ROWS = 50


class LedgerError(RuntimeError):
    """The ledger could not be read. Never reported to the model as 'no rows'."""


@dataclass(frozen=True)
class Transaction:
    account: str
    booked_on: date
    value_on: date | None
    amount_cents: int
    description: str
    details: str
    source: str
    category: str | None = None  # cash entries state theirs; bank rows get one per payee (categories.py)
    occurred_at: datetime | None = None
    kind: str | None = None
    fee_cents: int = 0
    balance_after_cents: int | None = None
    source_format: str | None = None
    bank_category: str | None = None
    merchant: str | None = None  # the importer's own; else derived from the text (merchants.of)


def connect(path: Path, migrate: bool = False) -> sqlite3.Connection:
    """A connection to the ledger. `migrate` (once per process, Money.open): create the schema, add the columns older
    ledgers lack, recreate the views and seed the rules. Not on every call: between dropping and recreating a view, a
    query on another connection finds no `tx`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")  # the bot and the CLI may write at once
    if not migrate:
        return con
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'transactions'").fetchone():
        columns = {r[1] for r in con.execute("PRAGMA table_info(transactions)")}
        for name, decl in (("merchant", "TEXT"), ("occurred_at", "TEXT"), ("kind", "TEXT"),  # older ledgers
                           ("fee_cents", "INTEGER NOT NULL DEFAULT 0"), ("balance_after_cents", "INTEGER"),
                           ("source_format", "TEXT"), ("bank_category", "TEXT"), ("payee", "TEXT"),
                           ("category_by", "TEXT"), ("proposed_category", "TEXT")):
            if name not in columns:
                con.execute(f"ALTER TABLE transactions ADD COLUMN {name} {decl}")
        # v2 (2026-10-28): the single 'bank' account is Intesa, from its Lista Movimenti export
        con.execute("UPDATE transactions SET account = 'intesa', source_format = 'intesa_movimenti'"
                    " WHERE account = 'bank'")
    # v3 (2026-10-28): substring rules gave way to per-payee groups. Their categories were all seeded defaults (no
    # budgets used them), so bank rows start over; cash entries keep the category the user stated.
    if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'category_rules'").fetchone():
        con.execute("UPDATE transactions SET category = NULL WHERE account != 'cash' AND category_by IS NULL")
        con.execute("UPDATE transactions SET category_by = 'cash' WHERE account = 'cash'")
        con.execute("DROP TABLE category_rules")
    con.executescript(SCHEMA)
    rederive(con)
    categories.apply(con)
    con.commit()
    return con


def rederive(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Merchant and payee are derived from a row's stored text (merchants.of, categories.payee_of): recompute them, so
    a parser fix reaches movements imported before it. A payee a fix split off a wrongly merged one ('ADDEBITO
    DIRETTO' → 'WIND TRE', 'TELECOM ITALIA') inherits that one's decision as the model's guess, never as the user's
    word. Returns the (old, new) payees that changed."""
    rows = con.execute("SELECT id, description, details, source_format, kind, merchant, payee FROM transactions"
                       " WHERE account != 'cash'").fetchall()
    changed = []
    for i, desc, details, fmt, kind, merchant, payee in rows:
        m = merchants.of(desc, details, fmt, kind)
        p = categories.payee_of(m, desc)
        if (m, p) != (merchant, payee):
            changed.append((m, p, i, payee))
    if not changed:
        return []
    con.executemany("UPDATE transactions SET merchant = ?, payee = ? WHERE id = ?", [c[:3] for c in changed])
    moved = sorted({(old, new) for _, new, _, old in changed if old and new and old != new})
    decided = {r[0]: r for r in con.execute("SELECT payee, category, by, p, model FROM payees")}
    sources: dict[str, set[str]] = {}
    for old, new in moved:
        sources.setdefault(new, set()).add(old)
    now = datetime.now().isoformat(timespec="seconds")
    inherited = [(new, decided[old][1], decided[old][3], decided[old][4] or "inherited", now)
                 for new, olds in sources.items() if len(olds) == 1 and new not in decided
                 and (old := next(iter(olds))) in decided]
    con.executemany("INSERT OR IGNORE INTO payees VALUES (?, ?, 'model', ?, ?, ?)", inherited)
    if moved:
        log.info("ledger: payees re-derived: %s", "; ".join(f"{o} → {n}" for o, n in moved))
    return moved


def _ids(txs: list[Transaction]) -> list[str]:
    # Two identical coffees on the same day are both real: the occurrence index keeps them apart,
    # and stays stable when the same export (or an overlapping one, in the same order) is re-imported.
    # Intesa rows hash under their pre-v2 account name, so re-importing an old export adds nothing; Revolut's
    # timestamp is part of the key (Intesa has none).
    seen: Counter[tuple] = Counter()
    ids = []
    for t in txs:
        when = t.occurred_at.isoformat() if t.occurred_at else t.booked_on.isoformat()
        text = (t.description, t.details)
        if t.source_format == "intesa_operazioni":  # its exports punctuate the same text differently ('12:50' / '1250')
            text = tuple(re.sub(r"[^A-Z0-9]", "", x.upper()) for x in text)
        key = (LEGACY_ACCOUNT.get(t.account, t.account), when, t.amount_cents, *text)
        ids.append(hashlib.sha256(repr((key, seen[key])).encode()).hexdigest()[:16])
        seen[key] += 1
    return ids


def insert(con: sqlite3.Connection, txs: list[Transaction]) -> int:
    """Insert transactions, skipping ones already present. Returns how many were new."""
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO transactions (id, account, booked_on, value_on, amount_cents, description, details,"
        " category, source, merchant, occurred_at, kind, fee_cents, balance_after_cents, source_format, bank_category,"
        " payee, category_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (i, t.account, t.booked_on.isoformat(), t.value_on and t.value_on.isoformat(), t.amount_cents,
             t.description, t.details, t.category and t.category.strip().lower(), t.source, merchant,
             t.occurred_at and t.occurred_at.isoformat(), t.kind, t.fee_cents, t.balance_after_cents, t.source_format,
             t.bank_category, None if t.account == "cash" else categories.payee_of(merchant, t.description),
             "cash" if t.category else None)
            for i, t in zip(_ids(txs), txs)
            for merchant in [t.merchant or merchants.of(t.description, t.details, t.source_format, t.kind)]
        ],
    )
    new = con.total_changes - before
    categories.apply(con)  # a known payee's new payments get its group now, no model call
    con.commit()
    return new


def _read_only_authorizer(action: int, *_args) -> int:
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def run_select(path: Path, sql: str) -> dict:
    """Run one model-written SELECT on a read-only connection. Raises LedgerError if the db is missing."""
    if not path.exists():
        raise LedgerError(f"no ledger at {path}; import a bank export first")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.set_authorizer(_read_only_authorizer)
        cur = con.execute(sql)
        rows = cur.fetchmany(MAX_ROWS + 1)
        return {
            "columns": [d[0] for d in cur.description or []],
            "rows": [list(r) for r in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS,
        }
    finally:
        con.close()
