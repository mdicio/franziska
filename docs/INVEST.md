# Investments: the DEGIRO data and the financial analyst

**Phase I0 (data in) and I2 (the market report) are built**; the analyst's own route (I1, I3) is not: investment
questions are answered by the money assistant over the same tables. This file covers (1) importing the DEGIRO exports
into tables the agents can read, (2) the financial analyst, and (3) the market report and the research it is built
on. Decisions 23–29 in `DESIGN.md` §7 point here. The principles are the bank side's: code computes, the model reads,
every figure has a source, answers know where the data ends.

DEGIRO is the first broker adapter; another broker is a new importer with the same three outputs (trades, cash
movements, position snapshots).

## 1. The DEGIRO exports

| File | Holds | Overlaps | Role |
|---|---|---|---|
| `Portfolio.csv` | Positions on one day: quantity, closing price, local and EUR value, plus one cash line. **No date in the file**; names cut at ~30 chars | quantities follow from the trades, cash from the movements | a **check** on its day |
| Transactions export | one row per fill: quantity, price, local / EUR value, exchange rate, AutoFX fee, broker fee, total, order id, venue. Also corporate actions as 00:00 rows without an order id (splits, listing changes) | every trade and its fee is also a movement | **source of trades** |
| Account statement | every cash row, Italian or English descriptions: deposits, buys and sells, transaction costs, yearly connection costs per exchange, dividends and withholding, FX debit / credit for foreign-currency trades, interest, courtesy credits, cash-sweep pairs, split and product-change lines | trades and trade fees duplicate the transactions file | **source of everything else** |

What the files guarantee, and the importer checks:
- Positions rebuilt from the trades equal the snapshot for every instrument.
- Every order id is in both files and the trade fees agree.
- The running `Balance` chains per currency (previous + change = balance) with **0 breaks once the cash-sweep rows
  are left out** ("Degiro Cash Sweep Transfer" and the amount-less transfers to and from the partner bank's deposit
  account). The last EUR balance equals the snapshot's cash line, and deposits + every non-sweep EUR movement add up
  to it to the cent.
- One CSV quirk: a long product name wraps onto its own row (`,,ACC ETF,,,…`), to be joined to the row above.

## 2. The data (phase I0)

`franziska/invest/`: `importer.py` (the three formats, the wrapped-name row, the in-file checks), `store.py` (the
`inv_` schema, content-hash ids), `service.py` (`Portfolio`: import, `rebuild`, the cross-file checks, `overview`),
`__main__.py` (`python -m franziska.invest import FILE… [--on YYYY-MM-DD]`, `show`). The bot imports a DEGIRO `.csv`
sent in the chat (detected before the bank formats) and answers `/portfolio`. No model anywhere.

Refusals: a statement with a row missing is refused (the currency chain breaks); an overlapping partial statement adds
only its new rows; a snapshot with a wrong quantity gives ❌ naming the instrument; a statement that starts after the
first trade gives "put in: unknown" instead of a wrong gain; re-importing everything adds 0 rows. Files can come in any
order (checks wait for the missing file).

Details:
- `inv_movements` stores every statement row with its `kind` (trades, fees and sweeps included), so the cross-file
  checks and the balance can be recomputed; income and costs are filtered by kind.
- A snapshot's day comes from its file name (`Portfolio_2026-10-28.csv`), else `--on`, else the import day; the reply
  says which.
- Income in EUR: a USD dividend is converted at the rate of the first USD → EUR conversion at or after it
  (`inv_income`).
- A bank → broker transfer can be linked as an own transfer (the `links` table) instead of counting as spending.

### Where it lives

The tables are in **`ledger.sqlite`** with an `inv_` prefix and the broker is an account of kind `broker`: the same
privacy class as the bank (never on MCP, never to a cloud model).

### Tables

| Table | Holds | Written by |
|---|---|---|
| `instruments` | ISIN, full name (from the trades), kind (etf / stock), currency, exchanges seen, the Yahoo ticker, and facts: TER, index, distributing / accumulating, domicile, region and sector weights, each with source and date | the import; the ticker after the price check (below); facts after a ✅ tap |
| `inv_trades` | one row per fill: order id, time, ISIN, signed quantity, price, currency, local value, EUR value, FX rate, AutoFX fee, fees EUR, total EUR, venue; `kind` = trade / split / product_change | the import |
| `inv_movements` | every statement row with its `kind`: deposit, withdrawal, trade, fee_trade, fee_connection, dividend, withholding, fx_debit, fx_credit, interest, courtesy, sweep, split, product_change; currency, amount, balance after, ISIN, order id | the import |
| `inv_snapshots` | each Portfolio export: day, ISIN, quantity, closing price, currency, value local / EUR; the cash line; the check results | the import |
| `inv_prices`, `fx_rates` | daily closes for holdings, watchlist and benchmarks (Yahoo); EUR/USD and others (ECB reference rates) | nightly job, no model |
| `inv_positions`, `inv_realized`, `inv_income` and the view `inv_costs`; `inv_daily` (phase I1) | per ISIN: quantity, weighted average cost in EUR (fees included, USD at the trade's own rate), value, unrealised and realised gains; per sale: proceeds, cost, gain; per day: value, cash, net deposits, time-weighted index; per year: dividends gross / withholding / net, fees, AutoFX, connection fees | **code, rebuilt** after every import and price update |
| `inv_theses` | why you hold or watch something, what would make you sell, a review date | you, through the analyst (a tap when the run read the web) |
| `watchlist` | ticker or ISIN, why, alert levels | the same |

Weighted average cost is the Italian method: a sale realises proceeds minus the average cost of the shares sold, and
the average of the shares left does not change.

### Import (`python -m franziska.invest import <files…>`, or the files dropped in Telegram)

Detect each file by its header, parse (comma decimals, the wrapped-name row), then check:
1. Movements: the balance chain per currency with sweep rows excluded; 0 breaks or the file is refused, naming the
   first break.
2. Every trade order id appears in both files when both cover its day; movement fees per order = transaction fees.
3. A snapshot: quantities rebuilt from the trades up to its day = its quantities; its cash = the movements' balance
   that day.
4. Idempotent: ids are content hashes (trades: order id + time + ISIN + quantity + price; movements: date, time,
   value date, description, change, currency, balance). Re-importing overlapping exports adds 0 rows.

Then rebuild the derived tables and reply like the bank import: new rows per file, period covered, ✅ / ❌ per
check, positions and value. `python -m franziska.invest show` prints the positions table without a model.

### Keeping it fresh

Positions only change when you trade. Monthly, or after a trade: export *Transactions* and *Account statement*
from the last export's day (overlap is fine), and the *Portfolio* if you want a check. The Sunday freshness nudge
includes the broker when its last movement is more than 30 days old.

### Prices (decision 25)

Yahoo Finance's chart endpoint (unofficial, no key, covers XAMS, XETRA, Euronext Paris, Borsa Italiana, Nasdaq),
fetched by a nightly job with plain httpx, a cache and polite pacing; ECB reference rates for FX.
- Ticker mapping: from the ISIN and the venue you traded on (S&P 500 ETF on XAMS → the Amsterdam line). A mapping is
  accepted only when Yahoo's close on a snapshot day is within 1% of the snapshot's closing price; otherwise
  `/portfolio` says which instrument has no price and values it at its last snapshot.
- If Yahoo breaks, the value falls back to the last snapshot with its date ("valori al 28/10"); nothing is made up.

## 3. The financial analyst (📈 route `invest`, phases I1 and I3; not built)

A local agent (rung 4, Pydantic AI, the same local model as the others) with ~12 tools over `invest/service.py`, the
memory toolset (scope `invest`, privacy class money) and the figures guard. Router: one new route, "investments:
portfolio, ETFs, stocks, markets, dividends, investment ideas", next to `money` (the bank).

### Tools

| Tool | Does |
|---|---|
| `portfolio_overview(as_of?)` | positions, weights, value, unrealised gain, cash, data freshness |
| `position(name)` | one instrument: trades, average cost, realised and unrealised, dividends, its thesis |
| `performance(period, benchmark?)` | time-weighted and money-weighted (XIRR) return, vs a benchmark (default FTSE All-World) |
| `income_and_costs(year?)` | dividends gross / withheld / net, fees, AutoFX, connection fees, estimated TER cost |
| `exposure(by)` | asset type, region, sector, currency, single stocks vs ETFs; look-through where `instruments` has ETF weights |
| `query_portfolio(sql)` | read-only SQL over the `inv_` views, same authorizer as `query_ledger` |
| `what_if(changes)` | code only: new weights, realised gain, estimated 26% tax, fees for "sell half of X, add 1,000 € All-World". Never an order |
| `cash_context()` | typed call into `Money`: bank balances, average monthly savings, months of expenses covered. Never bank rows |
| `market_notes(query)` | FTS over `market.sqlite` (the report's articles and texts) |
| `web_search(query)`, `read_page(url)` | SearXNG; queries checked in code (decision 26); the run is tainted after them |
| `watchlist(...)`, `thesis(...)` | read and propose writes |

Examples it must answer from the tools: "com'è andato quest'anno vs l'All-World?", "quanto ho preso di dividendi nel
2025?", "quanto ho pagato di commissioni da sempre?", "quanta tecnologia ho davvero?", "se vendo metà di X cosa
succede?". It should also point out, unasked: idle cash, concentration in single stocks, the largest position.

### Your style (phase I3)

`/invest_setup` asks ~8 questions once: goals and horizon, the fall you'd sit through, monthly contribution, target
split (core ETFs vs single stocks vs themes), themes you like or avoid, your rules ("no single stock above 10%",
"keep 6 months of expenses at the bank"). The answers become `profile/investor` (stated, saved on the 💾 tap) and
targets in a table. Code adds a factual behaviour summary from the history (deposit rhythm, lump sums, holding
periods, big sells and buys with their dates) that the analyst compares with what you stated. Drift from your
targets appears in the brief and in answers.

### Opportunities

For "cosa faccio con la liquidità?" or "ha senso la difesa europea?" the answer has a fixed shape:
1. what you already hold there (exposure, overlap);
2. what the market says: dated sources from `market.sqlite` plus fresh searches;
3. 2–3 options, each through `what_if` (allocation, costs, tax, concentration);
4. how each fits your stated rules and your `cash_context`;
5. what would change the view.

Also: watchlist with price alerts (in the brief), theses reviewed quarterly, "where should the next 500 € go"
(code: the most underweight against your targets). It never says "buy now" and never trades: DEGIRO has no
official API and the unofficial ones are not used.

## 4. The market report (phase I2, decisions 27–28; built)

`franziska/market/` (`sources.py` HTTP, `store.py` market.sqlite, `figures.py` the market table, `news.py`,
`tickers.py`, `overlay.py`, `brief.py` the pipeline, `pdf.py` WeasyPrint), `workflows/markets.py` (route `markets`,
`/markets [weekly]`, `python -m franziska.workflows markets "weekly"`), `python -m franziska.market daily|weekly
[--day D] [--no-model]`, `tickers`, `ticker ISIN SYMBOL`. The bot sends it on weekdays at `FRANZISKA_MARKET_AT`
(after the life brief), the weekly edition on Mondays; the web UI links the PDF. Every run also stores the report as
plain text in `market.sqlite` (`reports.text`: the PDF minus the portfolio overlay), read back by the
`market_report(edition, day)` tool on the money route and on the MCP server.

- **Output is a PDF** (A4, 2–3 pages: headline and summary, tiles, your portfolio with a 30-session chart and a
  table per holding, the news by topic with numbered citations, your holdings in the news, the calendar, the
  markets table with sparklines, the sources with links). The Telegram message is its caption.
- **News**: RSS feeds (CNBC, MarketWatch, FT, Il Sole 24 Ore, ANSA, ECB, Fed), Yahoo Finance headlines for the
  stocks, and code-written queries per holding theme (`news.THEMES`) and three macro ones, searched on SearXNG
  (Google News search feeds without it). SearXNG news gets no `time_range` (it would leave out every engine that
  answers); the period is kept in code from each result's date or age ('9 ore fa').
- Page text by trafilatura for direct links; Google News links are redirects, so those stories are read from the
  title and the other outlets' copies of the same story.
- Closes: Yahoo first, FRED / ECB as fallbacks and for rates (euro area AAA 10y as the Bund's proxy, Italy's 10y
  only as the ECB's monthly average: no free daily BTP source). Closes are stored in `market.sqlite` (`prices`),
  not `inv_prices`: they are public data.
- Tickers (decision 25): found by search and checked against the snapshot (DEGIRO's export carries the previous
  session's close, so both days are tried); one that search can't find is set by hand
  (`python -m franziska.market ticker ISIN SYMBOL`, checked the same way).
- Calendar: FOMC and ECB decision days read from their own pages, earnings dates of the stocks from Nasdaq.
- The figures guard: every number in a point must be in the table, the calendar or the stories it cites; a report
  with misses is rewritten once, then failing points are dropped. Story digests are cached, so Monday reuses the
  week's.
- Timing on gemma-4-26B: daily ~2.5 min (figures 15 s, news 15–30 s, model ~1.5 min, overlay 14 s), weekly
  ~2–4.5 min.

Two outputs, so the research can move to a stronger model without your data:

| Output | Contains | Where it runs | Stored |
|---|---|---|---|
| **Market report** | markets, macro, rates, FX, commodities, news on the instruments held + watchlist + your themes, the week's calendar (earnings, ex-dividend days, central banks, macro data) | the local model, or a cloud one on `FRANZISKA_CLOUD_ROUTES=markets`. Its input is **only** public data: instrument names, tickers, themes, articles. Never quantities, weights, amounts, gains | `market.sqlite` |
| **Portfolio overlay** | your value and its change, contributions per holding, cash share, drift from targets, watchlist alerts, data freshness | always local, code only (no model) | only in the PDF, not in `market.sqlite` |

### The workflow (`markets`, a workflow per DESIGN §5: it never writes outside its stores)

1. **Code**: prices, benchmarks (S&P 500, Stoxx 600, FTSE MIB, Nasdaq 100, MSCI EM), EUR/USD, Bund and BTP 10y, Brent,
   gold; the overlay figures; calendar items.
2. **Search, code-written queries**: fixed queries from the instruments, themes and macro topics; dedupe by URL and
   title; extract page text (trafilatura), capped per article; snippet only when a page can't be read.
3. **Map**: one typed extraction per article (topic, instruments concerned, 2-line summary, date, source). Pages
   are untrusted: only these typed fields go on.
4. **Reduce**: one generation writes the report from the extractions and the market figures. The figures guard
   checks every number against them; every news line carries source and date.
5. **Assemble**: overlay + report → the PDF.

**It always produces an output.** Ladder: full report → if the model is busy or off, the overlay plus the
headlines with their links (no summaries) → if search fails too, the overlay alone with "notizie non disponibili".
Each degraded report says which step failed.

Example caption (every number invented):
```
📈 Mercati, mar 3 nov
Portafoglio 25,000 € (+0.8% ieri, +2.1% settimana) · liquidità 20%
↑ Stock A +3.4% (contributo +120 €) · ↓ ETF B −1.9%
S&P 500 +0.4 · Stoxx 600 −0.2 · FTSE MIB +0.6 · EUR/USD 1.112
📅 Stock A trimestrale mer 19 · BCE giovedì
⚠️ Single stock 30% dell'investito (tuo limite: 25%)
```

### SearXNG

Self-hosted in Docker (`deploy/searxng/`), JSON output enabled (`search.formats: [html, json]`), reached at
`FRANZISKA_SEARXNG_URL`. The search engines it queries see the queries from your IP: instrument names and topics,
nothing else. It can be rate-limited by an engine; the report then degrades as above.

## 5. Privacy and safety

- Portfolio data (`inv_` tables, `invest` memory, the overlay) is the money class: local model only, never MCP,
  never a cloud request. The check sits in code at the cloud wrapper, not in a prompt.
- `market.sqlite` holds public data only and is the one store a cloud model may read, when the `markets` route is on
  a cloud model.
- Search queries: code writes the report's; in Q&A, code rejects a model-written query containing any number other
  than a year, so no amount or quantity reaches a search engine.
- A run that read a web page is tainted: watchlist, thesis and target writes become proposals you tap.
- No orders, ever.

## 6. Build order

| Phase | What | Status |
|---|---|---|
| **I0: data in** | `invest/importer.py`, the tables, the checks, the CLI (`import`, `show`), Telegram upload detection, `/portfolio` (snapshot values, no model) | built |
| **I1: the analyst answers** | Yahoo prices + ticker check, ECB FX, the derived tables (performance, income, costs), `invest` route + agent + tools, `/perf` | not built: money answers over the same tables |
| **I2: the report** | SearXNG, `market.sqlite`, the `markets` workflow, weekday and Monday jobs, the degrade ladder, `/markets` | built |
| **I3: opportunities** | `/invest_setup`, targets and drift, `what_if`, watchlist and alerts, theses, web research in Q&A, ETF look-through facts | not built |
| Backlog | Tax helper from DEGIRO's yearly statements (RW / IVAFE, RT gains, RM foreign dividends); the web UI pages (value vs deposits, allocation) | — |
