"""DEGIRO export parsers: Transactions, Account statement and Portfolio (.csv, as of the October 2026 exports).

Each parser returns an Export: the rows and the checks that prove the file is whole. A file whose own figures don't
add up is refused (ImportFormatError), never imported in part. Checks across files (an order in both, a snapshot's
quantities against the trades) need both in the ledger and run in service.py after every import.

All three: comma separated, decimals with a comma and quoted ("85,7000"); a product name too long for its cell
wraps onto a row of its own with only that text (`,,ACC ETF,,,…`), joined back to the row above. Dates dd-mm-yyyy.

Transactions (`full_transaction_history_degiro.csv`): Date, Time, Product, ISIN, Reference exchange, Venue,
Quantity (signed), Price, <currency>, Local value, <currency>, Value EUR, Exchange rate, AutoFX Fee, Transaction
and/or third party fees EUR, Total EUR, Order ID. One row per fill: an order filled in parts has one row per part
under the same Order ID, its fee on one of them. Rows without an Order ID at 00:00 are corporate actions: a split
(NVIDIA 10:1 on 10 Jun 2024: −1 at 1208.88, +10 at 120.888) or a listing change (−161 on MIL, +161 on EPA). Check:
Total EUR = Value EUR + AutoFX + fees, to the cent (DEGIRO rounds each one, so 1 cent off is allowed).

Account statement (`account_movements_full.csv`): Date, Time, Value date, Product, ISIN, Description, FX, Change
(<currency>, amount), Balance (<currency>, amount), Order Id; newest first. Descriptions are in the account's
language (Italian here). Check: in file order from the oldest, each row's balance = the previous balance of that
currency + its change, **leaving out the cash sweep rows** ('Degiro Cash Sweep Transfer' and the amount-less
'Trasferisci sul/dal tuo conto deposito presso flatexDEGIRO Bank'): cash moved to the partner bank and back, which
the balance column doesn't follow. On the real file: 0 breaks without them, 75 with.

Portfolio (`Portfolio.csv`): Product, Symbol/ISIN, Amount, Closing, Local value (<currency>, amount), Value in EUR.
Names cut at ~30 characters ("…"); a cash line 'CASH & CASH FUND & FTX CASH (EUR)' with no ISIN. **No date in
the file**: the caller gives the day (the file name's date, or the day it was imported).
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

TRANSACTIONS = "degiro_transactions"
MOVEMENTS = "degiro_movements"
PORTFOLIO = "degiro_portfolio"
ACCOUNT = "degiro"

TRANSACTIONS_HEADER = ["Date", "Time", "Product", "ISIN", "Reference exchange", "Venue", "Quantity", "Price", "",
                       "Local value", "", "Value EUR", "Exchange rate", "AutoFX Fee",
                       "Transaction and/or third party fees EUR", "Total EUR", "Order ID"]
MOVEMENTS_HEADER = ["Date", "Time", "Value date", "Product", "ISIN", "Description", "FX", "Change", "", "Balance", "",
                    "Order Id"]
PORTFOLIO_HEADER = ["Product", "Symbol/ISIN", "Amount", "Closing", "Local value", "", "Value in EUR"]

# Statement descriptions → kind, first match wins. Italian as seen; English as DEGIRO writes it in an English account.
KINDS = [
    (r"^Degiro Cash Sweep Transfer|conto deposito presso flatexDEGIRO Bank|flatexDEGIRO Bank Cash Sweep", "sweep"),
    (r"^CAMBIO DI PRODOTTO|^PRODUCT CHANGE", "product_change"),
    (r"^RETTIFICA PER FRAZIONAMENTO|^STOCK SPLIT", "split"),
    (r"^(Acquisto|Vendita|Buy|Sell) ", "trade"),
    (r"costi di transazione|Transaction and/or third", "fee_trade"),
    (r"Costi di connessione|Connection Fee", "fee_connection"),
    (r"^Ritenuta sul dividendo|^Dividend Tax", "withholding"),
    (r"^Dividendo|^Dividend", "dividend"),
    (r"^Prelievo FX|^FX Withdrawal|^FX Debit", "fx_debit"),
    (r"^Credito FX|^FX Credit", "fx_credit"),
    (r"^Deposit", "deposit"),  # 'Deposito', 'Deposito flatex', 'Deposit', 'flatex Deposit' below
    (r"^flatex Deposit", "deposit"),
    (r"^(Prelievo|Ritiro|Withdrawal|flatex Withdrawal)", "withdrawal"),
    (r"Interest|^Interesse", "interest"),
    (r"courtesy", "courtesy"),
]
_TRADE_TEXT = re.compile(r"^(?:CAMBIO DI PRODOTTO: )?(?P<side>Acquisto|Vendita|Buy|Sell) (?P<qty>[\d.,]+) .*@(?P<price>[\d.,]+) "
                         r"(?P<ccy>[A-Z]{3})")
_FILE_DAY = [(re.compile(r"(\d{4})[-_.](\d{2})[-_.](\d{2})"), "ymd"), (re.compile(r"(\d{2})[-_.](\d{2})[-_.](\d{4})"), "dmy")]


class ImportFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Trade:
    at: datetime
    isin: str
    product: str
    exchange: str  # DEGIRO's reference exchange (EAM, XET, NDQ, EPA, MIL, TDG…)
    venue: str  # where it was filled (XAMS, XETA, ARCX…); '' for a corporate action
    quantity: float  # > 0 bought, < 0 sold
    price: float
    currency: str
    local_cents: int  # in `currency`, < 0 for a buy
    eur_cents: int
    fx: float  # local currency per EUR (1 for EUR)
    autofx_cents: int  # ≤ 0
    fee_cents: int  # ≤ 0
    total_cents: int  # EUR, what it did to the cash: Value EUR + AutoFX + fees
    order_id: str  # '' for a corporate action


@dataclass(frozen=True)
class Movement:
    at: datetime
    value_on: date | None
    isin: str
    product: str
    description: str
    kind: str  # KINDS, or 'other'
    fx: float | None  # on an FX conversion: the rate (USD per EUR)
    currency: str  # of the change; '' on an amount-less row
    change_cents: int | None
    balance_currency: str
    balance_cents: int | None
    order_id: str
    seq: int  # position in the file from the oldest row: orders rows that share a minute


@dataclass(frozen=True)
class Holding:
    isin: str  # '' for the cash line
    product: str
    quantity: float
    close: float | None
    currency: str
    local_cents: int
    eur_cents: int


@dataclass
class Export:
    format: str
    trades: list[Trade] = field(default_factory=list)
    movements: list[Movement] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    on: date | None = None  # a portfolio's day
    period: tuple[date, date] | None = None
    checks: list[str] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)


def _num(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    if "," in s:  # Italian: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def _cents(s: str) -> int | None:
    n = _num(s)
    return None if n is None else round(n * 100)


def _when(day: str, at: str = "") -> datetime:
    return datetime.strptime(f"{day.strip()} {at.strip() or '00:00'}", "%d-%m-%Y %H:%M")


def _rows(path: Path) -> list[list[str]]:
    """The data rows, with wrapped cells joined to the row above."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))[1:]
    out: list[list[str]] = []
    for r in rows:
        if not any(c.strip() for c in r):
            continue
        if out and _continuation(r):
            prev = out[-1]
            for i, c in enumerate(r):
                if c.strip() and i < len(prev):
                    prev[i] = f"{prev[i].rstrip()} {c.strip()}"
            continue
        out.append(list(r))
    return out


def _continuation(r: list[str]) -> bool:
    """A row holding only the wrapped end of text cells: no date, no amounts."""
    return not r[0].strip() and not any(re.fullmatch(r"-?[\d.,]+", c.strip()) for c in r if c.strip())


def detect(path: Path) -> str | None:
    """TRANSACTIONS, MOVEMENTS, PORTFOLIO, or None when the file isn't a DEGIRO export."""
    if path.suffix.lower() != ".csv":
        return None
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:  # a bank's cp1252 .csv isn't DEGIRO's
        first = next(csv.reader(f), [])
    for fmt, header in ((TRANSACTIONS, TRANSACTIONS_HEADER), (MOVEMENTS, MOVEMENTS_HEADER),
                        (PORTFOLIO, PORTFOLIO_HEADER)):
        if first[:len(header)] == header:
            return fmt
    return None


def file_day(name: str) -> date | None:
    """A date in a file name ('Portfolio_2026-10-28.csv', 'portfolio 28-10-2026.csv'), or None."""
    for rx, order in _FILE_DAY:
        if m := rx.search(name):
            y, mo, d = m.groups() if order == "ymd" else m.groups()[::-1]
            try:
                return date(int(y), int(mo), int(d))
            except ValueError:
                return None
    return None


def read(path: Path, on: date | None = None) -> Export:
    """Parse one export. `on`: a portfolio's day (else the date in its file name)."""
    fmt = detect(path)
    if fmt == TRANSACTIONS:
        return read_transactions(path)
    if fmt == MOVEMENTS:
        return read_movements(path)
    if fmt == PORTFOLIO:
        day = on or file_day(path.name)
        if day is None:
            raise ImportFormatError(f"{path.name}: a Portfolio export has no date; say which day it is.")
        return read_portfolio(path, day)
    raise ImportFormatError(f"{path.name}: not a DEGIRO export I know (Transactions, Account statement, Portfolio .csv).")


def read_transactions(path: Path) -> Export:
    ex = Export(TRANSACTIONS)
    off = []
    for r in _rows(path):
        (day, at, product, isin, exchange, venue, qty, price, ccy, local, _lccy, value_eur, fx, autofx, fees, total,
         order) = (r + [""] * 17)[:17]
        t = Trade(at=_when(day, at), isin=isin.strip(), product=product.strip(), exchange=exchange.strip(),
                  venue=venue.strip(), quantity=_num(qty), price=_num(price), currency=ccy.strip(),
                  local_cents=_cents(local) or 0, eur_cents=_cents(value_eur) or 0, fx=_num(fx) or 1.0,
                  autofx_cents=_cents(autofx) or 0, fee_cents=_cents(fees) or 0, total_cents=_cents(total) or 0,
                  order_id=order.strip())
        if abs(t.eur_cents + t.autofx_cents + t.fee_cents - t.total_cents) > 1:
            off.append(f"{t.at:%d/%m/%Y} {t.product}")
        ex.trades.append(t)
    if not ex.trades:
        raise ImportFormatError(f"{path.name}: no trades")
    if off:
        raise ImportFormatError(f"{path.name}: Total EUR ≠ value + AutoFX + fees on {len(off)} rows ({off[0]}…); "
                                "export it again.")
    days = [t.at.date() for t in ex.trades]
    ex.period = (min(days), max(days))
    actions = sum(1 for t in ex.trades if not t.order_id)
    ex.checks.append(f"degiro: {len(ex.trades)} fills ({actions} from corporate actions), each total = value + "
                     f"AutoFX + fees ✅ ({ex.period[0]:%d/%m/%Y} – {ex.period[1]:%d/%m/%Y})")
    return ex


def kind_of(description: str) -> str:
    return next((k for rx, k in KINDS if re.search(rx, description, re.IGNORECASE)), "other")


def read_movements(path: Path) -> Export:
    ex = Export(MOVEMENTS)
    rows = _rows(path)[::-1]  # oldest first; rows sharing a minute keep the file's order
    last: dict[str, int] = {}
    breaks: dict[str, int] = {}
    for seq, r in enumerate(rows):
        day, at, value_on, product, isin, desc, fx, ccy, change, bccy, balance, order = (r + [""] * 12)[:12]
        m = Movement(at=_when(day, at), value_on=_when(value_on).date() if value_on.strip() else None,
                     isin=isin.strip(), product=product.strip(), description=desc.strip(), kind=kind_of(desc),
                     fx=_num(fx), currency=ccy.strip() if change.strip() else "", change_cents=_cents(change),
                     balance_currency=bccy.strip(), balance_cents=_cents(balance), order_id=order.strip(), seq=seq)
        ex.movements.append(m)
        if m.kind == "sweep" or m.change_cents is None:
            continue
        expected = last.get(m.currency, 0) + m.change_cents
        if m.balance_currency == m.currency and m.balance_cents is not None:
            if m.currency in last and expected != m.balance_cents:
                breaks[m.currency] = breaks.get(m.currency, 0) + 1
            last[m.currency] = m.balance_cents
        else:
            last[m.currency] = expected
    if not ex.movements:
        raise ImportFormatError(f"{path.name}: no movements")
    if breaks:
        raise ImportFormatError(
            f"{path.name}: the running balance doesn't add up ({', '.join(f'{c}: {n} breaks' for c, n in breaks.items())}):"
            " rows are missing; export the account statement again.")
    days = [m.at.date() for m in ex.movements]
    ex.period = (min(days), max(days))
    sweeps = sum(1 for m in ex.movements if m.kind == "sweep")
    ex.checks.append(f"degiro: running balance consistent over {len(ex.movements) - sweeps} movements "
                     f"({', '.join(f'{c} {v / 100:.2f}' for c, v in sorted(last.items()))}; {sweeps} cash-sweep rows "
                     f"left out) ✅ ({ex.period[0]:%d/%m/%Y} – {ex.period[1]:%d/%m/%Y})")
    others = sorted({m.description for m in ex.movements if m.kind == "other"})
    if others:
        ex.checks.append(f"degiro: ⚠️ {len(others)} descriptions of a kind I don't know, kept as 'other': "
                         f"{'; '.join(others[:3])}")
    return ex


def trade_text(description: str) -> tuple[float, float, str] | None:
    """(signed quantity, price, currency) out of 'Acquisto 10 Apple Inc@180 USD (US0378331005)'."""
    if not (m := _TRADE_TEXT.match(description)):
        return None
    qty = _num(m["qty"])
    return (-qty if m["side"] in ("Vendita", "Sell") else qty), _num(m["price"]), m["ccy"]


def read_portfolio(path: Path, on: date) -> Export:
    ex = Export(PORTFOLIO, on=on, period=(on, on))
    for r in _rows(path):
        product, isin, qty, close, ccy, local, eur = (r + [""] * 7)[:7]
        if not isin.strip() and (m := re.search(r"^CASH.*\(([A-Z]{3})\)", product, re.IGNORECASE)):
            ccy = m[1]  # the cash line: 'CASH & CASH FUND & FTX CASH (EUR)', its value in the 'Local value' column
        ex.holdings.append(Holding(isin=isin.strip(), product=product.strip(), quantity=_num(qty) or 0.0,
                                   close=_num(close), currency=ccy.strip(), local_cents=_cents(local) or 0,
                                   eur_cents=_cents(eur) or 0))
    if not ex.holdings:
        raise ImportFormatError(f"{path.name}: no positions")
    total = sum(h.eur_cents for h in ex.holdings)
    value = f"{total / 100:,.2f}".replace(",", "'")
    ex.checks.append(f"degiro: snapshot of {on:%d/%m/%Y}, {sum(1 for h in ex.holdings if h.isin)} positions + cash, "
                     f"{value} €")
    return ex
