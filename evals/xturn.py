"""References across turns and assistants (ROADMAP 1), on the real bot and model. Run on demand.

The same harness as evals/week.py (fake Telegram, scratch stores, simulated clock), with invented data: a bill sent
as a document that money reads and life then has to act on ('ricordami di pagarla'), a figure money gives that a
later life turn must carry ('per quella cifra'), a planned message whose last step needs money's result, and past
conversations before and after an older chat turn exists. Judged on the stores and the turn log, plus a few reply
patterns.

It runs on the configured routes, FRANZISKA_CLAUDE_ROUTES included: the cross-turn misses depend on where life runs.
With life on Claude, two more checks apply to the follow-up of money's bill: life's own history holds no amount,
and the turn is logged as seen as money (Core._hidden_follow_up). With life local they pass as not applicable.

    uv run --env-file .env python -m evals.xturn              # ~4 min on the 26B (10 steps), budget 10 min
    uv run --env-file .env python -m evals.xturn --list       # the steps, without running anything

Results: evals/runs/xturn-<time>/ (steps.jsonl, summary.md). Holds the GPU like the week eval.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta

from evals import week
from evals.week import Check, Step, bill, doc, reply, say, task
from franziska.finance import ledger

BUDGET_S = 10 * 60

BILL = """ACME LUCE S.p.A. - Bolletta energia elettrica
Cliente: Mario Rossi, via dei Tigli 8, 20100 Milano
Codice cliente: 000123456
Periodo di fornitura: settembre - ottobre 2026
Consumo: 212 kWh
Totale da pagare: 84,20 EUR
Scadenza: 20/11/2026
Pagamento con bollettino o addebito diretto SEPA.
""".encode()


def called(tool: str) -> Check:
    return Check(f"{tool} called", lambda w, o: any(c.name == tool and c.ok for g in o.guards for c in g.calls))


def _life_off_machine(w: week.World) -> bool:
    models = w.bot.models
    return models.where(models.for_route("life")) != "local"


def no_amount_in_life_history() -> Check:
    """With life on Claude, what it saw of money's bill must not stay in the history its own model reads later."""
    def fn(w, o):
        return not _life_off_machine(w) or not re.search(
            r"84[,.]20", str(w.chat.context.chat_data.get(("history", "life"), [])))
    return Check("life's history has no amount (life on Claude only)", fn)


def logged_seen_as_money() -> Check:
    sql = "SELECT seen_as FROM turns WHERE route = 'life' AND parent IS NULL ORDER BY id DESC LIMIT 1"

    def fn(w, o):
        return not _life_off_machine(w) or (w.bot.turns.con.execute(sql).fetchone() or [None])[0] == "money"
    return Check("logged seen as money (life on Claude only)", fn)


def task_has(pattern: str, figure: str) -> Check:
    """The latest task matching `pattern` names `figure` in its title or notes."""
    return Check("the task carries the figure", lambda w, o: (r := w.item(pattern)) is not None and bool(
        re.search(figure, f"{r['title']} {r.get('notes') or ''}")))


def seed_boat(w: week.World) -> None:
    """An older chat turn on Telegram about the boat trip: the second question must find it."""
    w.bot.turns.add(week.USER, "chat", "a luglio facciamo la gita in barca a Ponza con Giulia, serve lo skipper?",
                    "Per una barca a vela sopra i 10 m serve la patente nautica o uno skipper.",
                    at=datetime(2026, 9, 6, 10, 0))


STEPS: list[Step] = [
    # a bill as a document: money reads it
    doc("mon 09:00", "bolletta_acme.txt", BILL, "money", reply(r"84[,.]20"), mime="text/plain"),
    # life acts on it, without being told what 'la' is
    say("mon 09:03", "ok, ricordami di pagarla 3 giorni prima della scadenza", "life",
        task("bollett|acme|luce|pag", remind_at="2026-11-17"), no_amount_in_life_history(), logged_seen_as_money()),
    # the figure comes from the document money read: backed, not invented
    say("mon 09:05", "quanto era l'importo esatto?", "money", reply(r"84[,.]20")),
    # hours later, outside the follow-up window: fine locally; on Claude life can't see the bill (accepted)
    say("mon 16:00", "segna quella bolletta della luce tra le cose da pagare", "life", bill("acme|luce", 8420),
        known="life on Claude can't see money's bill past the follow-up window: privacy table, accepted 2026-11-12"),
    # money filters by merchant, not by the whole category (146 of 268 € in food & drink)
    say("tue 10:00", "quanto ho speso al supermercato a ottobre?", "money", reply(r"146"),
        known="model: sums the category though TX_VIEW_DOC names merchant and says to filter it with LIKE"),
    # life carries money's figure into the task (146, or the 268 money may have said)
    say("tue 10:02", "ok, ricordami venerdì di rivedere il budget per quella cifra", "life",
        task_has("budget", r"146|268"),
        known="model: the thread shows money's figure as the turn just before; the local model leaves it out"),
    # a plan: a life step, a money step, and a life step that needs money's result (122 € > 100)
    say("wed 09:00", "sposta il task 'comprare il pane' a domani, e quanto ho speso in ristoranti a ottobre? se più "
        "di 100 euro ricordami sabato di cucinare di più", None,
        task("pane", due_at="2026-11-05"), task("cucin", due_at="2026-11-07")),
    # past conversations: nothing yet, then an older Telegram turn the retry must find
    say("thu 10:00", "abbiamo mai parlato della gita in barca a Ponza?", "life",
        called("past_conversations"), reply(r"giulia|skipper", present=False)),
    week.job("thu 10:01", "seed_barca", run=seed_boat),
    say("thu 10:03", "sono sicuro che ne abbiamo parlato su telegram, la gita in barca a ponza, controlla di nuovo",
        "life", called("past_conversations"), reply(r"giulia|skipper|settembre")),
]


def seed(world: week.World) -> None:
    """October's food & drink (146 € at the supermarket, 122 € in restaurants) and the task the plan moves."""
    spent = [("SUPERMERCATO ESSELUNGA", 5840, 3), ("SUPERMERCATO ESSELUNGA", 4210, 12),
             ("SUPERMERCATO ESSELUNGA", 4550, 24), ("TRATTORIA DA PINO", 7000, 10), ("PIZZERIA VESUVIO", 5200, 18)]
    with world.bot.money._con() as con:
        ledger.insert(con, [ledger.Transaction("intesa", date(2026, 10, day), None, -cents,
                                               f"Pagamento POS {who} MILANO", "", "eval seed",
                                               category="food & drink", merchant=who)
                            for who, cents, day in spent])
    world.bot.life.add_task("comprare il pane", due_at=(week.MONDAY + timedelta(days=2)).date())


def run(model: str = "", budget_s: float = BUDGET_S, log=print) -> list[dict]:
    return week.run(model=model, log=log, steps=STEPS, name="xturn", budget_s=budget_s, setup=seed)


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
