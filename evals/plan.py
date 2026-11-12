"""What has already run, on which model, at which budget, and what a sweep still has to run.

No separate store: every bench and mail_eval run already writes results.jsonl, so this reads all of them under
evals/runs (funnel stage dirs included). A row's budget is (token cap per answer, seconds per task); None tokens
means no cap (the server's context window), the default since 2026-11-02.

A (model, task) pair is done for a target budget when some row finished it (not cut short by a timeout, the token
cap, the run's budget or a server error) within that budget: its cap was no higher than the target's (or never hit)
and it took no longer than the target's seconds. Such a row would have come out the same under the target budget,
so it isn't run again; a cut-short row always is. The funnel's "wide" stage asks `todo()` for its task list.

Apples to apples (evals/harness.py): a row settles a pair only if its harness version is the current one (or
registered equal to it), its task version is the task's current one, and its config is how the model is served now.
Rows from before versioning are read as the legacy versions in evals/harnesses.json, with their task and config
taken as the current ones (the registry note says why that holds for bench; for mail no legacy version is equal).

    uv run python -m evals.plan --models gemma4-26b-a4b,gemma4-26b-qat --tasks wide-main   # what would run
    uv run python -m evals.plan --coverage long             # model × area, passed/finished, bench at 900 s, no cap
    uv run python -m evals.plan --coverage chat --kind mail
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evals" / "runs"

# Named targets: (token cap or None, seconds per task)
TARGETS = {"chat": (None, 240), "long": (None, 900), "best": (None, 1800)}
# Rows written before they carried their budget: the funnel's dirs by stage label, standalone bench dirs by name.
LABEL_BUDGET = {"smoke": (8000, 240), "simple": (8000, 240), "best": (32000, 1800), "full": (16000, 900)}
LEGACY = {"bench-20261101-131537": (16000, 900)}
BENCH_DEFAULT = (8000, 240)  # bench's defaults until 2026-11-02
CODE_TAGS = {"code", "workspace"}


def task_sets() -> dict[str, tuple[str, tuple[str, ...], str, tuple[str, ...], str]]:
    """name → (bench target, bench tasks, mail target, mail scenarios, what). Imported lazily: the task modules and
    mail_eval are heavy, and --coverage doesn't need them."""
    from evals.mail_eval import ALL, SCENARIOS
    from evals.tasks import TASKS, tags_of

    every = tuple(t.name for t in TASKS)
    no_code = tuple(t.name for t in TASKS if not CODE_TAGS & set(tags_of(t)))
    mail_all = tuple(s.name for s in ALL)
    return {
        # main models: every bench task (900 s each, the hard ones need it) and every mail scenario at the 240 s a
        # chat allows; what earlier runs finished within that is skipped
        "wide-main": ("long", every, "chat", mail_all, "every bench task and mail scenario"),
        # the same without code and workspace tasks, for a model too slow for them (they are ~85% of the time)
        "wide-main-nocode": ("long", no_code, "chat", mail_all, "every bench task except code, every mail scenario"),
        # small models: a quick chat turn (easy tier without code: coding never goes to a small model; single-step
        # mail), at the chat time limit
        "wide-small": ("chat", tuple(t.name for t in TASKS if t.tier == "easy" and not CODE_TAGS & set(tags_of(t))),
                       "chat", tuple(s.name for s in SCENARIOS), "the easy tier (no code) and the single-step mail"),
    }


def version_of(kind: str, row: dict, path: Path) -> str:
    """The row's harness version, after equal versions are followed; unversioned rows get their legacy version."""
    from evals import harness

    v = row.get("harness")
    if not v:
        if kind == "bench":
            v = "bench-v1"
        else:
            m = re.search(r"(\d{8})-\d{4}", path.parent.name)
            v = "mail-v0" if m and "funnel-" not in str(path) and m[1] < "20261101" else "mail-v1"
    return harness.canonical(kind, v)


@cache
def current_harness(kind: str) -> str:
    from evals import harness

    return harness.canonical(kind, harness.current(kind))


@cache
def current_task_v(kind: str, task: str) -> str | None:
    from evals import harness
    from evals.mail_eval import ALL
    from evals.tasks import TASKS

    t = next((x for x in (TASKS if kind == "bench" else ALL) if x.name == task), None)
    return harness.task_version(t) if t else None


@cache
def current_config(kind: str, model: str) -> str | None:
    """How the model is served now, as the evals record it (None: not a SERVERS entry, e.g. 'name@low')."""
    from evals import harness
    from evals.mail_eval import SERVERS

    spec = SERVERS.get(model)
    if spec is None:
        return None
    if kind == "bench":
        return harness.config_id(spec, thinking=True)
    return harness.config_id(spec, thinking=True, reasoning=spec.get("reasoning", ""), routing="")


def same_setup(r: dict) -> bool:
    """Harness, task and config as they are now. Unversioned rows: the harness and the task by the legacy entry's
    versions (a task edited since, e.g. its judge, no longer counts them), the config assumed."""
    from evals import harness

    if r["harness"] != current_harness(r["kind"]):
        return False
    tv = r["task_v"]
    if not tv:
        snap = harness.legacy_task_v(r["kind"])
        tv = snap.get(r["task"], "unknown") if snap else None
    if tv and tv != current_task_v(r["kind"], r["task"]):
        return False
    return not r["config"] or r["config"] == current_config(r["kind"], r["model"])


def budget_of(kind: str, row: dict, path: Path) -> tuple[int | None, int]:
    """(token cap, seconds) of a row."""
    if "budget" in row:
        mt, to = row["budget"]
        return (mt or None, to)
    d = path.parent
    if d.name in LEGACY:
        return LEGACY[d.name]
    if "funnel-" in str(d):
        return LABEL_BUDGET.get(d.name.split("-", 1)[0], BENCH_DEFAULT)
    return BENCH_DEFAULT


def finished(kind: str, row: dict) -> bool:
    """A result that says something about the model, not about a limit or a server error."""
    if kind == "bench":
        return not row["skipped"] and not row.get("truncated") and not any(k in row["why"] for k in (
            "timeout", "ran out of tokens", "cut at the token limit", "error:", "budget used up"))
    return not any(k in row["note"] for k in (
        "timeout after", "ModelServerError", "token limit", "ConnectError", "RemoteProtocolError"))


def fits(r: dict, target: tuple[int | None, int]) -> bool:
    """A finished row of the current setup that would have come out the same under `target`."""
    if not r["done"] or not same_setup(r):
        return False
    cap, secs = r["budget"]
    return (cap or math.inf) <= (target[0] or math.inf) and r["seconds"] <= target[1]


@cache
def rows() -> list[dict]:
    """Every bench and mail row under evals/runs as {kind, model, task, budget, passed, done, seconds, dir}."""
    out = []
    for f in sorted(RUNS.rglob("results.jsonl")):
        if ".old-" in str(f):  # moved aside by funnel --forget: measured wrong
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if "task" in r:
                kind, model, task, passed = "bench", r["model"], r["task"], r["passed"]
            elif "scenario" in r:
                kind, model, task, passed = "mail", r["server"], r["scenario"], r["ok"]
            else:
                continue  # router, speed
            out.append({"kind": kind, "model": model, "task": task, "budget": budget_of(kind, r, f), "passed": passed,
                        "done": finished(kind, r), "seconds": r.get("seconds", 0), "dir": str(f.parent.relative_to(RUNS)),
                        "harness": version_of(kind, r, f), "task_v": r.get("task_v"), "config": r.get("config"),
                        "cost": r.get("cost_usd")})
    return out


def done(kind: str, target: str) -> dict[tuple[str, str], str]:
    """(model, task) → the run dir of a row that settles it at this target."""
    t = TARGETS[target]
    return {(r["model"], r["task"]): r["dir"] for r in rows() if r["kind"] == kind and fits(r, t)}


def todo(model: str, kind: str, tasks: tuple[str, ...], target: str) -> list[str]:
    d = done(kind, target)
    return [t for t in tasks if (model, t) not in d]


def show_plan(models: list[str], set_name: str) -> str:
    bt, bench, mt, mail, what = task_sets()[set_name]
    out = [f"{set_name}: {what}", f"  bench {len(bench)} tasks at {bt} {TARGETS[bt]}, mail {len(mail)} scenarios at "
           f"{mt} {TARGETS[mt]} (tokens None = no cap)", ""]
    for m in models:
        tb, tm = todo(m, "bench", bench, bt), todo(m, "mail", mail, mt)
        skipped = [t for t in bench if t not in tb] + [s for s in mail if s not in tm]
        out.append(f"{m}: run bench {len(tb)}, mail {len(tm)}; already settled {len(skipped)}"
                   + (f" ({', '.join(skipped)})" if skipped else ""))
    return "\n".join(out)


def coverage(target: str, kind: str) -> str:
    """Model × area at one target: passed / settled, and (n) pairs only cut short so far. Per pair, the settling row
    with the largest budget counts."""
    from evals.tasks import TASKS, tags_of

    tags = {t.name: tags_of(t) for t in TASKS}
    t = TARGETS[target]
    best: dict[tuple[str, str], dict] = {}
    cut: set[tuple[str, str]] = set()
    other = 0
    for r in rows():
        if r["kind"] != kind:
            continue
        if not same_setup(r):
            other += 1
            continue
        key = (r["model"], r["task"])
        if fits(r, t):
            cap, secs = r["budget"]
            prev = best.get(key)
            if not prev or (cap or math.inf, secs) >= (prev["budget"][0] or math.inf, prev["budget"][1]):
                best[key] = r
        else:
            cut.add(key)
    cut -= set(best)
    models = list(dict.fromkeys(m for m, _ in [*best, *cut]))
    cells: dict[tuple[str, str], list] = defaultdict(list)
    for key in [*best, *cut]:
        m, task = key
        for g in ("all", *(tags.get(task, ()) if kind == "bench" else ())):
            cells[(m, g)].append(best.get(key))
    areas = ["all"] + sorted({g for _, g in cells} - {"all"})
    out = [f"{kind} at {target} {t}, harness {current_harness(kind)}: passed/settled, (n) only cut short so far; "
           f"{other} rows of another harness, task or config version left out", "",
           "| model | " + " | ".join(areas) + " |", "|---|" + "---|" * len(areas)]
    for m in models:
        row = []
        for g in areas:
            rs = cells.get((m, g), [])
            fin = [r for r in rs if r]
            n_cut = len(rs) - len(fin)
            row.append((f"{sum(r['passed'] for r in fin)}/{len(fin)}" + (f" ({n_cut})" if n_cut else "")) if rs else "")
        out.append(f"| {m} | " + " | ".join(row) + " |")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default="", help="comma-separated SERVERS names")
    p.add_argument("--tasks", default="wide-main", help="a task set: wide-main, wide-main-nocode, wide-small")
    p.add_argument("--coverage", metavar="TARGET", choices=list(TARGETS), help="model × area at this target, then exit")
    p.add_argument("--kind", default="bench", choices=["bench", "mail"])
    args = p.parse_args()
    if args.coverage:
        print(coverage(args.coverage, args.kind))
        return
    print(show_plan([m for m in args.models.split(",") if m], args.tasks))


if __name__ == "__main__":
    main()
