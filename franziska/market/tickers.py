"""Which Yahoo symbol prices each holding (DESIGN decision 25): a candidate counts only when its currency is the
holding's and its close on the snapshot's day, or the trading day before (DEGIRO's export carries the last close),
is within 1% of the snapshot's price. Candidates: Yahoo's matches for the ISIN and the name, plus each match's base
symbol on the exchanges the user traded on (Yahoo's ISIN search often finds only the London line)."""

import logging
import re
from datetime import date, timedelta
from pathlib import Path

from franziska.invest.service import Holding, Portfolio
from franziska.market import sources, store
from franziska.market.sources import Http, SourceError

log = logging.getLogger(__name__)

TOLERANCE = 0.01
MAX_TRIES = 10  # charts fetched per holding at most
# DEGIRO reference exchange → Yahoo exchange codes and symbol suffix.
EXCHANGES = {"EAM": (("AMS",), ".AS"), "XET": (("GER",), ".DE"), "TDG": (("GER",), ".DE"),
             "EPA": (("PAR",), ".PA"), "MIL": (("MIL",), ".MI"), "NDQ": (("NMS", "NGM", "NCM"), ""),
             "NSY": (("NYQ",), "")}


def candidates(http: Http, h: Holding) -> list[str]:
    found: list[tuple[str, str]] = []
    for query in dict.fromkeys((h.isin, h.name, _plain(h.name))):
        try:
            found += [(s, ex) for s, ex, kind in sources.yahoo_search(http, query) if kind in ("ETF", "EQUITY")]
        except SourceError as e:
            log.info("ticker search %r: %s", query, e)
    mine = [EXCHANGES[x] for x in h.exchanges if x in EXCHANGES]
    codes = {c for codes, _ in mine for c in codes}
    ranked = [s for s, ex in found if ex in codes] + [s.split(".")[0] + suffix for s, _ in found for _, suffix in mine]
    ranked += [s for s, _ in found]
    return list(dict.fromkeys(ranked))  # unique, in order


def _plain(name: str) -> str:
    """The fund's name without the share class and wrapper words: 'VANGUARD FTSE EMERGING MKTS UCITS ETF USD DIS'
    → 'Vanguard FTSE Emerging Markets'."""
    words = re.sub(r"[()\-]", " ", name.upper().split(" UCITS")[0]).replace("MKTS", "MARKETS").split()
    return " ".join(w for w in words if w not in {"ETF", "USD", "EUR", "DIS", "DIST", "ACC", "1C"}).title()


def matches(series: sources.Series, h: Holding) -> bool:
    if series.currency.upper() != h.currency.upper() or not h.price or not h.price_on:
        return False
    on = date.fromisoformat(h.price_on)
    closes = [v for d, v in series.rows if on - timedelta(days=5) <= d <= on]
    return any(abs(v - h.price) <= TOLERANCE * h.price for v in closes[-2:])


def map_tickers(portfolio: Portfolio, market_db: Path, http: Http | None = None, force: bool = False) -> list[str]:
    """Find a checked symbol for each holding that has none (all of them with `force`). Returns a line per holding."""
    http = http or Http()
    out = []
    for h in portfolio.holdings():
        if h.ticker and not force:
            continue
        chosen = None
        for symbol in candidates(http, h)[:MAX_TRIES]:
            try:
                series = sources.yahoo_chart(http, symbol, days=40)
            except SourceError:
                continue
            if matches(series, h):
                chosen = symbol
                store.save_prices(market_db, symbol, series.currency, series.rows)
                break
        if chosen:
            portfolio.set_ticker(h.isin, chosen)
            out.append(f"✅ {h.name}: {chosen}")
        else:
            out.append(f"❌ {h.name}: no symbol whose close matches the snapshot's {h.price} {h.currency}. Set one by"
                       f" hand: python -m franziska.market ticker {h.isin} <SYMBOL> (checked the same way)")
    return out


def set_ticker(portfolio: Portfolio, market_db: Path, isin: str, symbol: str, http: Http | None = None) -> str:
    """A symbol chosen by hand, checked like a found one."""
    h = next((h for h in portfolio.holdings() if h.isin == isin), None)
    if h is None:
        return f"No holding with ISIN {isin}."
    series = sources.yahoo_chart(http or Http(), symbol, days=40)
    if not matches(series, h):
        return (f"{symbol} doesn't match: {series.currency} closes {[round(v, 2) for _, v in series.rows[-3:]]}, "
                f"snapshot {h.price} {h.currency} on {h.price_on}.")
    store.save_prices(market_db, symbol, series.currency, series.rows)
    portfolio.set_ticker(isin, symbol)
    return f"✅ {h.name}: {symbol}"
