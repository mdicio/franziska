"""CLI for the finance assistant.

    python -m franziska.finance import FILE.xlsx ...   import bank exports (idempotent), report bills paid
    python -m franziska.finance --month [YYYY-MM]      monthly summary (no LLM)
    python -m franziska.finance --week [YYYY-MM-DD]    a Monday-to-Sunday week, default the last complete one (no LLM)
    python -m franziska.finance --recurring            monthly charges found in the data (no LLM)
    python -m franziska.finance --budgets              spending against budgets (no LLM)
    python -m franziska.finance --review               biggest payees whose group is the model's guess (no LLM)
    python -m franziska.finance --categorise           group payees that have none (local model, AnyJev)
    python -m franziska.finance "question"             ask the agent
"""

import sys
from pathlib import Path

from franziska import guards
from franziska.config import Settings
from franziska.finance import categories
from franziska.finance.agent import agent
from franziska.finance.service import Money, eur
from franziska.llm import USAGE_LIMITS, build_model
from franziska.router import Router
from franziska.secretary.service import Life


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    settings = Settings.from_env()
    money = Money.open(settings.ledger_db, settings.timezone, Life.open(settings.life_db, settings.timezone),
                       owner=settings.owner_name)
    command, *args = argv
    if command == "import" and args:
        for f in args:
            print(money.import_file(Path(f)))
    elif command == "--month":
        print(money.month_summary(args[0] if args else None))
    elif command == "--week":
        print(money.week_summary(args[0] if args else None))
    elif command == "--recurring":
        print(money.recurring_text())
    elif command == "--budgets":
        print(money.budget_status())
    elif command == "--review":
        print("\n".join(f"{p} · {n}× · {eur(c)} → {g}" for p, n, c, g in money.review(20)))
    elif command == "--categorise":
        router = Router.from_settings(settings)
        print(money.categorise(categories.decider(router), settings.router_model or settings.model, progress=print))
    else:
        print(guards.run_sync(agent, " ".join(argv), "money", money.now(), model=build_model(settings), deps=money,
                              usage_limits=USAGE_LIMITS))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
