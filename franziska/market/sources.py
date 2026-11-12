"""Public data over HTTP, no keys and no model: closes (Yahoo's chart endpoint, FRED), FX and rates (ECB), news (RSS
feeds, Google News search feeds), central-bank calendars (the Fed's and the ECB's own pages), earnings dates (Nasdaq),
and an article's text (trafilatura).

Polite by construction: one client, at most one request per host every PACE_S, one retry after a 429/503 that waits
what the server asks (capped), then SourceError. Callers degrade on SourceError; nothing here retries harder.
"""

import csv
import html
import io
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Franziska/0.1 (personal market brief)"
PACE_S = {"query1.finance.yahoo.com": 1.5, "news.google.com": 1.0}
DEFAULT_PACE_S = 0.5
RETRY_WAIT_MAX_S = 20.0
ARTICLE_CHARS = 5000  # of an article's text kept for the model (INVEST.md §4: ~6k)


class SourceError(RuntimeError):
    """A source didn't answer usefully; the message says which and why."""


class Http:
    def __init__(self, timeout_s: float = 20.0):
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout_s, follow_redirects=True)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, url: str, **params) -> httpx.Response:
        host = urlparse(url).netloc
        for attempt in range(2):
            with self._lock:  # reserve this host's next slot; the wait is outside, so other hosts go meanwhile
                slot = max(time.monotonic(), self._last.get(host, 0) + PACE_S.get(host, DEFAULT_PACE_S))
                self._last[host] = slot
            if (wait := slot - time.monotonic()) > 0:
                time.sleep(wait)
            try:
                r = self.client.get(url, params=params or None)
            except httpx.HTTPError as e:
                raise SourceError(f"{host}: {type(e).__name__}") from e
            if r.status_code in (429, 503) and attempt == 0:
                after = r.headers.get("retry-after", "")
                delay = min(float(after) if after.isdigit() else 5.0, RETRY_WAIT_MAX_S)
                log.info("%s answered %d; waiting %.0f s once", host, r.status_code, delay)
                time.sleep(delay)
                continue
            if r.status_code != 200:
                raise SourceError(f"{host}: HTTP {r.status_code}")
            return r
        raise SourceError(f"{host}: rate-limited")


# ---- closes ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Series:
    symbol: str
    currency: str  # '' for an index level or a rate
    name: str
    rows: list[tuple[date, float]]  # oldest first, one per trading day


def yahoo_chart(http: Http, symbol: str, days: int = 40) -> Series:
    """Daily closes for the last `days` (the newest bar is today's price while its market is open)."""
    rng = "1mo" if days <= 31 else "3mo" if days <= 92 else "1y"
    r = http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}", range=rng, interval="1d")
    try:
        res = r.json()["chart"]["result"][0]
        meta, stamps = res["meta"], res.get("timestamp") or []
        closes = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise SourceError(f"Yahoo {symbol}: no data") from e
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
    rows: dict[date, float] = {}
    for t, c in zip(stamps, closes, strict=False):
        if c is not None:
            rows[datetime.fromtimestamp(t, tz).date()] = float(c)
    if not rows:
        raise SourceError(f"Yahoo {symbol}: no closes")
    return Series(symbol, meta.get("currency") or "", meta.get("longName") or meta.get("shortName") or symbol,
                  sorted(rows.items()))


def yahoo_search(http: Http, query: str) -> list[tuple[str, str, str]]:
    """(symbol, exchange, quote type) of Yahoo's matches for a name or an ISIN."""
    r = http.get("https://query1.finance.yahoo.com/v1/finance/search", q=query, quotesCount=15, newsCount=0)
    return [(q["symbol"], q.get("exchange", ""), q.get("quoteType", "")) for q in r.json().get("quotes", [])
            if q.get("symbol")]


def fred(http: Http, series_id: str, since: date) -> list[tuple[date, float]]:
    r = http.get("https://fred.stlouisfed.org/graph/fredgraph.csv", id=series_id, cosd=since.isoformat())
    rows = []
    for rec in csv.reader(io.StringIO(r.text)):
        if len(rec) == 2 and re.fullmatch(r"\d{4}-\d\d-\d\d", rec[0]) and rec[1] not in (".", ""):
            rows.append((date.fromisoformat(rec[0]), float(rec[1])))
    if not rows:
        raise SourceError(f"FRED {series_id}: no data")
    return rows


def ecb(http: Http, key: str, since: date) -> list[tuple[date, float]]:
    """An ECB Data Portal series, e.g. 'EXR/D.USD.EUR.SP00.A' (USD per EUR) or the AAA 10-year yield."""
    r = http.get(f"https://data-api.ecb.europa.eu/service/data/{key}", startPeriod=since.isoformat(),
                 format="csvdata")
    rows = []
    for rec in csv.DictReader(io.StringIO(r.text)):
        day, value = rec.get("TIME_PERIOD", ""), rec.get("OBS_VALUE", "")
        if value:
            rows.append((date.fromisoformat(day if len(day) == 10 else f"{day}-01"), float(value)))
    if not rows:
        raise SourceError(f"ECB {key}: no data")
    return rows


# ---- news -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Item:
    url: str
    title: str
    source: str
    published: datetime  # UTC, naive
    summary: str
    feed: str


def _text(fragment: str | None) -> str:
    """Feed text without markup or entities, on one line."""
    t = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(fragment or "")))
    return re.sub(r"\s+", " ", t).strip()


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    return (dt.astimezone(UTC) if dt.tzinfo else dt).replace(tzinfo=None)


def rss(http: Http, url: str, feed: str, source: str = "") -> list[Item]:
    """An RSS 2.0 or Atom feed. `source`: the publisher's name; Google News items carry their own."""
    r = http.get(url)
    try:
        root = ElementTree.fromstring(r.content)
    except ElementTree.ParseError as e:
        raise SourceError(f"{feed}: not a feed") from e
    atom = "{http://www.w3.org/2005/Atom}"
    out = []
    for it in root.iter("item"):
        link, title = (it.findtext("link") or "").strip(), _text(it.findtext("title"))
        when = _when(it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"))
        if not (link and title and when):
            continue
        src = (it.findtext("source") or "").strip() or source
        if src and title.endswith(f" - {src}"):  # Google News: 'Headline - Publisher'
            title = title[: -len(src) - 3]
        summary = "" if "news.google.com" in link else _text(it.findtext("description"))
        out.append(Item(link, title, src or urlparse(link).netloc, when, summary[:600], feed))
    for e in root.iter(f"{atom}entry"):
        link_el = e.find(f"{atom}link")
        link, title = (link_el.get("href", "") if link_el is not None else ""), _text(e.findtext(f"{atom}title"))
        when = _when(e.findtext(f"{atom}published") or e.findtext(f"{atom}updated"))
        if link and title and when:
            out.append(Item(link, title, source or urlparse(link).netloc, when, _text(e.findtext(f"{atom}summary"))[:600],
                            feed))
    return out


def google_news(http: Http, query: str, lang: str, days: int) -> list[Item]:
    """A Google News search as a feed (personal use, as its terms allow). Links are Google's redirects: the
    title and publisher are kept, the page itself is not read."""
    region = {"it": ("it", "IT", "IT:it"), "en": ("en-US", "US", "US:en")}[lang]
    url = "https://news.google.com/rss/search?" + urlencode(
        {"q": f"{query} when:{days}d", "hl": region[0], "gl": region[1], "ceid": region[2]})
    return rss(http, url, f"Google News: {query}")


def searxng_search(http: Http, base_url: str, query: str, category: str = "general", lang: str = "",
                   time_range: str = "") -> dict:
    """A self-hosted SearXNG's JSON answer (FRANZISKA_SEARXNG_URL; deploy/searxng). SourceError when it is down, its
    JSON output is off, or every engine failed."""
    params = {"q": query, "format": "json", "categories": category}
    if lang:
        params["language"] = lang
    if time_range:
        params["time_range"] = time_range
    r = http.get(base_url.rstrip("/") + "/search", **params)
    try:
        data = r.json()
    except ValueError as e:
        raise SourceError("SearXNG: not JSON (is 'json' in search.formats?)") from e
    if not data.get("results") and data.get("unresponsive_engines"):
        raise SourceError("SearXNG: no engine answered (" + ", ".join(
            f"{e}: {why}" for e, why in data["unresponsive_engines"]) + ")")
    return data


# '9 ore fa | RomaToday', '2 hours ago | Reuters': how Bing and DuckDuckGo news give a result's age (no publishedDate)
_AGE = re.compile(r"\b(\d+)\s*([a-z]+)\s+(?:fa|ago)\b", re.IGNORECASE)
_AGE_S = {"sec": 1, "min": 60, "m": 60, "ora": 3600, "ore": 3600, "hou": 3600, "h": 3600, "gio": 86400, "day": 86400,
          "d": 86400, "set": 604800, "wee": 604800, "w": 604800, "mes": 2592000, "mon": 2592000}


def searxng_published(res: dict, now: datetime | None = None) -> datetime | None:
    """A SearXNG result's time (UTC, naive): its publishedDate, else the age in its metadata, else None."""
    if when := _when(res.get("publishedDate")):
        return when
    if (m := _AGE.search(res.get("metadata") or "")) and (unit := _AGE_S.get(m[2].lower()[:3])):
        return (now or datetime.now(UTC).replace(tzinfo=None)) - timedelta(seconds=int(m[1]) * unit)
    return None


def searxng_source(res: dict) -> str:
    """The publisher: the name news engines put after '|' in the metadata, else the site."""
    meta = res.get("metadata") or ""
    name = meta.split("|", 1)[1].strip() if "|" in meta else ""
    return name or urlparse(res["url"]).netloc.removeprefix("www.")


def searxng(http: Http, base_url: str, query: str, days: int, lang: str = "") -> list[Item]:
    """News from SearXNG, dated, of the last `days`. No time_range: SearXNG then leaves out every engine that can't
    filter by time, which on 2026-10-30 was every news engine that answered (0 results). The period is kept here."""
    now = datetime.now(UTC).replace(tzinfo=None)
    out = []
    for res in searxng_search(http, base_url, query, "news", lang)["results"]:
        when = searxng_published(res, now)
        if res.get("url") and res.get("title") and when and now - when <= timedelta(days=days):
            out.append(Item(res["url"], _text(res["title"]), searxng_source(res), when,
                            _text(res.get("content"))[:600], f"SearXNG: {query}"))
    return out


def article_text(http: Http, url: str) -> str | None:
    """The article's main text, or None (Google redirect, paywall, script-only page)."""
    if "news.google.com" in url:
        return None
    import trafilatura  # slow to import; only the runs that read pages need it

    try:
        page = http.get(url).text
    except SourceError:
        return None
    text = trafilatura.extract(page, include_comments=False, include_tables=False, favor_precision=True)
    if not text or len(text) < 300:
        return None
    return text[:ARTICLE_CHARS]


# ---- calendars ------------------------------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                      "september", "october", "november", "december"], 1)}
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"


def fomc_meetings(http: Http) -> list[tuple[date, str]]:
    """Each FOMC meeting's decision day (its last day), from the Fed's calendar page."""
    page = http.get(FOMC_URL).text
    out = []
    for year in re.findall(r"(\d{4}) FOMC Meetings", page):
        start = page.find(f"{year} FOMC Meetings")
        nxt = page.find("FOMC Meetings", start + 20)
        section = page[start: nxt if nxt > 0 else len(page)]
        for month, days in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?'
                                      r'fomc-meeting__date[^>]*>([^<]+)<', section, re.DOTALL):
            last_month = month.split("/")[-1].strip().lower()
            m = MONTHS.get(last_month) or next((v for k, v in MONTHS.items() if k.startswith(last_month[:3])), None)
            nums = re.findall(r"\d+", days)
            if m and nums:
                notes = " (with projections)" if "*" in days else ""
                out.append((date(int(year), m, int(nums[-1])), f"Fed: FOMC rate decision{notes}"))
    if not out:
        raise SourceError("Fed calendar: no meetings found")
    return out


def ecb_meetings(http: Http) -> list[tuple[date, str]]:
    """The ECB's monetary policy decision days (day 2 of each meeting) from its Governing Council calendar."""
    page = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", http.get(ECB_URL).text)))
    found = re.findall(r"(\d\d)/(\d\d)/(\d{4}) Governing Council of the ECB: monetary policy meeting[^()]*"
                       r"(?:\(Day (\d)\))?", page)
    out = sorted({date(int(y), int(m), int(d)) for d, m, y, day in found if day in ("", "2")})
    if not out:
        raise SourceError("ECB calendar: no meetings found")
    return [(d, "ECB: rate decision") for d in out]


def earnings_date(http: Http, symbol: str) -> tuple[date, str] | None:
    """A US stock's next earnings day from Nasdaq's page data: (day, 'confirmed' | 'estimated')."""
    try:
        data = http.get(f"https://api.nasdaq.com/api/analyst/{symbol}/earnings-date").json().get("data") or {}
    except (SourceError, ValueError):
        return None
    m = re.search(r"(\w{3}) (\d\d), (\d{4})", data.get("announcement") or "")
    if not m or m.group(1).lower() not in [k[:3] for k in MONTHS]:
        return None
    month = next(v for k, v in MONTHS.items() if k.startswith(m.group(1).lower()))
    text = (data.get("reportText") or "").lower()
    return date(int(m.group(3)), month, int(m.group(2))), "estimated" if "estimated" in text else "expected"
