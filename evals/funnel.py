"""Model funnel: every candidate through cheap stages first, the expensive ones only for models still in the race.
One command, one table, rerunnable: results are kept per (model, stage) in the funnel dir, so adding a model
later runs only that model, and the baseline is measured once.

Stages (each is an existing eval, run as a subprocess on one model at a time):
  speed    ~1 min   load time, VRAM, RAM, prompt and generation tok/s on a ~2.7k-token mail prompt
  router   ~2 min   router_eval: 54 labelled messages, accuracy and seconds per decision
  smoke    ~10 min  the tasks that separated models before: bench ttl_cache, calendar_move, ambiguous_booking,
                    ma_taxslot + mail triage, agenda_sweep, obey_mail, which_luca, luca_confirm (parse_duration left
                    on 2026-11-02: at a chat's time limit it measured the budget, every main model ran out thinking)
  simple   ~6 min   small models only: bench slugify, calendar_move, ambiguous_booking + mail delivery,
                    flight_code, iliad_amount, not_found, gym_task, sara_en (can it handle a plain turn alone?)
  full     ~1.5 h   --full only: mail eval ×3 (all scenarios) + the whole bench
  wide     ~1-2 h   --wide only: every task the model hasn't settled yet (evals.plan): main models every bench task
                    (900 s each) and every mail scenario; small models the easy tier without code and the single-step
                    mail, at a chat's 240 s. --wide-set picks another set (wide-main-nocode). A pair finished in any
                    earlier run within the same limits isn't run again.

No stage caps tokens per answer (2026-11-02): the server's context window is the limit, the time limits stay.

Gates only decide whether the next, more expensive stage runs; the table says why a model stopped, and
--no-gates runs everything. They are signal, not verdicts:
  speed:  the server loads, and generation >= 15 tok/s (a 1k-token thinking turn in about a minute)
  router: small models need >= 48/54 to be worth testing as a router; main models aren't gated here
  smoke:  full and wide runs only for main models within 2 checks of the baseline's smoke score

    uv run --env-file .env python -m evals.funnel                                   # every model in CANDIDATES
    uv run --env-file .env python -m evals.funnel --models gemma4-26b-qat,lfm2.5-24b-a2b
    uv run --env-file .env python -m evals.funnel --resume evals/runs/funnel-20261101-1700 --models qwen3.5-4b
    uv run --env-file .env python -m evals.funnel --resume evals/runs/funnel-... --full   # survivors' full runs

The bot's model server is stopped once for the whole funnel and started again at the end (also on Ctrl-C).
Results: evals/runs/funnel-<time>/ (stages.jsonl, summary.md; each stage's own run dir is linked in stages.jsonl).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from evals import plan as planner
from evals.mail_eval import PORT, SERVERS, LlamaServer, _unit, free_gpu, vram_used
from evals.mailbox import seed

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evals" / "runs"
BASELINE = "gemma4-26b-a4b"

# name in mail_eval.SERVERS → role. 'main': could replace gemma; 'small': router / simple turns next to it;
# 'speed': a placement or speculative variant of a model tested elsewhere (same output), speed stage only.
CANDIDATES: dict[str, str] = {
    "gemma4-26b-a4b": "main",
    "gemma4-26b-qat": "main",
    "gemma4-26b-dflash": "speed",
    "gemma4-26b-qat-dflash": "speed",
    "qwen3.6-35b-a3b": "main",
    "lfm2.5-24b-a2b": "main",
    "ornith1.5-35b-a3b": "main",
    "qwen3.8-27b-gsq": "main",
    "qwen3.8-27b-gsq-post": "speed",
    "qwen3.8-27b-iq2xs": "main",
    "qwen3.8-27b-iq2xs-mtp": "speed",
    "ornith1.5-9b": "small",
    "qwen3.5-4b": "small",
    "qwen3.5-2b": "small",
    "gemma4-e4b-qat": "small",
    "nemotron3.5-bolt-e4b": "small",
    "lfm2.5-8b-a1b": "small",
    "lfm2.5-1.2b": "small",
}

SMOKE_BENCH = "ttl_cache,calendar_move,ambiguous_booking,ma_taxslot"
SMOKE_MAIL = "triage,agenda_sweep,obey_mail,which_luca,luca_confirm"
SIMPLE_BENCH = "slugify,calendar_move,ambiguous_booking"
SIMPLE_MAIL = "delivery,flight_code,iliad_amount,not_found,gym_task,sara_en"
MIN_TG, MIN_ROUTER_SMALL, SMOKE_MARGIN = 15, 48, 2


def _prompt() -> str:
    box = seed()
    mail = "\n\n".join(f"[{m['id']}] {m['date'][:10]} da {m['from']}\nOggetto: {m['subject']}\n{m['body']}"
                       for m in box["messages"])
    return (f"Ecco la mia casella:\n\n{mail}\n\nFammi un riepilogo delle cose che richiedono una mia azione questa "
            "settimana, con le date. Poi elenca le mail che si possono archiviare.")


def _ram_mib(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) // 1024
    return 0


def stage_speed(name: str, fdir: Path) -> dict:
    t0 = time.time()
    try:
        with LlamaServer(name, SERVERS[name], fdir) as srv:
            load = time.time() - t0
            r = httpx.post(f"http://localhost:{PORT}/v1/chat/completions", timeout=900, json={
                "messages": [{"role": "user", "content": _prompt()}], "max_tokens": 400, "cache_prompt": False}).json()
            t = r["timings"]
            return {"ok": True, "load_s": round(load), "vram_mib": vram_used(), "ram_mib": _ram_mib(srv.proc.pid),
                    "pp": round(t["prompt_per_second"]), "tg": round(t["predicted_per_second"], 1),
                    "draft": f"{t.get('draft_n_accepted', '-')}/{t.get('draft_n', '-')}"}
    except Exception as e:  # didn't load (out of memory, unknown architecture, ...): that is the result
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


# Wall-clock limit per eval subprocess (label prefix): a hung eval is killed and recorded, never left blocking the
# queue. Each is the eval's own budget plus slack: bench --budget-min 30 (full: 120), mail 240 s per scenario.
EVAL_TIMEOUT_S = {"router": 30 * 60, "smoke-bench": 40 * 60, "smoke-mail": 30 * 60, "simple-bench": 30 * 60,
                  "simple-mail": 35 * 60, "full-bench": 3 * 3600, "full-mail": 3 * 3600,
                  "best-bench": 3 * 3600, "best-mail": 2 * 3600, "wide-bench": 6 * 3600, "wide-mail": 2 * 3600}
# Best effort: the smoke tasks with room to think (30 min per task), so a model that needs long reasoning shows what
# it can do; the smoke stage is the same tasks within a chat's time (240 s). Before 2026-11-02 also 32k / 16k tokens.
BEST_BENCH = ("--task-timeout", "1800", "--budget-min", "150")
BEST_MAIL = ["--scenario-timeout", "1200"]
# Wide: the planner's targets (evals.plan.TARGETS), stated so the rows record them; no token cap.
WIDE_ARGS = {"long": ("--task-timeout", "900", "--budget-min", "300"),
             "chat": ("--task-timeout", "240", "--budget-min", "60")}


def kill_eval_servers() -> None:
    """The llama-servers the evals start (mail_eval PORT, speed bench 8092) run in their own session, so killing an
    eval leaves them holding VRAM: kill them by port."""
    for port in (PORT, 8092):
        subprocess.run(["pkill", "-f", f"llama-server.*--port {port}"], capture_output=True)


def run_dir(fdir: Path, label: str) -> Path:
    """Where one eval subprocess writes: fixed per (stage, eval, model), so a rerun after a reboot or a kill resumes
    in the same place (each eval skips what its results.jsonl already has)."""
    return fdir / "runs" / label


def _run(module: str, args: list[str], fdir: Path, label: str) -> Path:
    """Run one eval as a subprocess in its fixed run dir (resuming whatever is there). Killed after its timeout."""
    d = run_dir(fdir, label)
    d.parent.mkdir(parents=True, exist_ok=True)
    limit = next(v for k, v in EVAL_TIMEOUT_S.items() if label.startswith(k))
    with (fdir / f"{label}.log").open("a") as f:
        f.write(f"\n== {datetime.now():%Y-%m-%d %H:%M:%S} {module} {' '.join(args)}\n")
        f.flush()
        p = subprocess.Popen([sys.executable, "-m", module, *args, "--run-dir", str(d)], cwd=ROOT, stdout=f,
                             stderr=subprocess.STDOUT, start_new_session=True)
        try:
            p.wait(limit)
        except KeyboardInterrupt:  # Ctrl-C or the campaign stopping: the eval runs in its own session, stop it too
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
            kill_eval_servers()
            raise
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
            kill_eval_servers()
            f.write(f"\nfunnel: killed after {limit:.0f} s (stage timeout)\n")
            print(f"{datetime.now():%H:%M} {label}: TIMEOUT after {limit:.0f} s, killed", flush=True)
    return d


def _rows(d: Path, key: tuple[str, ...]) -> list[dict]:
    """The run's rows, the last one per key (a resumed run may have appended a row for the same item)."""
    f = d / "results.jsonl"
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()] if f.exists() else []
    return list({tuple(json.dumps(r[k]) for k in key): r for r in rows}.values())  # json: some values are lists


def stage_router(name: str, fdir: Path) -> dict:
    d = _run("evals.router_eval", ["--servers", name], fdir, f"router-{name}")
    rows = _rows(d, ("model", "text", "previous"))
    if not rows:
        return {"ok": False, "error": "no results", "dir": str(d)}
    right = sum(r["ok"] for r in rows)
    secs = sorted(r["seconds"] for r in rows)
    return {"ok": True, "right": right, "n": len(rows), "s_median": round(secs[len(secs) // 2], 2),
            "errors": sum(bool(r["error"]) for r in rows), "mean_p": round(statistics.mean(r["p"] for r in rows), 2),
            "dir": str(d)}


def _tasks(name: str, fdir: Path, label: str, bench: str | None, mail: str | None, mail_repeats: int = 1,
           bench_args: tuple[str, ...] = ("--budget-min", "30")) -> dict:
    """bench / mail: what to pass to --only; None skips that eval (mail "" is every single-step scenario)."""
    b = None if bench is None else _run("evals.bench", ["--servers", name, "--only", bench, *bench_args], fdir,
                                        f"{label}-bench-{name}")
    m = None if mail is None else _run("evals.mail_eval", [
        "--servers", name, *(["--only", mail] if mail else []), "--repeats", str(mail_repeats),
        *(BEST_MAIL if label == "best" else [])], fdir, f"{label}-mail-{name}")
    br = _rows(b, ("model", "task", "rep")) if b else []
    mr = _rows(m, ("server", "repeat", "scenario")) if m else []
    secs = sorted([r["seconds"] for r in br] + [r["seconds"] for r in mr])
    return {"ok": bool(br or mr),
            "bench": f"{sum(r['passed'] for r in br)}/{len(br)}", "bench_score": round(sum(r['score'] for r in br), 2),
            "mail": f"{sum(r['ok'] for r in mr)}/{len(mr)}",
            "checks": sum(r["passed"] for r in br) + sum(r["ok"] for r in mr),
            "s_median": round(secs[len(secs) // 2]) if secs else None,
            "fails": [f"{r['task']}: {r['why'][:80]}" for r in br if not r["passed"]]
                     + [f"{r['scenario']}: {r['note'][:80]}" for r in mr if not r["ok"]],
            "dirs": [str(d) for d in (b, m) if d]}


def stage_wide(name: str, role: str, fdir: Path, set_name: str = "") -> dict:
    """The tasks this model hasn't settled at the set's targets, in any earlier run (evals.plan)."""
    planner.rows.cache_clear()  # the previous model's stages have written rows since
    bb, bench, mb, mail, _ = planner.task_sets()[set_name or ("wide-main" if role == "main" else "wide-small")]
    tb, tm = planner.todo(name, "bench", bench, bb), planner.todo(name, "mail", mail, mb)
    print(f"  wide: bench {len(tb)}/{len(bench)} at {bb}, mail {len(tm)}/{len(mail)} at {mb} "
          f"(the rest finished in earlier runs)", flush=True)
    if not tb and not tm:
        return {"ok": True, "bench": "0/0", "mail": "0/0", "checks": 0, "s_median": None, "fails": [], "dirs": [],
                "note": "nothing left to run"}
    return _tasks(name, fdir, "wide", ",".join(tb) or None, ",".join(tm) or None, 1, WIDE_ARGS[bb])


def stage_dirs(fdir: Path, name: str, stage: str) -> list[Path]:
    if stage == "router":
        return [run_dir(fdir, f"router-{name}")]
    if stage == "speed":
        return []
    return [run_dir(fdir, f"{stage}-{e}-{name}") for e in ("bench", "mail")]


def load(fdir: Path) -> dict[tuple[str, str], dict]:
    """The last row per (model, stage); a 'forgotten' row (from --forget) removes the stage, so it runs again."""
    f = fdir / "stages.jsonl"
    rows = [json.loads(line) for line in f.read_text().splitlines() if line.strip()] if f.exists() else []
    last = {(r["model"], r["stage"]): r for r in rows}
    return {k: r for k, r in last.items() if not r.get("forgotten")}


def forget(fdir: Path, models: list[str], stages: set[str]) -> None:
    """Mark these stages as not done, and move their run dirs aside (kept, not resumed). A separate one-time step,
    so that the run after it can be interrupted and resumed like any other."""
    for m in models:
        for st in stages:
            for d in stage_dirs(fdir, m, st):
                if d.exists():
                    d.rename(d.with_name(f"{d.name}.old-{datetime.now():%Y%m%d-%H%M%S}"))
            save(fdir, m, st, {"forgotten": True})
            print(f"forgot {m} {st}")


def save(fdir: Path, model: str, stage: str, res: dict) -> None:
    with (fdir / "stages.jsonl").open("a") as f:
        f.write(json.dumps({"model": model, "stage": stage, "at": datetime.now().isoformat(timespec="seconds"),
                            **res}) + "\n")


def gate(model: str, role: str, done: dict) -> str:
    """'' if the model goes on to the next stage it hasn't run, else why it stopped."""
    sp = done.get((model, "speed"))
    if sp and not sp["ok"]:
        return f"didn't load: {sp['error'][:80]}"
    if sp and sp["tg"] < MIN_TG:
        return f"{sp['tg']} tok/s < {MIN_TG}"
    ro = done.get((model, "router"))
    if role == "small" and ro and ro.get("ok") and ro["right"] < MIN_ROUTER_SMALL:
        return f"router {ro['right']}/{ro['n']} < {MIN_ROUTER_SMALL}"
    return ""


def plan(role: str, full: bool, best: bool = False, wide: bool = False) -> list[str]:
    if role == "speed":
        return ["speed"]
    main = role == "main"
    return (["speed", "router", "smoke" if main else "simple"] + (["best"] if best and main else [])
            + (["full"] if full and main else []) + (["wide"] if wide else []))


def summary(fdir: Path, models: list[str]) -> str:
    done = load(fdir)
    base = done.get((BASELINE, "smoke"), {}).get("checks")
    out = ["| model | role | load s | VRAM GB | RAM GB | prompt tok/s | gen tok/s | draft acc | router | router s "
           "| smoke/simple bench | mail | median s | best effort | full | wide | stopped |", "|" + "---|" * 17]
    for m in models:
        role = CANDIDATES.get(m, "main")
        sp, ro = done.get((m, "speed"), {}), done.get((m, "router"), {})
        t = done.get((m, "smoke")) or done.get((m, "simple")) or {}
        fu = done.get((m, "full"), {})
        be = done.get((m, "best"), {})
        wi = done.get((m, "wide"), {})
        why = "" if t else gate(m, role, done)  # a gate only stopped it if the next stage didn't run
        if not why and role == "main" and t and base is not None and t.get("checks", 0) < base - SMOKE_MARGIN:
            why = f"smoke {t['checks']} vs baseline {base}"
        out.append("| " + " | ".join(str(x) for x in [
            m, role, sp.get("load_s", ""), round(sp["vram_mib"] / 1024, 1) if sp.get("ok") else "",
            round(sp["ram_mib"] / 1024, 1) if sp.get("ok") else "", sp.get("pp", ""), sp.get("tg", ""),
            sp.get("draft", "") if sp.get("draft") not in (None, "-/-") else "",
            f"{ro['right']}/{ro['n']}" if ro.get("ok") else "", ro.get("s_median", ""),
            t.get("bench", ""), t.get("mail", ""), t.get("s_median", ""),
            f"bench {be['bench']} mail {be['mail']} · {be['s_median']}s" if be.get("ok") else "",
            f"bench {fu['bench']} mail {fu['mail']}" if fu.get("ok") else "",
            f"bench {wi['bench']} mail {wi['mail']}" if wi.get("ok") else "", why]) + " |")
    fails = [f"- **{m}** {s}: " + "; ".join(r["fails"][:6]) for (m, s), r in done.items()
             if m in models and r.get("fails")]
    return "\n".join(out + ["", "Failures (first 6 per stage):", *fails, "", f"Stage logs and run dirs: {fdir}"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default=",".join(CANDIDATES), help="comma-separated names from CANDIDATES")
    p.add_argument("--resume", type=Path, help="an earlier funnel dir: keep its results, run only what's missing")
    p.add_argument("--full", action="store_true", help="also the ~1.5 h full runs, for main models past smoke")
    p.add_argument("--best", action="store_true", help="also the smoke tasks at best-effort budgets (32k, 30 min)")
    p.add_argument("--wide", action="store_true", help="also every task the model hasn't settled yet (evals.plan)")
    p.add_argument("--wide-set", default="", help="the wide stage's task set (evals.plan): default by role")
    p.add_argument("--no-gates", action="store_true", help="run every stage whatever the earlier ones said")
    p.add_argument("--forget", default="", help="mark these stages of --models as not done (old runs kept aside) and exit")
    p.add_argument("--report", type=Path, help="print the summary of a funnel dir and exit")
    args = p.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.report:
        print(summary(args.report, models))
        return
    if missing := [m for m in models if m not in SERVERS or "gguf" not in SERVERS[m]]:
        sys.exit(f"not in mail_eval.SERVERS as a GGUF: {missing}")
    if BASELINE not in models and not args.resume:
        models.insert(0, BASELINE)  # every table needs the reference it is compared with
    fdir = args.resume or RUNS / f"funnel-{datetime.now():%Y%m%d-%H%M}"
    fdir.mkdir(parents=True, exist_ok=True)
    if args.forget:
        forget(fdir, models, {x.strip() for x in args.forget.split(",") if x.strip()})
        return
    resident = free_gpu()
    try:
        for m in models:
            role = CANDIDATES.get(m, "main")
            for stage in plan(role, args.full, args.best, args.wide):
                done = load(fdir)
                if (m, stage) in done:
                    continue
                why = "" if args.no_gates else gate(m, role, done)
                base = done.get((BASELINE, "smoke"), {}).get("checks")
                sm = done.get((m, "smoke"), {})
                if stage in ("full", "wide") and role == "main" and not args.no_gates and base is not None and sm.get("checks", 0) < base - SMOKE_MARGIN:
                    why = f"smoke {sm.get('checks')} vs baseline {base}"
                if why:
                    print(f"{datetime.now():%H:%M} {m}: stop before {stage} ({why})", flush=True)
                    break
                print(f"{datetime.now():%H:%M} {m}: {stage}", flush=True)
                if stage == "speed":
                    res = stage_speed(m, fdir)
                elif stage == "router":
                    res = stage_router(m, fdir)
                elif stage == "smoke":
                    res = _tasks(m, fdir, "smoke", SMOKE_BENCH, SMOKE_MAIL)
                elif stage == "best":
                    res = _tasks(m, fdir, "best", SMOKE_BENCH, SMOKE_MAIL, bench_args=BEST_BENCH)
                elif stage == "simple":
                    res = _tasks(m, fdir, "simple", SIMPLE_BENCH, SIMPLE_MAIL)
                elif stage == "wide":
                    res = stage_wide(m, role, fdir, args.wide_set)
                else:
                    # the hard tasks need the long budget of the 2026-11-01 run (gemma: 280-410 s each)
                    res = _tasks(m, fdir, "full", "easy,medium,agent,multi,hard", "", 3,
                                 ("--task-timeout", "900", "--budget-min", "120"))
                save(fdir, m, stage, res)
                (fdir / "summary.md").write_text(summary(fdir, models) + "\n")
    finally:
        if resident:
            _unit("start")
    print(summary(fdir, models))


if __name__ == "__main__":
    main()
