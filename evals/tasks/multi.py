"""Multi-agent tasks: a main agent that hands self-contained requests to sub-agents, each its own tool loop."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from evals.tasks.base import Task, _score
from evals.tasks.home import (HOME_ORCH, LIFE, LIFE_SYS, MAIL, MONEY, MONEY_SYS, Home, amount_in, said)
from evals.tasks.worlds import ORCH, Office


def judge_trip(ws, final: str, o: Office) -> tuple[float, str]:
    a = o.appts
    return _score({
        "airport block 15-18": any(("airport" in t.lower() or "travel" in t.lower()) and s == "2026-11-05T15:00" and e == "2026-11-05T18:00"
                                   for t, s, e in a.values()),
        "project review to Fri 16:00": a.get("a6", ("",) * 3)[1] == "2026-11-06T16:00",
        "Marco call untouched": a.get("a7") == o.start_appts["a7"],
        "pack task due Thu": any("pack" in t["title"].lower() and t["due"] == "2026-11-05" for t in o.tasks.values()),
        "note with flight number": any("lh1234" in n.lower().replace(" ", "") for n in o.notes),
        "nothing deleted": not o.cancelled,
    })


def judge_budget(ws, final: str, o: Office) -> tuple[float, str]:
    f = final.replace(",", ".")
    new_tasks = [t for i, t in o.tasks.items() if i != "t1"]
    return _score({
        "restaurants total 169.60 stated": bool(re.search(r"169\.6", f)),
        "groceries total 312.40 stated": bool(re.search(r"312\.4", f)),
        "restaurant review task due Mon": any("restaurant" in t["title"].lower() and t["due"] == "2026-11-09" for t in new_tasks),
        "no groceries task": not any("grocer" in t["title"].lower() for t in new_tasks),
        "note with the total": any("169" in n for n in o.notes),
    })


def judge_taxslot(ws, final: str, o: Office) -> tuple[float, str]:
    tax = [v for v in o.appts.values() if "tax" in v[0].lower()]
    ok_slot = len(tax) == 1 and tax[0][1] in ("2026-11-06T11:00", "2026-11-06T15:30") and \
        datetime.fromisoformat(tax[0][2]) - datetime.fromisoformat(tax[0][1]) == timedelta(minutes=90)
    say = {"2026-11-06T11:00": ("11:00", "11.00", "11 am", "11am"), "2026-11-06T15:30": ("15:30", "15.30", "3:30", "3.30")}
    return _score({
        "90 min slot in a free window": ok_slot,
        "reply names the time": ok_slot and any(x in final.lower() for x in say[tax[0][1]]),
        "documents task due Thu": any("document" in t["title"].lower() and t["due"] == "2026-11-05" for t in o.tasks.values()),
        "other appointments untouched": all(o.appts.get(i) == v for i, v in o.start_appts.items()) and not o.cancelled,
    })

OLD = [
    Task("ma_trip", "multi", "orchestrator + calendar, tasks, notes agents: block time, resolve the overlap it causes, task, note",
         "I fly to Berlin tomorrow at 18:00, flight LH1234. Block the three hours before the flight in my calendar as "
         "'Airport and travel'. If anything of mine overlaps with that block, move it to the same time on Friday. Also add a "
         "task due tomorrow to pack and check in online, and save a note with the flight number.",
         judge=judge_trip, multi=True, system=ORCH),
    Task("ma_budget", "multi", "orchestrator + finance, tasks, notes agents: sum two categories, act only on the one over its limit",
         "How much did I spend on restaurants in October, and how much on groceries? If restaurants were over 150 euro, add a "
         "task due next Monday to review my restaurant budget and save a note with the total; do the same for groceries if "
         "they were over 400. Tell me both totals.",
         judge=judge_budget, multi=True, system=ORCH),
    Task("ma_taxslot", "multi", "orchestrator + calendar, tasks agents: find a free 90-min slot, survive a rejected booking",
         "Book a 90-minute 'Tax advisor' appointment on Friday, any time between 10:00 and 17:00 when I'm completely free, "
         "and tell me which time you picked. Also add a task due tomorrow to gather the tax documents.",
         judge=judge_taxslot, multi=True, system=ORCH),
]


# ---- 2026-11 tasks on Home: the bot's own assistants (life, money, mail) as sub-agents ---------------------------------
# Two shapes, as in the bot: an orchestrator that can only delegate (plan.py: several things in one message), and a
# peer that does its own part and asks another assistant for the rest (delegation.py: `ask`, money → life, life → mail).
def j_plumber(ws, final, o: Home):
    a = [v for v in o.new_appts().values() if said(v[0], "idraulic", "ferretti", "lavandino")]
    return _score({
        "appointment Thu 12 09:00": len(a) == 1 and a[0][1] == "2026-11-12T09:00",
        "reply drafted to the plumber": [d["reply_to"] for d in o.drafts] == ["m1"],
        "nothing else changed": all(c == "drafts" or c[5:] in o.new_appts() for c in o.changed()),
    })


def j_condo_mail(ws, final, o: Home):
    b = [b for b in o.bills if said(b["payee"] + b["match"], "condomin", "la rocca", "larocca")]
    return _score({"bill tracked": len(b) == 1, "432 € due 30 Nov": bool(b) and b[0]["amount"] == 432 and b[0]["due"] == "2026-11-30",
                   "no drafts or archiving": not o.wrote("draft_reply", "archive")})


def j_milan(ws, final, o: Home):
    a = o.appts
    blk = [v for i, v in o.new_appts().items() if v[1][:10] == "2026-11-20"]
    return _score({
        "block Fri 20 from 07:40 (or earlier) to 13:00": len(blk) == 1 and "2026-11-20T07:00" <= blk[0][1] <= "2026-11-20T07:40"
                                                         and blk[0][2] == "2026-11-20T13:00",
        "dental cleaning to Mon 23 10:00": a["c13"][1:] == ["2026-11-23T10:00", "2026-11-23T10:45"],
        "supplier call to Mon 23 12:00": a["c14"][1:] == ["2026-11-23T12:00", "2026-11-23T12:30"],
        "booking code saved": any("XK7Q2M" in f"{s} {f}".upper() for s, f in o.notes.values()),
        "print reminder Thu 21:00": any(t["remind"] == "2026-11-19T21:00" and said(t["title"] + t["notes"], "stamp", "print", "bigliett")
                                        for t in o.new_tasks().values()),
        "other appointments untouched": all(a.get(i) == list(v) for i, v in Home().appts.items() if i not in ("c13", "c14")),
    })


def j_month_close(ws, final, o: Home):
    t = list(o.new_tasks().values())
    return _score({
        "total 988.66": amount_in(final, 988.66),
        "top two: groceries, restaurants": said(final, "spesa", "supermerc", "aliment", "grocer") and said(final, "ristorant", "restaurant"),
        "review task Mon 16 for restaurants": any(x["due"][:10] == "2026-11-16" and said(x["title"] + x["notes"], "ristorant", "restaurant")
                                                  for x in t),
        "no task for the budgets not exceeded": not any(said(x["title"] + x["notes"], "spesa", "grocer", "aliment", "shopping") for x in t),
    })


def j_giulia(ws, final, o: Home):
    return _score({
        "dinner to Sat 20:00-23:00": o.appts["c4"][1:] == ["2026-11-14T20:00", "2026-11-14T23:00"],
        "allergy saved": any(said(f"{s} {f}", "noci", "nut") and said(f"{s} {f}", "giulia") for s, f in o.notes.values()),
        "nothing else changed": all(c == "appt c4" or c.startswith("note ") and int(c[5:]) > 100 for c in o.changed()),
    })


def j_gym(ws, final, o: Home):
    return _score({"45 € a month": amount_in(final, 45), "Wed and Fri": said(final, "mercoled", "wednesday", "11")
                   and said(final, "venerd", "friday", "13"), "no writes": not o.writes})


def j_flaky(ws, final, o: Home):
    a = [v for v in o.new_appts().values() if said(v[0], "commercialist")]
    return _score({"booked once, Mon 16 15:00-16:00": len(a) == 1 and a[0][1:] == ["2026-11-16T15:00", "2026-11-16T16:00"],
                   "no duplicates": len(o.new_appts()) == 1})


def j_bollo(ws, final, o: Home):
    rem = [t for t in o.tasks.values() if said(t["title"], "bollo") and not t["done"] and t["remind"][:10] == "2026-11-27"]
    return _score({"last year's 187.42": amount_in(final, 187.42), "reminder on 27 Nov": len(rem) >= 1,
                   "deadline kept": o.tasks[2]["due"] == "2026-11-30"})


def j_plumber_peer(ws, final, o: Home):
    a = [v for v in o.new_appts().values() if said(v[0], "idraulic", "ferretti", "lavandino")]
    return _score({"appointment Thu 12 09:00": len(a) == 1 and a[0][1] == "2026-11-12T09:00", "no drafts": not o.drafts,
                   "nothing else changed": len(o.changed()) == 1})


def j_laura(ws, final, o: Home):
    d = [d for d in o.drafts if d["reply_to"] == "m3"]
    return _score({"reply to Laura drafted": len(d) == 1 and len(o.drafts) == 1,
                   "it says Thursday": bool(d) and said(d[0]["body"], "giovedi", "thursday", "12"),
                   "report task due Thu": o.tasks[5]["due"][:10] == "2026-11-12" and not o.tasks[5]["done"],
                   "no duplicate task": not [t for t in o.new_tasks().values() if said(t["title"], "report")]})


def _ref(*steps, reply="Fatto."):
    """A reference for a multi task: the writes the sub-agents should end up making."""
    def run(o):
        for name, a in steps:
            out = o.call(name, a)
            assert not out.startswith("error"), (name, a, out)
        return reply
    return run


def _ref_flaky(o):
    assert o.call("add_appointment", {"title": "Commercialista", "starts_at": "2026-11-16T15:00"}).startswith("error")
    return _ref(("add_appointment", {"title": "Commercialista", "starts_at": "2026-11-16T15:00"}))(o)


HM = dict(world=Home, multi=True, system=HOME_ORCH)
# Peers (ask) run on the bot's prompts as they are: the bot says when to ask only in the ask tool's description.
NEW = [
    Task("mm_giulia", "multi", "two small requests for one assistant, context passed on", "Giulia mi ha chiesto di spostare "
         "la cena a sabato, stessa ora. E segnati che è allergica alle noci.", judge=j_giulia, agents={"life": LIFE},
         tags=("life", "notes", "it"), **HM,
         reference=_ref(("move_appointment", {"item_id": "c4", "starts_at": "2026-11-14T20:00"}),
                        ("remember", {"subject": "Giulia", "fact": "allergica alle noci"}))),
    Task("mm_gym", "multi", "read-only question across money and life: no writes", "quanto pago al mese di palestra e quando "
         "ci vado questa settimana?", judge=j_gym, agents={"life": LIFE, "money": MONEY}, tags=("life", "money", "it", "read"),
         **HM, reference=lambda o: "45,00 € al mese (FitLife). Questa settimana: mercoledì 11 e venerdì 13 alle 7:30."),
    Task("mm_flaky", "multi", "the calendar fails once: retry, book exactly once", "prenotami il commercialista lunedì 16 alle 15 per un'ora",
         judge=j_flaky, agents={"life": LIFE}, tags=("life", "it", "retry"), world=lambda: Home(flaky={"add_appointment": 1}),
         multi=True, system=HOME_ORCH, reference=_ref_flaky),
    Task("mm_plumber", "multi", "mail → calendar → reply: read a proposal, book it, confirm", "l'idraulico mi ha scritto per "
         "venire a sistemare il lavandino: mettilo in agenda quando propone lui e rispondigli che va bene",
         judge=j_plumber, agents={"life": LIFE, "mail": MAIL}, tags=("mail", "life", "it"), **HM,
         reference=_ref(("add_appointment", {"title": "Idraulico (lavandino)", "starts_at": "2026-11-12T09:00"}),
                        ("draft_reply", {"message_id": "m1", "body": "Buongiorno, giovedì alle 9 va benissimo. Grazie!"}))),
    Task("mm_condo_mail", "multi", "a bill found in the mail becomes a tracked bill", "c'è una mail del condominio con una "
         "rata da pagare: tienimela d'occhio come bolletta", judge=j_condo_mail, agents={"money": MONEY, "mail": MAIL},
         tags=("mail", "money", "it"), **HM,
         reference=_ref(("add_bill", {"payee": "Condominio Via Po", "amount_eur": 432, "due_on": "2026-11-30", "match": "Rata Q4"}))),
    Task("mm_laura", "multi", "reply to a mail and move the matching task (no duplicate)", "Laura mi ha scritto per il report: "
         "rispondile che glielo mando giovedì e sposta la scadenza del task del report a giovedì", judge=j_laura,
         agents={"life": LIFE, "mail": MAIL}, tags=("mail", "life", "it"), **HM,
         reference=_ref(("draft_reply", {"message_id": "m3", "body": "Ciao Laura, te lo mando giovedì!"}),
                        ("edit_task", {"item_id": 5, "due_at": "2026-11-12"}))),
    Task("mm_month_close", "multi", "figures from money, a task from life only where a budget was exceeded",
         "facciamo il punto su ottobre: quanto ho speso in tutto, quali sono le due categorie più alte, e se ho sforato "
         "un budget aggiungimi un task per lunedì per rivederlo", judge=j_month_close, agents={"life": LIFE, "money": MONEY},
         tags=("money", "life", "it"), **HM,
         reference=_ref(("add_task", {"title": "Rivedere il budget ristoranti", "due_at": "2026-11-16"}),
                        reply="Ottobre: 988,66 €. Più alte: spesa 343,00 e ristoranti 188,00 (sopra il budget di 150).")),
    Task("mm_milan_trip", "multi", "ticket from mail, block + two moves, a note and a reminder (6 writes, 2 agents)",
         "venerdì 20 vado a Milano col treno delle 7:40, il biglietto è nelle mail. Bloccami in agenda dalla partenza "
         "fino alle 13, sposta al lunedì dopo, stessa ora, quello che c'è in quella fascia, salva il codice di "
         "prenotazione nelle note e ricordami giovedì sera alle 21 di stampare il biglietto.", judge=j_milan,
         agents={"life": LIFE, "mail": MAIL}, tags=("mail", "life", "notes", "it"), **HM,
         reference=_ref(("add_appointment", {"title": "Treno per Milano", "starts_at": "2026-11-20T07:40", "ends_at": "2026-11-20T13:00"}),
                        ("move_appointment", {"item_id": "c13", "starts_at": "2026-11-23T10:00"}),
                        ("move_appointment", {"item_id": "c14", "starts_at": "2026-11-23T12:00"}),
                        ("remember", {"subject": "biglietto Milano 20/11", "fact": "PNR XK7Q2M, Frecciarossa 9512"}),
                        ("add_task", {"title": "Stampare il biglietto del treno", "remind_at": "2026-11-19T21:00"}))),
    # peers: the money assistant does its part and asks life for the rest; life asks mail
    Task("ask_bollo", "multi", "peer: money answers from the ledger, asks life for a reminder 3 days before the deadline",
         "quanto ho pagato di bollo auto l'anno scorso? E fammelo ricordare quest'anno tre giorni prima della scadenza "
         "che ho già in agenda.", judge=j_bollo, world=Home, tools=MONEY, agents={"life": LIFE}, call_tool="ask",
         system=MONEY_SYS, tags=("money", "life", "it", "ask-peer"),
         reference=_ref(("edit_task", {"item_id": 2, "remind_at": "2026-11-27T09:00"}),
                        reply="L'anno scorso 187,42 €. Promemoria il 27 novembre, scadenza il 30.")),
    Task("ask_plumber", "multi", "peer: life asks mail when the plumber comes, then books it itself",
         "l'idraulico mi ha scritto quando passa? mettimelo in agenda", judge=j_plumber_peer, world=Home, tools=LIFE,
         agents={"mail": MAIL}, call_tool="ask", system=LIFE_SYS, tags=("life", "mail", "it", "ask-peer"),
         reference=_ref(("add_appointment", {"title": "Idraulico Ferretti", "starts_at": "2026-11-12T09:00"}))),
]
