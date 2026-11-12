"""CLI for the investments (DEGIRO, docs/INVEST.md). No model.

    python -m franziska.invest import FILE.csv ... [--on YYYY-MM-DD]   import exports (idempotent); --on dates a
                                                                     Portfolio export (else its file name, else today)
    python -m franziska.invest show                                  value, cash, gains and positions
"""

import sys
from datetime import date
from pathlib import Path

from franziska.config import Settings
from franziska.invest import importer
from franziska.invest.importer import ImportFormatError
from franziska.invest.service import Portfolio


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    portfolio = Portfolio.open(settings.ledger_db, settings.timezone)
    command, *args = argv
    if command == "import" and args:
        on = None
        if "--on" in args:
            i = args.index("--on")
            on, args = date.fromisoformat(args[i + 1]), args[:i] + args[i + 2:]
        # Trades and the statement first, so a snapshot is checked against them in the same run.
        order = {"degiro_transactions": 0, "degiro_movements": 1, "degiro_portfolio": 2}
        files = sorted((Path(f) for f in args), key=lambda p: order.get(importer.detect(p) or "", 3))
        status = 0
        for f in files:
            try:
                print(portfolio.import_file(f, on), end="\n\n")
            except ImportFormatError as e:
                print(f"❌ {e}", end="\n\n")
                status = 1
        return status
    if command == "show":
        print(portfolio.overview())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
