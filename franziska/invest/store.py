"""The investment tables, in the same `ledger.sqlite` as the bank (the money privacy class; docs/INVEST.md §2).

Imported rows (`inv_trades`, `inv_movements`, `inv_snapshots`) are as DEGIRO states them, keyed by content hash so
an overlapping export adds nothing. Everything else (`inv_instruments` apart from what the user sets, and the
`inv_positions` / `inv_realized` / `inv_income` tables) is rebuilt from them by service.Portfolio.rebuild after
every import: the model never computes a figure, and the same rows always give the same figures.
"""

import hashlib
import sqlite3
from collections import Counter

from franziska.invest.importer import Holding, Movement, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS inv_trades (
    id           TEXT PRIMARY KEY,    -- content hash (store._ids)
    order_id     TEXT NOT NULL,       -- '' for a corporate action (split, listing change)
    traded_at    TEXT NOT NULL,       -- ISO 'YYYY-MM-DDTHH:MM', local time as DEGIRO states it
    isin         TEXT NOT NULL,
    product      TEXT NOT NULL,
    exchange     TEXT NOT NULL,       -- DEGIRO's reference exchange: EAM, XET, NDQ, EPA, MIL, TDG
    venue        TEXT NOT NULL,       -- where it was filled: XAMS, XETA, ARCX...; '' for a corporate action
    quantity     REAL NOT NULL,       -- > 0 bought, < 0 sold
    price        REAL NOT NULL,       -- in `currency`
    currency     TEXT NOT NULL,
    local_cents  INTEGER NOT NULL,    -- in `currency`, < 0 for a buy
    eur_cents    INTEGER NOT NULL,
    fx           REAL NOT NULL,       -- `currency` per EUR
    autofx_cents INTEGER NOT NULL,    -- currency conversion fee, <= 0
    fee_cents    INTEGER NOT NULL,    -- broker fee, <= 0
    total_cents  INTEGER NOT NULL,    -- EUR effect on the cash: eur + autofx + fee
    kind         TEXT NOT NULL,       -- 'trade' | 'corporate'
    source       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inv_movements (
    id             TEXT PRIMARY KEY,
    at             TEXT NOT NULL,     -- ISO 'YYYY-MM-DDTHH:MM'
    value_on       TEXT,
    isin           TEXT NOT NULL,     -- '' when not about a product
    product        TEXT NOT NULL,
    description    TEXT NOT NULL,     -- the account's language (Italian)
    kind           TEXT NOT NULL,     -- importer.KINDS: deposit, withdrawal, trade, fee_trade, fee_connection, dividend,
                                      -- withholding, fx_debit, fx_credit, interest, courtesy, sweep, split,
                                      -- product_change, other
    fx             REAL,              -- on an FX conversion, the rate (USD per EUR)
    currency       TEXT NOT NULL,     -- of the change; '' when the row has none
    change_cents   INTEGER,
    balance_currency TEXT NOT NULL,
    balance_cents  INTEGER,           -- the cash after it, in balance_currency; sweep rows don't follow it
    order_id       TEXT NOT NULL,
    seq            INTEGER NOT NULL,  -- order within its file, oldest first: orders rows of the same minute
    source         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inv_snapshots (
    on_date     TEXT NOT NULL,
    isin        TEXT NOT NULL,        -- '' for a cash line
    product     TEXT NOT NULL,
    quantity    REAL NOT NULL,
    close       REAL,                 -- closing price in `currency`
    currency    TEXT NOT NULL,
    local_cents INTEGER NOT NULL,
    eur_cents   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (on_date, isin, currency)
);
CREATE TABLE IF NOT EXISTS inv_instruments (
    isin      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,          -- the full name, from the trades (the snapshot cuts it)
    kind      TEXT NOT NULL,          -- 'etf' | 'stock'
    currency  TEXT NOT NULL,          -- the price currency of its trades
    exchanges TEXT NOT NULL,          -- reference exchanges seen, comma separated
    ticker    TEXT,                   -- price source symbol (phase I1), set after it matches a snapshot close
    facts     TEXT                    -- JSON: TER, index, distributing, domicile, weights, each with source and date
);
-- Rebuilt by service.Portfolio.rebuild: never edit by hand.
CREATE TABLE IF NOT EXISTS inv_positions (
    isin           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    currency       TEXT NOT NULL,
    quantity       REAL NOT NULL,     -- 0 for a position sold out (kept for its realised gain and dividends)
    cost_cents     INTEGER NOT NULL,  -- EUR paid for what is held, fees included (weighted average cost)
    realized_cents INTEGER NOT NULL,  -- EUR gained on sales, after fees
    dividends_cents INTEGER NOT NULL, -- EUR, net of withholding
    first_on       TEXT NOT NULL,
    last_trade_on  TEXT NOT NULL,
    price          REAL,              -- latest known close in `currency`
    price_on       TEXT,
    price_source   TEXT,              -- 'snapshot <file>' now; the price feed from phase I1
    eur_per_unit   REAL,              -- EUR per unit of `currency` on price_on
    value_cents    INTEGER,           -- quantity × price in EUR; NULL without a price
    unrealized_cents INTEGER          -- value − cost
);
CREATE TABLE IF NOT EXISTS inv_realized (
    trade_id       TEXT PRIMARY KEY REFERENCES inv_trades(id),
    sold_on        TEXT NOT NULL,
    isin           TEXT NOT NULL,
    quantity       REAL NOT NULL,     -- > 0, units sold
    proceeds_cents INTEGER NOT NULL,  -- EUR received, after fees
    cost_cents     INTEGER NOT NULL,  -- average cost of the units sold
    gain_cents     INTEGER NOT NULL
);
-- Dividends, withholding, interest and courtesy credits in EUR (USD at the rate of the conversion that followed).
CREATE TABLE IF NOT EXISTS inv_income (
    movement_id TEXT PRIMARY KEY REFERENCES inv_movements(id),
    on_date     TEXT NOT NULL,
    isin        TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- dividend | withholding | interest | courtesy
    currency    TEXT NOT NULL,
    cents       INTEGER NOT NULL,     -- in `currency`
    eur_cents   INTEGER NOT NULL
);
DROP VIEW IF EXISTS inv_costs;
CREATE VIEW inv_costs AS
    SELECT substr(traded_at, 1, 4) AS year, 'broker fee' AS kind, -SUM(fee_cents) AS cents FROM inv_trades GROUP BY 1
    UNION ALL SELECT substr(traded_at, 1, 4), 'autofx', -SUM(autofx_cents) FROM inv_trades GROUP BY 1
    UNION ALL SELECT substr(at, 1, 4), 'connection fee', -SUM(change_cents) FROM inv_movements
        WHERE kind = 'fee_connection' GROUP BY 1;
"""


def migrate(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def _hash(key: tuple, seen: Counter) -> str:
    # Two identical rows in one export are both real: the occurrence index keeps them apart and stays stable when an
    # overlapping export lists them again in the same order.
    h = hashlib.sha256(repr((key, seen[key])).encode()).hexdigest()[:16]
    seen[key] += 1
    return h


def insert_trades(con: sqlite3.Connection, trades: list[Trade], source: str) -> int:
    seen: Counter = Counter()
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO inv_trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(_hash((t.order_id, t.at.isoformat(), t.isin, t.exchange, t.quantity, t.price), seen), t.order_id,
          t.at.isoformat(timespec="minutes"), t.isin, t.product, t.exchange, t.venue, t.quantity, t.price, t.currency,
          t.local_cents, t.eur_cents, t.fx, t.autofx_cents, t.fee_cents, t.total_cents,
          "trade" if t.order_id else "corporate", source) for t in trades])
    return con.total_changes - before


def insert_movements(con: sqlite3.Connection, movements: list[Movement], source: str) -> int:
    seen: Counter = Counter()
    before = con.total_changes
    con.executemany(
        "INSERT OR IGNORE INTO inv_movements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(_hash((m.at.isoformat(), m.value_on, m.isin, m.description, m.currency, m.change_cents, m.balance_cents),
                seen), m.at.isoformat(timespec="minutes"), m.value_on and m.value_on.isoformat(), m.isin, m.product,
          m.description, m.kind, m.fx, m.currency, m.change_cents, m.balance_currency, m.balance_cents, m.order_id,
          m.seq, source) for m in movements])
    return con.total_changes - before


def replace_snapshot(con: sqlite3.Connection, on: str, holdings: list[Holding], source: str) -> int:
    """A day's snapshot replaces an earlier import of the same day. Returns the lines stored."""
    con.execute("DELETE FROM inv_snapshots WHERE on_date = ?", (on,))
    con.executemany("INSERT INTO inv_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(on, h.isin, h.product, h.quantity, h.close, h.currency, h.local_cents, h.eur_cents, source)
                     for h in holdings])
    return len(holdings)
