"""Agent memory over a simulated week (CHANGELOG §2, DESIGN §6), on the real bot and local model. Run on demand.

The same harness as evals/week.py (fake Telegram, scratch stores, simulated clock), with steps that probe what
the agents learn: notes vs memory, aliases that a later turn must use, profile facts only after the 💾 tap, a new
value replacing the old one, no tasks or amounts in memory, money's own scope, forgetting on request, and the
nightly expiry of a stale entry seeded 61 days back. Judged on the memory files and the stores, not on prose.

    uv run --env-file .env python -m evals.memory              # ~5 min on the 26B (16 steps), budget 10 min
    uv run --env-file .env python -m evals.memory --list       # the steps, without running anything

Results: evals/runs/memory-<time>/ (steps.jsonl, summary.md). Holds the GPU like the week eval.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import timedelta

from evals import week
from evals.week import Check, Step, bill, count, either, memory, note, reply, say, tap, task
from franziska.finance import ledger


def reminder_rule(pattern: str) -> Check:
    return Check(f"reminder rule /{pattern}/",
                 lambda w, o: bool(w.rows("SELECT words AS t FROM reminder_rules", pattern=pattern)))


BUDGET_S = 10 * 60


def memory_count(scope: str, pattern: str, n: int) -> Check:
    return Check(f"{n} {scope} memor{'y' if n == 1 else 'ies'} /{pattern}/",
                 lambda w, o: sum(bool(re.search(pattern, f"{e.name} {e.description} {e.body}", re.I))
                                  for e in w.bot.memory.entries([scope])) == n)


def called(tool: str) -> Check:
    return Check(f"{tool} called", lambda w, o: any(c.name == tool and c.ok for g in o.guards for c in g.calls))


def logged(route: str) -> Check:
    """The turn log has this route's turns, with their tool calls (DESIGN §6, past conversations)."""
    sql = "SELECT count(*) FROM turns WHERE route = ? AND tools != '[]'"
    return Check(f"turn log: {route} turns with tool calls", lambda w, o: w.bot.turns.con.execute(sql, (route,))
                 .fetchone()[0] > 0)


STEPS: list[Step] = [
    # notes are the user's notebook, not memory
    say("mon 09:00", "ricordati che il codice del cancello è 4471", "life",
        note("4471"), memory("life", "4471", present=False)),
    # an alias the next turns must use
    say("mon 09:05", "quando dico 'il dentista' intendo il dott. Bianchi, studio in via Po", "life",
        memory("life", "bianchi")),
    say("mon 09:10", "ricordami di chiamare il dentista domani alle 10", "life",
        task("dentist|bianchi", remind_at="2026-11-03T10:00"), memory_count("life", "bianchi", 1)),
    # profile: proposal, nothing saved until the tap, no "saved" claim before it
    say("mon 12:00", "abito in via Roma 12, scala B interno 7", "life",
        memory("profile", "roma 12", present=False), week.button("💾"),
        reply(r"\b(salvat|memorizzat|saved)", present=False),
        known="gemma4 sometimes says 'ho memorizzato' before the tap (CHANGELOG §2)"),
    tap("mon 12:01", r"", r"💾", memory("profile", "roma 12")),
    # a new value replaces the old one (same name), no second entry
    say("tue 10:00", "mi sono trasferito: ora abito in via Verdi 3, interno 2", "life", week.button("💾")),
    tap("tue 10:01", r"", r"💾", memory("profile", "verdi 3"), memory("profile", "roma 12", present=False)),
    # the profile reaches other routes: chat reads it, with no tools
    say("tue 10:05", "in che via abito?", "life", reply(r"verdi")),
    # tasks and amounts live in their tools, never in memory
    # a payment is a task and a bill, whichever route gets it
    say("wed 09:00", "devo pagare 50 euro al meccanico entro venerdì", None,
        bill("meccanico", 5000), task("meccanico", due_at="2026-11-06"),
        memory("life", r"meccanico|\b50\b", present=False), memory("money", r"meccanico|\b50\b", present=False)),
    # a standing reminder preference is a rule the store applies (reminder_rule), or a memory the agent applies
    say("wed 18:00", "le cose della palestra ricordamele sempre la sera, mai la mattina", "life",
        either(reminder_rule("palestra"), memory("life", "palestra"))),
    say("thu 09:00", "ricordami di rinnovare l'abbonamento della palestra domani", "life",
        task("palestra", remind_at=("2026-11-06T18", "2026-11-06T19", "2026-11-06T20", "2026-11-06T21"))),
    # money has its own scope; the payee's group is the ledger's (set_payee_category), a memory entry is optional
    say("thu 19:00", "BAR NERO SRL sull'estratto conto è il caffè sotto l'ufficio, mettilo nei bar", "money",
        either(memory("money", "nero"), Check("BAR NERO is food & drink", lambda w, o: bool(w.ledger(
            "SELECT 1 FROM transactions WHERE merchant = 'BAR NERO SRL' AND category = 'food & drink'")))),
        memory("life", "nero", present=False)),
    # a pure question learns nothing
    say("fri 12:00", "spiegami cos'è un ETF in due righe", "chat",
        memory("life", "etf", present=False), memory("money", "etf", present=False)),
    # forgetting on request
    say("sat 10:00", "dimentica quello che sai sul dentista", "life",
        memory("life", "bianchi", present=False), count("dentist|bianchi", 1)),
    # past conversations: a life turn seeded 40 days back is found; the money turn about the same bathroom isn't
    # reachable from life (its route only)
    say("sat 11:00", "cosa avevamo deciso per il bagno?", "life",
        called("past_conversations"), reply(r"marzo"), reply(r"864", present=False), logged("life")),
    # nightly expiry: the stale entry seeded 61 days back goes, the fresh ones stay
    week.job("sun 04:00", "expire", memory("life", "lavanderia", present=False),
             either(reminder_rule("palestra"), memory("life", "palestra"))),
    say("sun 10:00", "dove abito e qual è il codice del cancello?", "life", reply(r"verdi"), reply(r"4471")),
]


def seed(world: week.World) -> None:
    """A cache entry last used 61 days before the week: the 04:00 expiry on Sunday must remove it. Two old turns for
    past_conversations: a life one it must find, a money one it must not."""
    world.bot.turns.add(0, "life", "rifacciamo il bagno a marzo, con l'idraulico Marco",
                        "Ok: bagno a marzo con l'idraulico Marco.", at=week.MONDAY - timedelta(days=40))
    world.bot.turns.add(0, "money", "la rata per il bagno nuovo è di 864 euro", "Registrato: 864 €.",
                        at=week.MONDAY - timedelta(days=39))
    now = world.now
    world.now = week.MONDAY - timedelta(days=61)
    world.bot.memory.learn("life", "lavanderia", "la lavanderia è quella in via Dante", source="eval seed")
    world.now = now
    # the payee the money step names, as a card payment on the statement
    with world.bot.money._con() as con:
        ledger.insert(con, [ledger.Transaction(
            "intesa", (week.MONDAY - timedelta(days=3)).date(), None, -180, "Pagamento POS BAR NERO SRL MILANO", "",
            "eval seed", merchant="BAR NERO SRL")])


def run(model: str = "", budget_s: float = BUDGET_S, log=print) -> list[dict]:
    return week.run(model=model, log=log, steps=STEPS, name="memory", budget_s=budget_s, setup=seed)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="", help="override FRANZISKA_MODEL for this run")
    p.add_argument("--list", action="store_true", help="print the steps and their checks, run nothing")
    p.add_argument("--budget-min", type=float, default=BUDGET_S / 60, help="stop starting steps after this long")
    args = p.parse_args()
    if args.list:
        for s in STEPS:
            print(f"{s.at}  {s.route or '':6} {week._label(s)}" + (f"   (known: {s.known})" if s.known else ""))
            for c in s.checks:
                print(f"            · {c.what}")
        return
    rows = run(args.model, args.budget_min * 60)
    sys.exit(1 if any(not r["ok"] and not r["known"] for r in rows) else 0)


if __name__ == "__main__":
    main()
