"""CLI for the market report.

    python -m franziska.market daily|weekly [--day YYYY-MM-DD] [--no-model]   build the PDF (and print the caption)
    python -m franziska.market tickers [--force]                             find a checked price symbol per holding
    python -m franziska.market ticker ISIN SYMBOL                            set one by hand (checked the same way)

`--day`: the morning the report is for (closes before it; the news is always the latest). `--no-model`: the
degraded edition (figures, overlay, headlines) without the local model.
"""

import logging
import sys
from datetime import date, datetime, time

import pydantic_ai

from franziska import workflows
from franziska.config import Settings
from franziska.invest.service import Portfolio
from franziska.llm import Models
from franziska.market import pdf, tickers
from franziska.market.brief import Market


def main(argv: list[str]) -> int:
    pydantic_ai.BANNER_ENABLED = False
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "weasyprint", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not argv:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    portfolio = Portfolio.open(settings.ledger_db, settings.timezone)
    command, *args = argv
    if command == "tickers":
        print("\n".join(tickers.map_tickers(portfolio, settings.market_db, force="--force" in args)))
        return 0
    if command == "ticker" and len(args) == 2:
        print(tickers.set_ticker(portfolio, settings.market_db, args[0], args[1]))
        return 0
    if command not in ("daily", "weekly"):
        print(__doc__)
        return 2
    day = date.fromisoformat(args[args.index("--day") + 1]) if "--day" in args else None
    real_now = datetime.now()
    clock = (lambda: datetime.combine(day, time(7, 30))) if day else lambda: datetime.now()
    market = Market.from_settings(settings, portfolio)
    ctx = workflows.Ctx(model=Models(settings).for_route("markets"), router=None, now=clock, progress=lambda line: print(f"… {line}"))
    brief = market.build(ctx, command, day, use_model="--no-model" not in args)
    if day:  # the news can't be rewound: it is today's
        brief.created = real_now
    path = pdf.write_pdf(brief, market.reports_dir, market.lang, market.written_by(ctx.model), market.timezone)
    print(pdf.caption(brief))
    print(f"\n{path}\ntimings: {brief.timings}; points dropped by the figures check: {brief.dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
