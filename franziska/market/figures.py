"""The market table: indices, rates, FX and commodities, with their change over a day, a week and a month. Code only.

Each line has sources in order (Yahoo first, then an official series where one exists); what was fetched is stored
in market.sqlite, so a source that fails today falls back to the stored closes, said as such. Only closes before the
report's day count: a report written at 07:30 is about the sessions that ended, never a price mid-session.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from franziska.market import sources, store
from franziska.market.sources import Http, SourceError

log = logging.getLogger(__name__)

HISTORY_DAYS = 45  # fetched per series: a month back plus room for holidays


@dataclass(frozen=True)
class Spec:
    label: str
    group: str  # 'Equities' | 'Rates' | 'FX & commodities'
    sources: tuple[str, ...]  # 'yahoo:^GSPC', 'fred:SP500', 'ecb:<key>'
    rate: bool = False  # a yield in %: changes in basis points, not percent
    unit: str = ""
    note: str = ""
    cadence: str = "daily"  # 'monthly' (one value a month: change vs the month before) | 'level' (no change shown)


MARKETS = (
    Spec("S&P 500", "Equities", ("yahoo:^GSPC", "fred:SP500")),
    Spec("Nasdaq 100", "Equities", ("yahoo:^NDX", "fred:NASDAQ100")),
    Spec("STOXX Europe 600", "Equities", ("yahoo:^STOXX",)),
    Spec("DAX", "Equities", ("yahoo:^GDAXI",)),
    Spec("FTSE MIB", "Equities", ("yahoo:FTSEMIB.MI",)),
    Spec("MSCI Emerging Markets (EEM)", "Equities", ("yahoo:EEM",), unit="USD"),
    Spec("VIX (US volatility)", "Equities", ("yahoo:^VIX", "fred:VIXCLS")),
    Spec("US 10-year yield", "Rates", ("yahoo:^TNX", "fred:DGS10"), rate=True, unit="%"),
    Spec("Euro area AAA 10-year yield", "Rates", ("ecb:YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y",), rate=True, unit="%",
         note="ECB curve, close to the Bund"),
    Spec("Italy 10-year yield (monthly average)", "Rates", ("ecb:IRS/M.IT.L.L40.CI.0000.EUR.N.Z",), rate=True,
         unit="%", note="ECB, monthly", cadence="monthly"),
    Spec("ECB deposit rate", "Rates", ("ecb:FM/D.U2.EUR.4F.KR.DFR.LEV",), rate=True, unit="%", cadence="level"),
    Spec("EUR/USD", "FX & commodities", ("ecb:EXR/D.USD.EUR.SP00.A", "yahoo:EURUSD=X"), note="ECB reference rate"),
    Spec("Brent crude", "FX & commodities", ("yahoo:BZ=F", "fred:DCOILBRENTEU"), unit="USD"),
    Spec("Gold", "FX & commodities", ("yahoo:GC=F",), unit="USD"),
)
USD_PER_EUR = "ecb:EXR/D.USD.EUR.SP00.A"


@dataclass(frozen=True)
class Line:
    spec: Spec
    rows: list[tuple[date, float]]  # closes before the report's day, oldest first
    source: str  # the source that gave them, or 'stored (<source> failed)'

    @property
    def last(self) -> tuple[date, float] | None:
        return self.rows[-1] if self.rows else None

    def back(self, days: int) -> tuple[date, float] | None:
        """The close `days` calendar days before the last one (or the closest before it); 1 = the previous close."""
        if len(self.rows) < 2:
            return None
        if days == 1:
            return self.rows[-2]
        target = self.rows[-1][0] - timedelta(days=days)
        before = [r for r in self.rows if r[0] <= target]
        return before[-1] if before else None

    def change(self, days: int) -> float | None:
        """Percent change, or for a rate the change in basis points. A monthly series has only 'vs the month
        before' (asked as 30 days); a level has none."""
        if self.spec.cadence == "level" or (self.spec.cadence == "monthly" and days != 30):
            return None
        last, then = self.last, (self.back(1) if self.spec.cadence == "monthly" else self.back(days))
        if not last or not then or not then[1]:
            return None
        return (last[1] - then[1]) * 100 if self.spec.rate else (last[1] / then[1] - 1) * 100


def symbol_of(source: str) -> str:
    """The key the closes are stored under: 'yahoo:^GSPC' → '^GSPC', 'fred:SP500' → 'FRED:SP500'."""
    kind, _, key = source.partition(":")
    return key if kind == "yahoo" else f"{kind.upper()}:{key}"


def fetch(http: Http, db: Path, source: str, today: date) -> list[tuple[date, float]]:
    """Fresh closes from one source, stored; SourceError when it fails."""
    kind, _, key = source.partition(":")
    start = today - timedelta(days=HISTORY_DAYS)
    if kind == "yahoo":
        s = sources.yahoo_chart(http, key, days=HISTORY_DAYS)
        rows, currency = s.rows, s.currency
    elif kind == "fred":
        rows, currency = sources.fred(http, key, start), ""
    else:
        rows, currency = sources.ecb(http, key, start - timedelta(days=62 if "/M." in key else 0)), ""
    if kind == "yahoo" and key == "^TNX" and rows and rows[-1][1] > 20:  # older feeds quote ×10
        rows = [(d, v / 10) for d, v in rows]
    store.save_prices(db, symbol_of(source), currency, rows)
    return rows


def line(http: Http, db: Path, spec: Spec, day: date) -> Line:
    """The first source that answers; else the stored closes of the first that has any. `day` is the first day
    whose bar doesn't count (the report's morning; the day after, once every market has closed)."""
    failed = []
    for src in spec.sources:
        try:
            rows = [r for r in fetch(http, db, src, day) if r[0] < day]
            if rows:
                return Line(spec, rows, src.split(":")[0])
        except SourceError as e:
            failed.append(str(e))
    for src in spec.sources:
        if rows := [r for r in store.prices(db, symbol_of(src), day - timedelta(days=HISTORY_DAYS)) if r[0] < day]:
            return Line(spec, rows, f"stored ({'; '.join(failed)})")
    log.warning("no closes for %s: %s", spec.label, "; ".join(failed))
    return Line(spec, [], "unavailable: " + "; ".join(failed))


def market_table(http: Http, db: Path, day: date) -> list[Line]:
    return [line(http, db, spec, day) for spec in MARKETS]


def as_text(lines: list[Line]) -> str:
    """The table as the model reads it: every figure it may quote, exactly as the PDF prints it."""
    out = []
    for ln in lines:
        if not ln.last:
            continue
        d = ln.last[0]
        parts = [f"{ln.spec.label}: {fmt_value(ln)} " + (f"in {d:%B %Y}" if ln.spec.cadence == "monthly" else
                                                          f"on {d:%a %d %b}")]
        for days, name in ((1, "1 day"), (7, "1 week"), (30, "1 month")):
            if (c := ln.change(days)) is not None:
                parts.append(f"{name} {fmt_change(c, ln.spec.rate)}")
        out.append(", ".join(parts))
    return "\n".join(out)


def fmt_value(ln: Line) -> str:
    v = ln.last[1]
    if ln.spec.rate:
        return f"{v:.2f}%"
    if ln.spec.label == "EUR/USD":
        return f"{v:.4f}"
    return f"{v:,.2f}" + (f" {ln.spec.unit}" if ln.spec.unit else "")


def fmt_change(c: float, rate: bool) -> str:
    if rate:
        return f"{c:+.0f} bp"
    return f"{c:+.2f}%"
