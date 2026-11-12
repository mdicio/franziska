"""A frontier-class cloud model through the same harness, to tell harness or judge problems from model limits.

A task that the reference model also fails points at the harness, the task or its judge (look at it before blaming
the local models); one that the reference passes and the local models fail is a model gap. Signal, not verdict:
one repeat, and a strong model can fail for its own reasons too.

    uv run --env-file .env python -m evals.reference --estimate            # what one run costs, before running it
    uv run --env-file .env python -m evals.reference                       # run (resumable), then the comparison
    uv run --env-file .env python -m evals.reference --compare             # the comparison only
    uv run --env-file .env python -m evals.reference --model di-sonnet-5.5 --only-failed   # a ceiling, on what failed

Cloud servers are SERVERS entries with "cloud" and "price" (evals/mail_eval.py). No GPU: it runs next to a local
campaign. Limits: 900 s per bench task and per mail scenario (cloud latency is not the local model's), no token cap.
Results: evals/runs/reference-<model>/{bench,mail}/ with cost_usd in every row; versions as in evals/harness.py.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from evals import plan
from evals.mail_eval import SERVERS

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = "di-mimo-v2.6-pro"
LOCAL = ["gemma4-26b-a4b", "gemma4-26b-qat", "qwen3.8-27b-gsq"]  # the main candidates still in the race
TIMEOUT_S = 900
# Tokens per task, measured: gemma 4 26B on 2026-11-01/02 (llama-server logs: the whole context of each request,
# which is what a cloud provider bills, and the tokens generated), corrected with MiMo-V2.6-Pro's smoke test (it
# searched and read more: agenda_sweep 61k in / 4.5k out against gemma's ~26k / 1.9k). (in, out) per task.
PROFILE = {"code": (300, 8000), "workspace": (60000, 10000), "home-easy": (5000, 200), "home": (12000, 2000),
           "mail": (35000, 2500)}
WALL_S = {"bench": 12 * 3600, "mail": 6 * 3600}  # per eval process: killed past it, resumed on the next run


def _kind(task) -> str:
    from evals.tasks import tags_of

    tags = set(tags_of(task))
    if "workspace" in tags:
        return "workspace"
    if "code" in tags:
        return "code"
    return "home-easy" if task.tier == "easy" else "home"


def todo(model: str, only_failed: bool = False) -> tuple[list[str], list[str]]:
    """Bench tasks and mail scenarios the model hasn't settled at 900 s; with only_failed, those the reference model
    (DEFAULT) failed or never settled."""
    _, bench, _, mail, _ = plan.task_sets()["wide-main"]
    tb, tm = plan.todo(model, "bench", bench, "long"), plan.todo(model, "mail", mail, "long")
    if only_failed:
        passed = {(r["kind"], r["task"]) for r in plan.rows() if r["model"] == DEFAULT and r["passed"]
                  and plan.fits(r, plan.TARGETS["long"])}
        tb = [t for t in tb if ("bench", t) not in passed]
        tm = [t for t in tm if ("mail", t) not in passed]
    return tb, tm


def estimate(model: str, tb: list[str], tm: list[str]) -> str:
    from evals.tasks import TASKS

    by = {t.name: t for t in TASKS}
    tin = tout = 0
    for name in tb:
        i, o = PROFILE[_kind(by[name])]
        tin, tout = tin + i, tout + o
    tin, tout = tin + len(tm) * PROFILE["mail"][0], tout + len(tm) * PROFILE["mail"][1]
    pin, pout = SERVERS[model]["price"]
    cost = (tin * pin + tout * pout) / 1e6
    return (f"{model}: {len(tb)} bench tasks + {len(tm)} mail scenarios, ~{tin / 1e6:.2f}M tokens in, "
            f"~{tout / 1e6:.2f}M out → ~${cost:.2f} (likely ${cost * 0.6:.2f}–{cost * 1.6:.2f}: output varies most "
            f"with how long the model thinks). Prices ${pin}/${pout} per 1M in/out.")


def _run(module: str, args: list[str], wall: int) -> None:
    p = subprocess.Popen([sys.executable, "-m", module, *args], cwd=ROOT, start_new_session=True)
    try:
        p.wait(wall)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as e:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(30)
        if isinstance(e, KeyboardInterrupt):
            raise
        print(f"{module}: killed after {wall} s; rerun to resume", flush=True)


def compare(model: str) -> str:
    """Per task the reference next to the local models, at 900 s, current harness/task/config versions only."""
    plan.rows.cache_clear()
    t = plan.TARGETS["long"]
    res: dict[tuple[str, str, str], bool] = {}
    for r in plan.rows():
        if plan.fits(r, t):
            res[(r["kind"], r["task"], r["model"])] = r["passed"]
    locals_ = [m for m in LOCAL if any(k[2] == m for k in res)]
    out = [f"## {model} vs {', '.join(locals_)} (900 s, harness {plan.current_harness('bench')} / "
           f"{plan.current_harness('mail')})", "",
           "| kind | task | reference | " + " | ".join(locals_) + " | reading |", "|---|---|---|" + "---|" * len(locals_) + "---|"]
    groups: dict[str, list[str]] = {"harness?": [], "model gap": [], "all pass": [], "reference only fails": []}
    for kind, task in sorted({(k, x) for k, x, m in res if m == model}):
        ref = res[(kind, task, model)]
        loc = [res.get((kind, task, m)) for m in locals_]
        ran = [x for x in loc if x is not None]
        if not ref and ran and not any(ran):
            reading = "harness?"
        elif ref and ran and not all(ran):
            reading = "model gap"
        elif ref and ran:
            reading = "all pass"
        elif not ref and any(ran):
            reading = "reference only fails"
        else:
            reading = "reference fails, no local result" if not ref else "no local result"
        groups.setdefault(reading, []).append(f"{kind}/{task}")
        cells = ["" if x is None else ("✓" if x else "✗") for x in loc]
        out.append(f"| {kind} | {task} | {'✓' if ref else '✗'} | " + " | ".join(cells) + f" | {reading} |")
    out += ["", "Harness or judge suspects (the reference fails too): " + (", ".join(groups["harness?"]) or "none"),
            "Model gaps (the reference passes, a local model fails): " + (", ".join(groups["model gap"]) or "none")]
    cost = sum(r["cost"] or 0 for r in plan.rows() if r["model"] == model)
    out.append(f"\nSpent on {model} so far: ${cost:.2f}")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=DEFAULT, help="a SERVERS entry with 'cloud'")
    p.add_argument("--estimate", action="store_true", help="print what one run would cost and exit")
    p.add_argument("--compare", action="store_true", help="print the comparison and exit")
    p.add_argument("--only-failed", action="store_true", help=f"only what {DEFAULT} failed or hasn't settled")
    args = p.parse_args()
    if "cloud" not in SERVERS.get(args.model, {}):
        sys.exit(f"{args.model}: not a cloud SERVERS entry")
    if args.compare:
        print(compare(args.model))
        return
    tb, tm = todo(args.model, args.only_failed)
    print(estimate(args.model, tb, tm), flush=True)
    if args.estimate:
        return
    d = ROOT / "evals" / "runs" / f"reference-{args.model}"
    if tb:
        _run("evals.bench", ["--servers", args.model, "--only", ",".join(tb), "--task-timeout", str(TIMEOUT_S),
                             "--budget-min", str(WALL_S["bench"] // 60), "--run-dir", str(d / "bench")], WALL_S["bench"])
    if tm:
        _run("evals.mail_eval", ["--servers", args.model, "--only", ",".join(tm), "--repeats", "1",
                                 "--scenario-timeout", str(TIMEOUT_S), "--run-dir", str(d / "mail")], WALL_S["mail"])
    print(compare(args.model))


if __name__ == "__main__":
    main()
