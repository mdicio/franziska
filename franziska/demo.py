"""`python -m franziska demo`: the web UI on a made-up person's data, so you can look around before connecting anything.

A scratch data directory gets three months of bank movements (a Revolut-format statement, imported by the real
importer with its checks), spending groups and budgets, a bill that is also a reminder, a few tasks and appointments,
notes, and the documents of the library eval (a car insurance policy, a lease, a condominium statement). Then the web
UI starts on it. Nothing of yours is read: no Telegram, no Google, no mail, and the PC route is off.

Chat needs the local model (`python -m franziska doctor`); the agenda, the money pages and the documents work without.
"""

from __future__ import annotations

import csv
import dataclasses
import random
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from franziska.config import Settings

# (payee as the statement writes it, spending group, typical amount in €, times a month)
SHOPS = [("Esselunga", "food & drink", 46.0, 4), ("Bar Centrale", "food & drink", 3.2, 8),
         ("Deliveroo", "food & drink", 24.0, 2), ("ATM Milano", "transport", 2.2, 6), ("Q8", "transport", 55.0, 1),
         ("Zara", "shopping", 39.9, 1), ("Amazon", "shopping", 27.0, 2), ("Farmacia Duomo", "health & care", 14.5, 1),
         ("Cinema Anteo", "leisure", 9.5, 1)]
MONTHLY = [("Netflix", "subscriptions", 13.99, 3), ("Spotify", "subscriptions", 10.99, 9),
           ("Iliad", "home & bills", 9.99, 12), ("Enel Energia", "home & bills", 61.40, 18)]
RENT = ("To Immobiliare Brera", "home & bills", 950.0, 1)
SALARY = ("Payment from ACME Italia SRL", 2380.0, 27)


def statement(path: Path, today: date, rng: random.Random) -> None:
    """Three months of a made-up Revolut current account, with a running balance that adds up."""
    from franziska.finance.importer import REVOLUT_HEADER

    rows = []
    start = (today.replace(day=1) - timedelta(days=62)).replace(day=1)
    day = start
    while day <= today:
        for name, _, amount, per_month in SHOPS:
            if rng.random() < per_month / 30:
                rows.append((day, "Card Payment", name, -round(amount * rng.uniform(0.6, 1.5), 2)))
        for name, _, amount, dom in MONTHLY:
            if day.day == dom:
                rows.append((day, "Card Payment", name, -amount))
        if day.day == RENT[3]:
            rows.append((day, "Transfer", RENT[0], -RENT[2]))
        if day.day == SALARY[2]:
            rows.append((day, "Transfer", SALARY[0], SALARY[1]))
        day += timedelta(days=1)
    balance = 1840.0
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(REVOLUT_HEADER)
        for i, (d, kind, desc, amount) in enumerate(sorted(rows, key=lambda r: r[0])):
            balance = round(balance + amount, 2)
            at = datetime.combine(d, datetime.min.time()) + timedelta(hours=9, minutes=i % 50)
            w.writerow([kind, "Current", at.isoformat(sep=" "), at.isoformat(sep=" "), desc, f"{amount:.2f}", "0.00",
                        "EUR", "COMPLETED", f"{balance:.2f}"])


def seed(core, today: datetime) -> list[str]:
    """Fill a fresh Core's stores; returns what was done, one line each."""
    rng = random.Random(7)
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "account-statement_demo.csv"
        statement(path, today.date(), rng)
        out.append(core.money.import_file(path).splitlines()[0])
    for name, group, *_ in SHOPS + MONTHLY + [RENT]:
        payee = name.removeprefix("To ").upper()
        try:
            core.money.set_payee_category(payee, group)
        except Exception:  # a payee the import named differently: left for /categorise
            pass
    for group, eur in (("food & drink", 380), ("shopping", 120), ("leisure", 60)):
        core.money.set_budget(group, eur)
    out.append(core.money.add_bill("Condominio Via Po", 410.0, (today + timedelta(days=12)).date(), "Rata"))
    life = core.life
    at = lambda days, h, m=0: (today + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)  # noqa
    out.append(life.add_appointment("Dentista", at(2, 15, 30), location="Studio Bianchi, via Torino 12"))
    out.append(life.add_appointment("Cena con Sara", at(4, 20, 30), location="Trattoria Da Mario"))
    out.append(life.add_task("Rinnovare la carta d'identità", due_at=(today + timedelta(days=9)).date(),
                             priority="normal"))
    out.append(life.add_task("Chiamare l'idraulico per il lavandino", remind_at=at(1, 10), priority="high"))
    out.append(life.add_task("Regalo di compleanno per Luca", due_at=(today + timedelta(days=6)).date(),
                             priority="low"))
    out.append(life.remember("codice portone", "Il codice del portone è 1984B."))
    out.append(life.remember("misure", "Scarpe 43, camicie M."))
    from evals.library_corpus import DOCS

    for title, pages in DOCS.items():
        core.library.add(title, "\f".join(pages), "demo", "personal", kept=True)
    out.append(f"{len(DOCS)} documents in the library")
    return out


def main(port: int = 8766) -> int:
    import pydantic_ai
    import uvicorn

    from franziska import obs
    from franziska.core import Core
    from franziska.web.app import create_app

    pydantic_ai.BANNER_ENABLED = False
    where = Path(tempfile.mkdtemp(prefix="franziska-demo-"))
    base = Settings.from_env()
    settings = dataclasses.replace(
        base, data_dir=where, telegram_token="", allowed_ids=frozenset(), google=False, gog_account="", pc=False,
        code_repos={}, voice_model="", market_at="", market_mail=False, cloud_model="", cloud_routes=frozenset(),
        claude_model="", claude_routes=frozenset(), desktop_mac="", web_host="127.0.0.1", web_port=port,
        web_token="", backup_dir=where / "backup")
    obs.setup("franziska-demo", settings.traces_db, "")
    core = Core(settings)
    print(f"Demo data in {where} (a scratch folder; delete it when done):")
    for line in seed(core, core.life.now()):
        print("  " + line.splitlines()[0][:110], flush=True)
    print(f"\nOpen http://127.0.0.1:{port}  ·  try: 'quanto ho speso questo mese?', 'cosa ho domani?', "
          "'cosa dice il contratto d'affitto sul deposito?'  ·  Ctrl+C to stop", flush=True)
    uvicorn.run(create_app(settings, core), host="127.0.0.1", port=port, log_level="warning")
    return 0
