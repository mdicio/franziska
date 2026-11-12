"""The market report (docs/INVEST.md §4): a daily edition on weekdays and a weekly one on Mondays, as a PDF.

Two outputs, kept apart (DESIGN decision 27):
- the **market report**: markets, rates, FX, commodities, central banks and the news on the instruments held, written
  by the model from public data only (names, tickers, themes, articles). Stored in `market.sqlite`.
- the **portfolio overlay**: value, its change, each holding's contribution, cash share, concentration. Code only,
  from the ledger and the closes; it goes into the PDF and the Telegram caption, never into the model's input.

Pieces: `sources.py` (HTTP: Yahoo closes, FRED, ECB, RSS, Google News, Nasdaq earnings dates, article text),
`store.py` (market.sqlite), `figures.py` (the market table), `news.py` (collect and dedupe), `overlay.py`,
`brief.py` (the pipeline and its degrade ladder), `pdf.py` (HTML → PDF). `workflows/markets.py` makes it a route,
`/markets` and a CLI; the bot sends it at FRANZISKA_MARKET_AT on weekdays.
"""
