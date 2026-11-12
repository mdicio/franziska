"""The portfolio as plain methods: one implementation behind the CLI, the bot's /portfolio and (phase I1) the analyst.

Like `Money`, everything here is deterministic. An import stores the rows as DEGIRO states them, then `rebuild`
recomputes the derived tables from all stored rows, and `checks` compares the files with each other:
- an order is in both the Transactions and the Account statement, with the same quantity, amount and fee;
- a snapshot's quantities equal the trades up to its day, and its cash equals the statement's balance that day.

Cost is the weighted average (the Italian method), in EUR, fees and AutoFX included: a buy adds its Total EUR, a
sale takes out the average cost of the units sold and realises proceeds − that cost. A corporate action changes the
quantity, never the cost: its rows are netted per day and ISIN (a 10:1 split is −1 and +10 on the same day, a
listing change −161 and +161). Values use the latest snapshot's closing prices until the price feed (phase I1).
"""

import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from franziska.finance import ledger
from franziska.finance.service import eur
from franziska.invest import importer, store
from franziska.invest.importer import ACCOUNT, MOVEMENTS, PORTFOLIO, TRANSACTIONS, ImportFormatError

STALE_DAYS = 35  # a statement older than this is called out: exports are monthly (INVEST.md §2)
INCOME_KINDS = ("dividend", "withholding", "interest", "courtesy")
NAME_CHARS = 34  # a position's name in a phone message
RECONCILE_CENTS = 500  # the gain's parts may differ from it by this much: USD amounts converted at rounded rates

__all__ = ["ImportFormatError", "Portfolio"]


def signed(cents: int) -> str:
    return ("+" if cents > 0 else "−" if cents < 0 else "") + eur(abs(cents))


@dataclass(frozen=True)
class Holding:
    isin: str
    name: str
    kind: str  # 'etf' | 'stock'
    currency: str
    quantity: float
    cost_cents: int
    price: float | None  # the latest snapshot's close, in `currency`
    price_on: str | None
    eur_per_unit: float | None
    value_cents: int | None
    ticker: str | None
    exchanges: tuple[str, ...]  # DEGIRO's reference exchanges it was traded on: EAM, XET, NDQ, EPA, MIL, TDG


@dataclass
class Portfolio:
    db: Path
    timezone: str = "Europe/Rome"
    clock: Callable[[], datetime] | None = None

    @classmethod
    def open(cls, db: Path, timezone: str = "Europe/Rome", clock: Callable[[], datetime] | None = None) -> "Portfolio":
        con = ledger.connect(db)
        try:
            store.migrate(con)
        finally:
            con.close()
        return cls(db, timezone, clock)

    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None)

    def today(self) -> date:
        return self.now().date()

    @contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        con = ledger.connect(self.db)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- import ---------------------------------------------------------------------------------
    def import_file(self, path: Path, on: date | None = None) -> str:
        """Import one DEGIRO export, refusing one whose own figures don't add up; then rebuild and check across files.
        `on`: a Portfolio export's day (it has none); else the date in its file name, else today."""
        dated = "given" if on else "from the file name" if importer.file_day(path.name) else "the day of import"
        ex = importer.read(path, on=on or importer.file_day(path.name) or self.today())
        with self._con() as con:
            if ex.format == TRANSACTIONS:
                rows, new, what = len(ex.trades), store.insert_trades(con, ex.trades, path.name), "fills"
            elif ex.format == MOVEMENTS:
                rows, new, what = len(ex.movements), store.insert_movements(con, ex.movements, path.name), "movements"
            else:
                rows = new = store.replace_snapshot(con, ex.on.isoformat(), ex.holdings, path.name)
                what = "lines"
            con.execute(
                "INSERT INTO imports (file, format, account, period_from, period_to, rows, new, checks, imported_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path.name, ex.format, ACCOUNT, ex.period[0].isoformat(), ex.period[1].isoformat(), rows, new,
                 "\n".join(ex.checks), self.now().isoformat(timespec="seconds")))
            problems = rebuild(con)
            cross = checks(con)
        out = [f"Imported {path.name}: {rows} {what}, {new} new." if ex.format != PORTFOLIO else
               f"Imported {path.name}: {rows} {what} (replacing any earlier snapshot of that day).", *ex.checks]
        if ex.format == PORTFOLIO:
            out.append(f"📅 Snapshot dated {ex.on:%d/%m/%Y} ({dated}). Another day? Send it again named "
                       "Portfolio_YYYY-MM-DD.csv.")
        out += [*cross, *problems]
        return "\n".join(out)

    # ---- reading --------------------------------------------------------------------------------
    def data_until(self) -> dict[str, date]:
        """Per export kind, the last day stored: trades, statement, snapshot."""
        with self._con() as con:
            rows = con.execute(
                "SELECT 'trades', max(substr(traded_at, 1, 10)) FROM inv_trades"
                " UNION ALL SELECT 'statement', max(substr(at, 1, 10)) FROM inv_movements"
                " UNION ALL SELECT 'snapshot', max(on_date) FROM inv_snapshots").fetchall()
        return {k: date.fromisoformat(d) for k, d in rows if d}

    def holdings(self) -> list[Holding]:
        """What is held now (quantity ≠ 0), largest first, with the snapshot's price and the price ticker if mapped."""
        with self._con() as con:
            rows = con.execute(
                "SELECT p.isin, p.name, p.kind, p.currency, p.quantity, p.cost_cents, p.price, p.price_on,"
                " p.eur_per_unit, p.value_cents, i.ticker, i.exchanges FROM inv_positions p"
                " JOIN inv_instruments i USING (isin) WHERE p.quantity != 0 ORDER BY p.value_cents DESC NULLS LAST"
            ).fetchall()
        return [Holding(*r[:8], r[8], r[9], r[10], tuple(filter(None, (r[11] or "").split(",")))) for r in rows]

    def cash_and_put_in(self) -> tuple[int, int | None]:
        """EUR cash at the end of the statement, and deposits − withdrawals (None when the statement doesn't reach
        back to the first trade, so the total put in is unknown)."""
        with self._con() as con:
            cash = cash_balances(con).get("EUR", 0)
            put_in = con.execute("SELECT COALESCE(SUM(change_cents), 0) FROM inv_movements"
                                 " WHERE kind IN ('deposit', 'withdrawal') AND currency = 'EUR'").fetchone()[0]
            return cash, put_in if full_history(con) else None

    def set_ticker(self, isin: str, ticker: str | None) -> None:
        """The price symbol of an instrument, once its close has matched a snapshot (market/tickers.py)."""
        with self._con() as con:
            con.execute("UPDATE inv_instruments SET ticker = ? WHERE isin = ?", (ticker, isin))

    def overview(self) -> str:
        """Value, cash, what was put in, gains, and each position with its weight: a phone message, no model."""
        with self._con() as con:
            positions = con.execute(
                "SELECT name, quantity, price, currency, price_on, value_cents, cost_cents, unrealized_cents"
                " FROM inv_positions WHERE quantity != 0 ORDER BY value_cents DESC NULLS LAST").fetchall()
            if not positions and not con.execute("SELECT 1 FROM inv_movements LIMIT 1").fetchone():
                return "No DEGIRO data yet: send the Transactions, Account statement and Portfolio exports (.csv)."
            cash = cash_balances(con)
            put_in = con.execute("SELECT COALESCE(SUM(change_cents), 0) FROM inv_movements"
                                 " WHERE kind IN ('deposit', 'withdrawal') AND currency = 'EUR'").fetchone()[0]
            realized, dividends = con.execute(
                "SELECT COALESCE(SUM(realized_cents), 0), COALESCE(SUM(dividends_cents), 0) FROM inv_positions"
            ).fetchone()
            other_income = con.execute("SELECT COALESCE(SUM(eur_cents), 0) FROM inv_income"
                                       " WHERE kind IN ('interest', 'courtesy')").fetchone()[0]
            connection = con.execute("SELECT COALESCE(SUM(cents), 0) FROM inv_costs WHERE kind = 'connection fee'"
                                     ).fetchone()[0]
            fees = con.execute("SELECT COALESCE(SUM(cents), 0) FROM inv_costs").fetchone()[0]
            full = full_history(con)
            since = "" if full else f" (since {date.fromisoformat(_span(con, 'inv_movements', 'at')[0]):%d/%m/%Y})"
        unpriced = [p[0] for p in positions if p[5] is None]
        held = sum(p[5] or 0 for p in positions)
        cash_eur = cash.get("EUR", 0)
        total = held + cash_eur
        priced_on = sorted({p[4] for p in positions if p[4]})
        when = f"prices of {date.fromisoformat(priced_on[-1]):%d/%m/%Y}" if priced_on else "no prices yet"
        out = [f"📈 DEGIRO · {eur(total)} ({when}, from the snapshot)",
               f"Positions {eur(held)} · cash {eur(cash_eur)}" + (f" ({cash_eur / total:.0%})" if total else "")]
        out += [f"Cash in {c}: {eur(v).replace('€', c)}" for c, v in sorted(cash.items()) if c != "EUR" and v]
        if put_in and not full:
            out.append("Put in: unknown, the account statement doesn't reach back to the first trade.")
        elif put_in:
            gain = total - put_in
            out.append(f"Put in {eur(put_in)} (deposits − withdrawals) · gain {signed(gain)} ({gain / put_in:+.1%})")
        unrealized = sum(p[7] or 0 for p in positions)
        out.append(f"Unrealised {signed(unrealized)} · realised {signed(realized)} · dividends {signed(dividends)}"
                   f" · interest and credits {signed(other_income)} · connection fees {signed(-connection)}{since}")
        out.append(f"All fees so far (broker, AutoFX, connection): {eur(fees)}{since}")
        out.append("")
        for name, qty, price, ccy, _on, value, cost, gain in positions:
            label = name if len(name) <= NAME_CHARS else name[:NAME_CHARS - 1] + "…"
            if value is None:
                out.append(f"• {label} · {qty:g} · no price yet · cost {eur(cost)}")
                continue
            close = f"{price:,.2f}".replace(",", "'")
            out.append(f"• {label} · {qty:g} × {close} {ccy} = {eur(value)} ({value / total:.1%}) · cost {eur(cost)}"
                       f" → {signed(gain)}")
        if unpriced:
            out.append(f"⚠️ No price for {', '.join(unpriced)}: bought after the last snapshot; send a new Portfolio "
                       "export.")
        until = self.data_until()
        out.append("")
        out.append("Data: " + " · ".join(f"{k} {'of' if k == 'snapshot' else 'to'} {d:%d/%m/%Y}" for k, d in until.items()))
        if (s := until.get("statement")) and (self.today() - s).days > STALE_DAYS:
            out.append(f"⚠️ The account statement ends {s:%d/%m/%Y}: export a newer one for current cash and dividends.")
        return "\n".join(out)


# ---- rebuild and checks (module functions: they run inside the import's transaction) -------------
def _instruments(con: sqlite3.Connection) -> None:
    names: dict[str, tuple[str, str, set]] = {}
    for isin, product, ccy, exchange in con.execute(
            "SELECT isin, product, currency, exchange FROM inv_trades ORDER BY traded_at"):
        name, _, exchanges = names.get(isin, ("", "", set()))
        names[isin] = (max(name, product, key=len), ccy, exchanges | {exchange})
    con.executemany(
        "INSERT INTO inv_instruments (isin, name, kind, currency, exchanges) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(isin) DO UPDATE SET name = excluded.name, kind = excluded.kind, currency = excluded.currency,"
        " exchanges = excluded.exchanges",
        [(isin, name, "etf" if re.search(r"\bETF\b|\bUCITS\b", name, re.IGNORECASE) else "stock", ccy,
          ",".join(sorted(x for x in exchanges if x)))
         for isin, (name, ccy, exchanges) in names.items()])


def _income(con: sqlite3.Connection) -> tuple[list[tuple], list[str]]:
    """Dividends, withholding, interest and credits in EUR. A USD amount is converted at the rate of the first USD →
    EUR conversion at or after it (DEGIRO converts the day after); with none yet, at the last one before."""
    rates = defaultdict(list)  # currency → [(at, seq, rate)]
    for at, seq, ccy, fx in con.execute("SELECT at, seq, currency, fx FROM inv_movements WHERE kind = 'fx_debit'"
                                        " AND fx IS NOT NULL ORDER BY at, seq"):
        rates[ccy].append((at, seq, fx))
    rows, problems = [], []
    for mid, at, seq, isin, kind, ccy, cents in con.execute(
            f"SELECT id, at, seq, isin, kind, currency, change_cents FROM inv_movements WHERE kind IN"
            f" ({','.join('?' * len(INCOME_KINDS))}) AND change_cents IS NOT NULL", INCOME_KINDS):
        if ccy == "EUR":
            rows.append((mid, at[:10], isin, kind, ccy, cents, cents))
            continue
        later = [r for r in rates[ccy] if (r[0], r[1]) >= (at, seq)]
        rate = (later[0] if later else rates[ccy][-1] if rates[ccy] else (None, None, None))[2]
        if rate is None:
            problems.append(f"⚠️ {kind} of {cents / 100:.2f} {ccy} on {at[:10]}: no conversion rate, left out of EUR "
                            "figures.")
            continue
        rows.append((mid, at[:10], isin, kind, ccy, cents, round(cents / rate)))
    return rows, problems


def rebuild(con: sqlite3.Connection) -> list[str]:
    """Recompute instruments, positions, realised gains and income from the stored rows. Returns problems found."""
    _instruments(con)
    problems: list[str] = []
    qty: dict[str, float] = defaultdict(float)
    cost: dict[str, float] = defaultdict(float)
    realized: dict[str, int] = defaultdict(int)
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    sales = []
    events = con.execute("SELECT id, traded_at, isin, quantity, total_cents, kind FROM inv_trades"
                         " ORDER BY traded_at, kind = 'trade', id").fetchall()  # a day's corporate rows first
    corporate: dict[tuple[str, str], float] = defaultdict(float)
    for _id, at, isin, q, _total, kind in events:
        if kind == "corporate":
            corporate[(at[:10], isin)] += q
    applied = set()
    for tid, at, isin, q, total, kind in events:
        first.setdefault(isin, at[:10])
        last[isin] = at[:10]
        if kind == "corporate":
            key = (at[:10], isin)
            if key not in applied:
                applied.add(key)
                qty[isin] += corporate[key]
            continue
        if q > 0:
            qty[isin] += q
            cost[isin] += -total
            continue
        if -q > qty[isin] + 1e-9:
            problems.append(f"❌ {isin}: sold {-q:g} on {at[:10]} but held {qty[isin]:g}: a trade is missing.")
            continue
        out_cost = round(cost[isin] * -q / qty[isin])
        cost[isin] -= out_cost
        qty[isin] += q
        realized[isin] += total - out_cost
        sales.append((tid, at[:10], isin, -q, total, out_cost, total - out_cost))
    income, more = _income(con)
    problems += more
    dividends: dict[str, int] = defaultdict(int)
    for _mid, _on, isin, kind, _ccy, _cents, eur_cents in income:
        if kind in ("dividend", "withholding") and isin:
            dividends[isin] += eur_cents
    names = dict(con.execute("SELECT isin, name FROM inv_instruments"))
    meta = {i: (k, c) for i, k, c in con.execute("SELECT isin, kind, currency FROM inv_instruments")}
    con.execute("DELETE FROM inv_positions")
    con.execute("DELETE FROM inv_realized")
    con.execute("DELETE FROM inv_income")
    for isin, first_on in first.items():
        q = round(qty[isin], 6)
        price = con.execute("SELECT on_date, close, local_cents, eur_cents, quantity, source FROM inv_snapshots"
                            " WHERE isin = ? ORDER BY on_date DESC LIMIT 1", (isin,)).fetchone()
        value = per_unit = None
        if q == 0:
            value = 0
        if price and price[1] is not None:
            _on, close, local, eur_cents, snap_qty, _src = price
            per_unit = eur_cents / local if local else None
            if q and per_unit is not None:  # the snapshot's own EUR value when nothing was traded since
                value = eur_cents if snap_qty == q else round(q * close * per_unit * 100)
        held_cost = round(cost[isin]) if q else 0
        kind, ccy = meta[isin]
        con.execute("INSERT INTO inv_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (isin, names[isin], kind, ccy, q, held_cost, realized[isin], dividends[isin], first_on,
                     last[isin], price and price[1], price and price[0], price and f"snapshot {price[5]}", per_unit,
                     value, None if value is None else value - held_cost))
    con.executemany("INSERT INTO inv_realized VALUES (?, ?, ?, ?, ?, ?, ?)", sales)
    con.executemany("INSERT INTO inv_income VALUES (?, ?, ?, ?, ?, ?, ?)", income)
    return problems


def cash_balances(con: sqlite3.Connection, until: str | None = None) -> dict[str, int]:
    """Per currency, the statement's balance after its last movement (up to the end of day `until`); sweep rows
    don't count (importer: the balance column doesn't follow them)."""
    end = f"{until}T23:59" if until else "9999"
    rows = con.execute(
        "SELECT balance_currency, balance_cents FROM inv_movements m WHERE kind != 'sweep' AND balance_cents IS NOT"
        " NULL AND at <= ? AND (at, seq) = (SELECT at, seq FROM inv_movements WHERE kind != 'sweep' AND"
        " balance_cents IS NOT NULL AND balance_currency = m.balance_currency AND at <= ? ORDER BY at DESC, seq DESC"
        " LIMIT 1)", (end, end)).fetchall()
    return dict(rows)


def _span(con: sqlite3.Connection, table: str, column: str) -> tuple[str, str] | None:
    lo, hi = con.execute(f"SELECT min(substr({column}, 1, 10)), max(substr({column}, 1, 10)) FROM {table}").fetchone()
    return (lo, hi) if lo else None


def full_history(con: sqlite3.Connection) -> bool:
    """The statement starts no later than the first trade, so its deposits are all of them: only then are 'put in'
    and the total gain known."""
    st, tr = _span(con, "inv_movements", "at"), _span(con, "inv_trades", "traded_at")
    return bool(st and (not tr or st[0] <= tr[0]))


def _orders(con: sqlite3.Connection) -> list[str]:
    """Each order on a day both files cover is in both, with the same quantity, amount and fee."""
    st, tr = _span(con, "inv_movements", "at"), _span(con, "inv_trades", "traded_at")
    if not st or not tr:
        return ["⏳ Orders not checked across files yet: send the " +
                ("Account statement" if tr else "Transactions export") + " too."] if st or tr else []
    trades: dict[str, list] = {}
    for oid, day, q, local, fee in con.execute(
            "SELECT order_id, substr(traded_at, 1, 10), quantity, local_cents, fee_cents FROM inv_trades"
            " WHERE order_id != ''"):
        t = trades.setdefault(oid, [day, 0.0, 0, 0])
        t[1] += q
        t[2] += local
        t[3] += fee
    moves: dict[str, list] = {}
    for oid, day, kind, desc, change in con.execute(
            "SELECT order_id, substr(at, 1, 10), kind, description, change_cents FROM inv_movements"
            " WHERE order_id != '' AND kind IN ('trade', 'fee_trade')"):
        m = moves.setdefault(oid, [day, 0.0, 0, 0])
        if kind == "fee_trade":
            m[3] += change or 0
        elif parsed := importer.trade_text(desc):
            m[1] += parsed[0]
            m[2] += change or 0
    both = trades.keys() & moves.keys()
    off = [o for o in both if abs(trades[o][1] - moves[o][1]) > 1e-9 or trades[o][2] != moves[o][2]
           or trades[o][3] != moves[o][3]]
    out = []
    if off:
        t, m = trades[off[0]], moves[off[0]]
        out.append(f"❌ {len(off)} orders differ between the two files, e.g. {off[0]} on {t[0]}: quantity {t[1]:g} vs "
                   f"{m[1]:g}, amount {t[2] / 100:.2f} vs {m[2] / 100:.2f}, fee {t[3] / 100:.2f} vs {m[3] / 100:.2f}.")
    elif both:
        fees = sum(trades[o][3] for o in both)
        out.append(f"✅ {len(both)} orders in both files: quantities, amounts and fees equal (fees {eur(-fees)}).")
    only_t = [trades[o][0] for o in trades.keys() - moves.keys()]
    only_m = [moves[o][0] for o in moves.keys() - trades.keys()]
    if n := sum(1 for d in only_m if d > tr[1]):
        out.append(f"⏳ {n} orders in the statement after the last trade stored ({tr[1]}): send a newer Transactions "
                   "export.")
    if n := sum(1 for d in only_m if d < tr[0]):
        out.append(f"⏳ {n} orders in the statement before the first trade stored ({tr[0]}): send the Transactions "
                   "export from the start.")
    if n := sum(1 for d in only_m if tr[0] <= d <= tr[1]):
        out.append(f"❌ {n} orders in the statement are missing from the Transactions export.")
    if n := sum(1 for d in only_t if d > st[1]):
        out.append(f"⏳ {n} trades after the statement's last movement ({st[1]}): send a newer Account statement.")
    if n := sum(1 for d in only_t if st[0] <= d <= st[1]):
        out.append(f"❌ {n} trades are missing from the Account statement.")
    if not out:
        lo, hi = max(st[0], tr[0]), min(st[1], tr[1])
        out.append(f"✅ No orders on the days both files cover ({lo} – {hi})." if lo <= hi else
                   f"⏳ Orders not checked: the files share no days (trades {tr[0]} – {tr[1]}, statement {st[0]} – "
                   f"{st[1]}).")
    return out


def _snapshot(con: sqlite3.Connection) -> list[str]:
    row = con.execute("SELECT max(on_date) FROM inv_snapshots").fetchone()
    if not row or not row[0]:
        return []
    on = row[0]
    lines = con.execute("SELECT isin, product, quantity, currency, local_cents FROM inv_snapshots WHERE on_date = ?",
                        (on,)).fetchall()
    out = []
    if con.execute("SELECT 1 FROM inv_trades LIMIT 1").fetchone():
        held: dict[str, float] = defaultdict(float)
        for isin, q in con.execute("SELECT isin, quantity FROM inv_trades WHERE traded_at <= ?", (f"{on}T23:59",)):
            held[isin] += q
        snap = {isin: q for isin, _p, q, _c, _l in lines if isin}
        names = dict(con.execute("SELECT isin, name FROM inv_instruments"))
        off = [(i, held.get(i, 0.0), snap.get(i, 0.0)) for i in sorted(set(snap) | {i for i, q in held.items()
                                                                                   if abs(q) > 1e-9})
               if abs(held.get(i, 0.0) - snap.get(i, 0.0)) > 1e-9]
        if off:
            out += [f"❌ Snapshot {on}: {names.get(i, i)} holds {s:g}, the trades give {h:g}." for i, h, s in off[:5]]
            out.append("   A trade is missing, or the snapshot's day is wrong.")
        else:
            out.append(f"✅ Snapshot {on}: the trades give the same quantity for all {len(snap)} positions.")
    else:
        out.append(f"⏳ Snapshot {on}: send the Transactions export to check its quantities.")
    if con.execute("SELECT 1 FROM inv_movements LIMIT 1").fetchone():
        cash = cash_balances(con, on)
        for isin, _p, _q, ccy, local in lines:
            if isin:
                continue
            stated = cash.get(ccy, 0)
            mark = "✅" if stated == local else "❌"
            out.append(f"{mark} Snapshot cash {local / 100:.2f} {ccy}, statement balance {stated / 100:.2f} {ccy}.")
    return out


def _reconcile(con: sqlite3.Connection) -> list[str]:
    """Value − what was put in must equal unrealised + realised + income − connection fees: every euro of gain is
    explained. What is left is the rounding of USD amounts converted at DEGIRO's rates."""
    if not all(con.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
               for t in ("inv_snapshots", "inv_trades", "inv_movements")):
        return []
    if not full_history(con):
        start = _span(con, "inv_movements", "at")[0]
        why = (f"⏳ Gain not reconciled: the statement starts {start}, after the first trade. Send the account "
               "statement from the start for deposits and the total gain.")
        return [why]
    held, unpriced = con.execute("SELECT COALESCE(SUM(value_cents), 0), SUM(value_cents IS NULL) FROM inv_positions"
                                 ).fetchone()
    if unpriced:
        return []
    cash = cash_balances(con).get("EUR", 0)
    put_in = con.execute("SELECT COALESCE(SUM(change_cents), 0) FROM inv_movements WHERE kind IN ('deposit',"
                         " 'withdrawal') AND currency = 'EUR'").fetchone()[0]
    parts = con.execute("SELECT COALESCE(SUM(unrealized_cents + realized_cents), 0) FROM inv_positions").fetchone()[0]
    parts += con.execute("SELECT COALESCE(SUM(eur_cents), 0) FROM inv_income").fetchone()[0]
    parts -= con.execute("SELECT COALESCE(SUM(cents), 0) FROM inv_costs WHERE kind = 'connection fee'").fetchone()[0]
    gap = held + cash - put_in - parts
    mark = "✅" if abs(gap) <= RECONCILE_CENTS else "❌"
    return [f"{mark} Gain {signed(held + cash - put_in)} = unrealised + realised + income − connection fees, within "
            f"{eur(abs(gap))}" + (" (currency rounding)." if mark == "✅" else ": a movement is missing or misread.")]


def checks(con: sqlite3.Connection) -> list[str]:
    return _orders(con) + _snapshot(con) + _reconcile(con)
