"""The portfolio overlay (INVEST.md §4): value, its change, each holding's contribution, cash share, concentration.
Code only, from the ledger's positions and the closes; nothing here reaches the model or market.sqlite's reports.

Values use today's quantities over the whole window (positions change only with a trade; a trade after the last
Transactions export shows up as a snapshot mismatch at import). USD closes are converted at the ECB reference rate
of the same day. All holdings are aligned on one calendar: a holding without a close that day (its exchange was
closed) keeps its previous close, so the contributions add up to the total change.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from franziska.invest.service import Holding, Portfolio
from franziska.market import figures, sources, store
from franziska.market.sources import Http, SourceError

WINDOW_DAYS = 45


@dataclass(frozen=True)
class Move:
    name: str
    ticker: str | None
    kind: str
    value_cents: int
    weight: float  # of the total, cash included
    day_cents: int  # change over the last session
    day_pct: float | None
    period_cents: int  # over the edition's period (a day or a week)
    period_pct: float | None
    month_pct: float | None
    priced: str  # 'close of <day>' or 'snapshot of <day>'


@dataclass
class Overlay:
    day: date | None  # the last session in the figures
    previous: date | None
    period_start: date | None
    total_cents: int
    cash_cents: int
    put_in_cents: int | None
    moves: list[Move]
    series: list[tuple[date, int]]  # total value per session, oldest first
    notes: list[str] = field(default_factory=list)  # data gaps, freshness

    @property
    def held_cents(self) -> int:
        return sum(m.value_cents for m in self.moves)

    def change(self, start: date | None) -> tuple[int, float | None]:
        if not self.series or start is None:
            return 0, None
        then = next((v for d, v in self.series if d == start), None)
        if then is None:
            return 0, None
        now = self.series[-1][1]
        return now - then, ((now / then - 1) * 100 if then else None)

    @property
    def day_change(self) -> tuple[int, float | None]:
        return self.change(self.previous)

    @property
    def period_change(self) -> tuple[int, float | None]:
        return self.change(self.period_start)

    def facts(self) -> list[str]:
        """Plain facts about the shape of the portfolio: no limits are set yet (/invest_setup, INVEST.md §3)."""
        out = []
        if self.total_cents:
            out.append(f"Cash is {self.cash_cents / self.total_cents:.0%} of the account.")
        held = self.held_cents
        if held:
            stocks = sum(m.value_cents for m in self.moves if m.kind == "stock")
            if stocks:
                out.append(f"Single stocks are {stocks / held:.0%} of the invested money.")
            top = max(self.moves, key=lambda m: m.value_cents)
            out.append(f"Largest position: {short(top.name)} at {top.value_cents / held:.0%} of the invested money.")
        return out


def short(name: str) -> str:
    from franziska.market.brief import short as shorten  # brief imports this module

    return shorten(name)


def _closes(http: Http, db: Path, symbol: str, day: date, notes: list[str]) -> list[tuple[date, float]]:
    try:
        series = sources.yahoo_chart(http, symbol, days=WINDOW_DAYS)
        rows = series.rows
        store.save_prices(db, symbol, series.currency, rows)
    except SourceError as e:
        rows = store.prices(db, symbol, day - timedelta(days=WINDOW_DAYS))
        notes.append(f"{symbol}: {e}; " + ("using stored closes" if rows else "no closes"))
    return [r for r in rows if r[0] < day]


def build(portfolio: Portfolio, db: Path, http: Http, day: date, period_days: int,
          usd_per_eur: list[tuple[date, float]] | None = None) -> Overlay:
    """The overlay for a report on `day` (closes before it) over `period_days` (1 daily, 7 weekly)."""
    holdings = portfolio.holdings()
    cash, put_in = portfolio.cash_and_put_in()
    notes: list[str] = []
    if usd_per_eur is None:
        try:
            usd_per_eur = figures.fetch(http, db, figures.USD_PER_EUR, day)
        except SourceError as e:
            usd_per_eur = store.prices(db, figures.symbol_of(figures.USD_PER_EUR), day - timedelta(days=WINDOW_DAYS))
            notes.append(f"EUR/USD: {e}")
    fx = sorted(r for r in usd_per_eur if r[0] < day)
    closes: dict[str, list[tuple[date, float]]] = {}
    for h in holdings:
        if h.ticker:
            closes[h.isin] = _closes(http, db, h.ticker, day, notes)
    sessions = sorted({d for rows in closes.values() for d, _ in rows})
    # Start where every priced holding has a close, so no value jumps when a series begins.
    starts = [rows[0][0] for rows in closes.values() if rows]
    sessions = [d for d in sessions if not starts or d >= max(starts)]

    def at(rows: list[tuple[date, float]], d: date) -> float | None:
        before = [v for x, v in rows if x <= d]
        return before[-1] if before else None

    def eur_cents(h: Holding, d: date | None) -> int:
        """The holding's value on session `d` (None: at the snapshot's price)."""
        price = at(closes.get(h.isin, []), d) if d else None
        if price is None:
            return h.value_cents or 0
        rate = 1.0 if h.currency == "EUR" else (1 / at(fx, d) if h.currency == "USD" and at(fx, d) else None)
        if rate is None:
            return h.value_cents or 0
        return round(h.quantity * price * rate * 100)

    series = [(d, cash + sum(eur_cents(h, d) for h in holdings)) for d in sessions]
    last = sessions[-1] if sessions else None
    previous = sessions[-2] if len(sessions) > 1 else None
    period_start = previous if period_days == 1 else next(
        (d for d in reversed(sessions) if last and d <= last - timedelta(days=period_days)), None)
    month_start = next((d for d in reversed(sessions) if last and d <= last - timedelta(days=30)), None)
    total = series[-1][1] if series else cash + sum(h.value_cents or 0 for h in holdings)
    moves = []
    for h in holdings:
        now = eur_cents(h, last)

        def delta(start: date | None, h=h, now=now) -> tuple[int, float | None]:
            if start is None or h.isin not in closes:
                return 0, None
            then = eur_cents(h, start)
            return now - then, ((now / then - 1) * 100 if then else None)

        priced = (f"close of {at_day:%d %b}" if (at_day := _last_day(closes.get(h.isin, []), last)) else
                  f"snapshot of {h.price_on}")
        if h.isin not in closes:
            notes.append(f"{h.name}: no price symbol, valued at the snapshot of {h.price_on}")
        moves.append(Move(h.name, h.ticker, h.kind, now, now / total if total else 0, *delta(previous),
                          *delta(period_start), delta(month_start)[1], priced))
    moves.sort(key=lambda m: m.value_cents, reverse=True)
    until = portfolio.data_until()
    if (s := until.get("statement")) and (day - s).days > 35:
        notes.append(f"Cash is the account statement's of {s:%d %b %Y}: export a newer one for current cash.")
    return Overlay(last, previous, period_start, total, cash, put_in, moves, series, notes)


def _last_day(rows: list[tuple[date, float]], last: date | None) -> date | None:
    days = [d for d, _ in rows if last is None or d <= last]
    return days[-1] if days else None
