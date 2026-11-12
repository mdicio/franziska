"""The news a report reads: fixed feeds (markets, macro, central banks) plus searches written by code from what is
held (DESIGN decision 26: no model writes a query, and a query never holds a number). Items are stored in
market.sqlite as they are found, so the weekly edition also reads what the daily runs found during the week.

Titles of the same story from several outlets are merged (word overlap), keeping the copy with a readable link and a
summary.
"""

import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from franziska.invest.service import Holding
from franziska.market import sources, store
from franziska.market.sources import Http, SourceError

log = logging.getLogger(__name__)

FEEDS = (  # (name, url, publisher)
    ("CNBC Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069", "CNBC"),
    ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258", "CNBC"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "MarketWatch"),
    ("FT Markets", "https://www.ft.com/markets?format=rss", "Financial Times"),
    ("Il Sole 24 Ore Finanza", "https://www.ilsole24ore.com/rss/finanza.xml", "Il Sole 24 Ore"),
    ("Il Sole 24 Ore Economia", "https://www.ilsole24ore.com/rss/economia.xml", "Il Sole 24 Ore"),
    ("ANSA Economia", "https://www.ansa.it/sito/notizie/economia/economia_rss.xml", "ANSA"),
    ("ECB press", "https://www.ecb.europa.eu/rss/press.html", "ECB"),
    ("Fed press", "https://www.federalreserve.gov/feeds/press_all.xml", "Federal Reserve"),
)
# A holding's theme → the search for it. Matched on the instrument's name; a stock is searched by its ticker.
THEMES = (
    (r"FTSE MIB", "Borsa Milano FTSE MIB", "it"),
    (r"S&P 500", "S&P 500", "en"),
    (r"INFORMATION TECH", "tech stocks", "en"),
    (r"NEW ENERGY|CLEAN ENERGY", "clean energy stocks", "en"),
    (r"EMERGING", "emerging markets stocks", "en"),
    (r"CYBER", "cybersecurity stocks", "en"),
    (r"ALL-WORLD|MSCI WORLD\b", "global stock markets", "en"),
)
MACRO = (("ECB interest rates", "en"), ("BCE tassi spread BTP", "it"), ("Federal Reserve rates", "en"))
PER_QUERY = 15  # newest items kept per feed or search
SAME_STORY = 0.6  # word overlap (Jaccard) above which two titles are one story
FETCHERS = 8  # feeds, searches and articles fetched at once (each host still paced by Http)
_WORD = re.compile(r"[a-zà-ü0-9]{3,}")


@dataclass(frozen=True)
class Query:
    text: str
    lang: str
    about: str  # what it is for: a holding's name, 'markets', 'macro'


def queries(holdings: list[Holding]) -> list[Query]:
    """The macro searches, then one per holding: a US stock by its ticker, a fund by its theme (THEMES), and anything
    else (a stock listed in Milan, a fund of a theme not listed) by its own name, so no holding is left without news."""
    from franziska.market.brief import short  # brief imports this module

    out = [Query(t, lang, "macro") for t, lang in MACRO]
    for h in holdings:
        if h.kind == "stock" and h.ticker and re.fullmatch(r"[A-Z]{1,5}", h.ticker):
            out.append(Query(f"{h.ticker} stock", "en", h.name))
            continue
        theme = next(((text, lang) for pattern, text, lang in THEMES if re.search(pattern, h.name, re.IGNORECASE)),
                     None)
        if theme is None:
            theme = (f"{short(h.name)} stock" if h.kind == "stock" else short(h.name), "en")
            log.info("news: no theme for %s; searching its name: %s", h.name, theme[0])
        out.append(Query(*theme, h.name))
    return list({q.text: q for q in out}.values())


def collect(http: Http, db: Path, holdings: list[Holding], days: int, searxng_url: str = "") -> list[str]:
    """Fetch every feed and search into market.sqlite, in parallel (Http still paces each host). Returns the
    failures (an empty list when all answered)."""
    jobs = [(name, lambda u=url, n=name, p=publisher: sorted(sources.rss(http, u, n, p),
                                                             key=lambda i: i.published)[-PER_QUERY:])
            for name, url, publisher in FEEDS]
    stocks = [h.ticker for h in holdings if h.kind == "stock" and h.ticker]
    if stocks:
        jobs.append(("Yahoo Finance headlines", lambda: sources.rss(
            http, f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={','.join(stocks)}&region=US&lang=en-US",
            f"Yahoo Finance: {', '.join(stocks)}")[:PER_QUERY]))
    for q in queries(holdings):
        search = ((lambda q=q: sources.searxng(http, searxng_url, q.text, days, q.lang)) if searxng_url
                  else (lambda q=q: sources.google_news(http, q.text, q.lang, days)))
        jobs.append((f"search '{q.text}'", lambda s=search: s()[:PER_QUERY]))
    failed, found = [], []
    with ThreadPoolExecutor(FETCHERS) as pool:
        for (name, _), result in zip(jobs, [pool.submit(fn) for _, fn in jobs]):
            try:
                found += result.result()
            except SourceError as e:
                failed.append(f"{name}: {e}")
    with closing(store.connect(db)) as con, con:
        con.executemany(
            "INSERT OR IGNORE INTO items (id, url, title, source, published, summary, feed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(store.item_id(i.url), i.url, i.title, i.source, i.published.isoformat(timespec="minutes"), i.summary,
              i.feed) for i in found])
    log.info("news: %d items fetched, %d sources failed", len(found), len(failed))
    return failed


@dataclass
class Story:
    id: str
    url: str
    title: str
    source: str
    published: datetime
    summary: str
    feed: str
    text: str | None
    digest: str | None
    also: list[str]  # other outlets with the same story

    @property
    def readable(self) -> bool:
        return "news.google.com" not in self.url


def stories(db: Path, start: datetime, end: datetime) -> list[Story]:
    """The stored items published in [start, end) (UTC), one per story, newest first."""
    with closing(store.connect(db)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT * FROM items WHERE published >= ? AND published < ? ORDER BY published DESC",
                           (start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"))).fetchall()
    kept: list[tuple[set[str], Story]] = []
    for r in rows:
        s = Story(r["id"], r["url"], r["title"], r["source"], datetime.fromisoformat(r["published"]), r["summary"],
                  r["feed"], r["text"], r["digest"], [])
        words = set(_WORD.findall(s.title.lower()))
        twin = next((k for k in kept if words and len(words & k[0]) / len(words | k[0]) >= SAME_STORY), None)
        if twin is None:
            kept.append((words, s))
            continue
        other = twin[1]
        better = (s.readable, bool(s.summary)) > (other.readable, bool(other.summary))
        if better:
            s.also, s.digest, s.text = [*other.also, other.source], s.digest or other.digest, s.text or other.text
            kept[kept.index(twin)] = (words | twin[0], s)
        elif s.source not in other.also and s.source != other.source:
            other.also.append(s.source)
    return [s for _, s in kept]


def window(edition: str, now: datetime) -> tuple[datetime, datetime]:
    """The UTC span a report covers: the last 30 hours (a weekday), or the last 7 days and a night (Monday)."""
    back = timedelta(days=7, hours=8) if edition == "weekly" else timedelta(hours=30)
    return now - back, now


def save_text(db: Path, item: str, text: str | None) -> None:
    with closing(store.connect(db)) as con, con:
        con.execute("UPDATE items SET text = ? WHERE id = ?", (text or "", item))


def save_digest(db: Path, item: str, digest: str) -> None:
    with closing(store.connect(db)) as con, con:
        con.execute("UPDATE items SET digest = ? WHERE id = ?", (digest, item))
