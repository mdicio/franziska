"""Router accuracy and calibration per model (CHANGELOG §3): is the router's p usable for "ask when unsure"?

On gemma-4-26B the router is right ~12/14 but says p=1.00 on its misses too, so the 0.6 threshold never fires.
This runs the real Router (AnyJev L0 on llama-server /completion logprobs) over a labelled set of messages, on
each model in mail_eval.SERVERS that has a GGUF, one server at a time (the bot's resident server is stopped and
started again after, like mail_eval). Per model:

- accuracy, and the misses with their p;
- calibration: mean p when right vs wrong, expected calibration error (5 bins), and how often the bot would have
  asked (p < min_p) on a miss vs on a hit;
- a temperature fit: the T that minimises log loss when the probabilities are sharpened/flattened as p^(1/T).
  T well above 1 means the distribution's order is fine but it is overconfident, and a per-model T in Router fixes
  "ask when unsure" without changing the model;
- seconds per decision, and errors (a LabelTokenError means the model's tokenizer splits a label: not compatible
  with the router's options as written).

    uv run --env-file .env python -m evals.router_eval                       # every GGUF model, ~10-15 min
    uv run --env-file .env python -m evals.router_eval --servers gemma4-26b-a4b,qwen3.6-35b-a3b
    uv run --env-file .env python -m evals.router_eval --resident            # the bot's running server only

Results: evals/runs/router-<time>/ (results.jsonl, summary.md).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from pathlib import Path
from datetime import datetime

from evals.mail_eval import GGUF, PORT, SERVERS, LlamaServer
from franziska import gpu, workflows
from franziska.core import MAIL_ROUTE
from franziska.config import ROOT, Settings
from franziska.router import Router

BUDGET_S = 20 * 60  # whole run; a model that would start past it is skipped

# (message, expected route, previous turn or None). Italian and English, typos, follow-ups, and the known-hard ones.
CASES: list[tuple[str, str, tuple[str, str] | None]] = [
    ("ricordami di pagare il bollo auto entro venerdì", "life", None),
    ("ricordmi di chiamre il comercialista domani mattina", "life", None),
    ("dentista giovedì alle 15:30", "life", None),
    ("cosa ho domani?", "life", None),
    ("sposta il dentista a venerdì stessa ora", "life", None),
    ("ricordati che il codice del cancello è 4471", "life", None),
    ("qual è il codice della cantina?", "life", None),
    ("fatto la carta d'identità", "life", None),
    ("aiutami a pianificare la prossima settimana", "life", None),
    ("smettila di ricordarmi il bollo", "life", None),
    ("I need to buy a birthday present for Luca by next Saturday", "life", None),
    ("what's on my calendar this weekend?", "life", None),
    ("ho speso 25 euro dal barbiere, in contanti", "money", None),
    ("quanto ho speso in contanti questa settimana?", "money", None),
    ("quanto ho speso al supermercato a settembre?", "money", None),
    ("devo pagare la rata del condominio di 410 euro entro il 1 dicembre", "money|life", None),  # both have add_bill
    ("quali bollette devo ancora pagare?", "money", None),
    ("how much did I save last month?", "money", None),
    ("metti un budget di 200 euro per i ristoranti", "money", None),
    # Investments: money, whatever the broker is called (the route's description names none, 2026-11-12).
    ("come sta andando il mio portafoglio?", "money", None),
    ("quanto valgono oggi i miei ETF?", "money", None),
    ("how much are my investments up this year?", "money", None),
    ("quante azioni ho su Fineco?", "money", None),
    ("e ad agosto?", "money", ("money", "quanto ho speso al supermercato a luglio?")),
    ("e giovedì?", "life", ("life", "cosa ho mercoledì?")),
    ("anzi, dopodomani", "life", ("life", "ricordami di chiamare la banca domani")),
    ("spiegami la differenza tra ETF e fondi comuni", "chat", None),
    ("what is the capital of Australia?", "chat", None),
    ("ciao, come va?", "chat", None),
    ("scrivimi una poesia sull'autunno", "chat", None),
    ("come si fa il ragù?", "chat", None),
    ("translate 'good morning' into German", "chat", None),
    ("cosa conviene di più, un mutuo fisso o variabile in generale?", "chat", None),
    ("ho ricevuto una mail da Luca? cosa dice?", "mail", None),
    ("rispondi a Sara che va bene giovedì", "mail", None),
    ("archivia le newsletter nella posta", "mail", None),
    ("trova il documento del contratto su Drive", "mail", None),
    ("cosa mi ha scritto l'amministratore?", "mail", None),
    ("did the airline send my boarding pass?", "mail", None),
    ("aggiungi una riga al foglio delle spese di casa su Google Sheets", "mail", None),
    # misses of evals/week.py and evals/memory.py (2026-10-29), and the workflows the bot also routes to
    ("dimentica quello che sai sul dentista", "life", None),
    ("cosa devo fare questo weekend?", "life", None),
    ("spiegami cos'è un ETF in due righe", "chat", None),
    ("BAR NERO SRL sull'estratto conto è il caffè sotto l'ufficio, mettilo nei bar", "money", None),
    ("ho pagato il bollo", "life", ("life", "smettila di ricordarmi il bollo, lo faccio domani sera alle 21")),
    ("categorizza le spese", "categorise", None),
    ("controlla le bollette arrivate per mail nell'ultimo mese", "mail_bills", None),
    ("mandami il report dei mercati di oggi", "markets", None),
    # the web route (2026-10-30: these went to chat, which made up "ci sono diverse notizie online")
    ("cerca il web per riferimenti a rivoluzioni oggi", "web", None),
    ("ci sono articoli che parlano di proteste oggi?", "web", None),
    ("a che ora chiude il Louvre la domenica?", "web", None),
    ("cerca su google le recensioni della trattoria da Enzo", "web", None),
    ("what's the latest news on the ECB?", "web|markets", None),
    ("usa google", "web", ("chat", "cerca il web per riferimenti a rivoluzioni oggi")),
    # the document library (2026-10-31): what a saved document says is life's (money's for a bank's own papers)
    ("cosa copre la mia polizza auto?", "life", None),
    ("entro quando posso disdire il contratto d'affitto?", "life", None),
    ("nel regolamento del condominio si possono tenere cani?", "life", None),
    ("cosa diceva il manuale della caldaia sull'errore F28?", "life", None),
]


def routes(with_mail: bool) -> dict[str, str]:
    """As the bot has them: the workflows, and the mail route when it is on."""
    return {**{w.name: w.description for w in workflows.ALL}, **({"mail": MAIL_ROUTE} if with_mail else {})}


def evaluate(router: Router, label: str, min_p: float, log=print) -> list[dict]:
    rows = []
    for text, want, previous in CASES:
        t0 = time.time()
        error = ""
        try:
            r = router.route(text, previous)
        except Exception as e:  # noqa: BLE001 - route() already catches; this is a crash in the eval itself
            r, error = None, f"{type(e).__name__}: {e}"
        row = {"model": label, "text": text, "want": want, "previous": previous, "seconds": round(time.time() - t0, 2),
               "got": r.name if r else None, "p": round(r.p, 4) if r else None,
               "probs": {k: round(v, 4) for k, v in r.probs.items()} if r else {}, "error": error or ("" if r else
                                                                                                    "no route")}
        row["ok"] = row["got"] in want.split("|")
        row["asks"] = r is not None and r.p < min_p
        rows.append(row)
        mark = "✅" if row["ok"] else "❌"
        log(f"  {mark} {row['seconds']:4.1f}s p={row['p'] if r else '-':<6} {row['got'] or '-':6} (want {want:5}) {text}")
    return rows


def fit_temperature(rows: list[dict]) -> float:
    """T minimising the log loss of the expected route under p^(1/T), normalised; grid over 0.5–20."""
    usable = [r for r in rows if r["probs"]]
    if not usable:
        return float("nan")

    def loss(t: float) -> float:
        total = 0.0
        for r in usable:
            scaled = {k: max(v, 1e-12) ** (1 / t) for k, v in r["probs"].items()}
            z = sum(scaled.values())
            right = sum(scaled.get(w, 0.0) for w in r["want"].split("|"))  # 'money|life': either is right
            total -= math.log(max(right / z, 1e-12))
        return total / len(usable)

    grid = [0.5 + 0.25 * i for i in range(79)]  # 0.5 … 20
    return min(grid, key=loss)


def ece(rows: list[dict], bins: int = 5) -> float:
    usable = [r for r in rows if r["p"] is not None]
    if not usable:
        return float("nan")
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        group = [r for r in usable if lo < r["p"] <= hi or (b == 0 and r["p"] == 0)]
        if group:
            total += len(group) / len(usable) * abs(statistics.mean(r["ok"] for r in group)
                                                    - statistics.mean(r["p"] for r in group))
    return total


def summary(rows: list[dict], min_p: float) -> str:
    lines = ["| model | right | mean p right | mean p wrong | ECE | asks on miss | asks on hit | fitted T | "
             "s/decision | errors |", "|---|---|---|---|---|---|---|---|---|---|"]
    for model in dict.fromkeys(r["model"] for r in rows):
        rs = [r for r in rows if r["model"] == model]
        hits, misses = [r for r in rs if r["ok"]], [r for r in rs if not r["ok"] and r["p"] is not None]

        def mean_p(group):
            return f"{statistics.mean(r['p'] for r in group):.2f}" if group else "–"

        lines.append(f"| {model} | {len(hits)}/{len(rs)} | {mean_p(hits)} | {mean_p(misses)} | {ece(rs):.2f} | "
                     f"{sum(r['asks'] for r in misses)}/{len(misses)} | {sum(r['asks'] for r in hits)}/{len(hits)} | "
                     f"{fit_temperature(rs):.2f} | {statistics.median(r['seconds'] for r in rs):.2f} | "
                     f"{sum(bool(r['error']) for r in rs)} |")
    lines += ["", f"min_p = {min_p}. Misses:"]
    lines += [f"- {r['model']}: “{r['text']}” → {r['got']} p={r['p']} (want {r['want']}){' ' + r['error'] if r['error'] else ''}"
              for r in rows if not r["ok"]]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    gguf = [k for k, v in SERVERS.items() if "gguf" in v]
    p.add_argument("--servers", default=",".join(gguf), help=f"comma-separated, from {gguf}")
    p.add_argument("--resident", action="store_true", help="only the bot's running server (FRANZISKA_BASE_URL)")
    p.add_argument("--no-mail", action="store_true", help="without the mail route (as when gog is not signed in)")
    p.add_argument("--budget-min", type=float, default=BUDGET_S / 60)
    p.add_argument("--run-dir", type=Path, help="write here; if it has results, resume: skip models already in it")
    args = p.parse_args()
    base = Settings.from_env()
    run_dir = args.run_dir or ROOT / "evals" / "runs" / f"router-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prev = run_dir / "results.jsonl"  # resuming: a model with rows here is done (its rows are written together)
    rows = [json.loads(line) for line in (prev.read_text().splitlines() if prev.exists() else []) if line.strip()]
    started = time.time()

    def record(new: list[dict]) -> None:
        rows.extend(new)
        with (run_dir / "results.jsonl").open("a") as f:
            for r in new:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if args.resident:
        with gpu.hold("router eval"):
            router = Router(base.model, min_p=base.router_min_p, vocab=base.router_vocab,
                            routes=routes(not args.no_mail), server_url=base.server_url)
            print(f"{base.model} (resident)")
            record(evaluate(router, base.model, base.router_min_p))
    else:
        for name in args.servers.split(","):
            if any(r["model"] == name for r in rows):
                print(f"{name}: already in {run_dir}")
                continue
            if time.time() - started > args.budget_min * 60:
                print(f"skip {name}: over the time budget")
                continue
            print(name)
            with gpu.hold("router eval"), LlamaServer(name, SERVERS[name], run_dir):
                router = Router(name, min_p=base.router_min_p, vocab=str(GGUF / SERVERS[name]["gguf"]),
                                routes=routes(not args.no_mail), server_url=f"http://localhost:{PORT}")
                record(evaluate(router, name, base.router_min_p))
    text = summary(rows, base.router_min_p)
    (run_dir / "summary.md").write_text(text + "\n")
    print("\n" + text + f"\n\n{run_dir}")


if __name__ == "__main__":
    main()
