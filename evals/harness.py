"""Harness and config versions, so a table only ever compares like with like.

Every bench and mail_eval row carries three identifiers:

- `harness`: the version of the code that ran the task (bench: the runner, the worlds and their tools; mail: the
  bot's mail agent and its tools, the fake Gmail, the mailbox). Computed from those files' contents and looked up
  in evals/harnesses.json. A change nobody registered becomes a new version on its own (printed as a warning), so
  results never mix silently; `--same-as` declares a change that can't move results (a comment, a refactor, a new
  server type) equal to the version before it.
- `task_v`: the task itself (its request, turns, system prompt, tools, world, judge and reference), so editing one
  task only invalidates that task's rows.
- `config`: how the model was served (gguf and llama-server args, sampling, reasoning, thinking, the llama.cpp
  build, or the cloud endpoint), registered in evals/configs.jsonl. The budget (token cap, seconds) is kept apart,
  in the row's `budget`, because evals.plan compares budgets by inclusion, not equality.

Rows written before versioning (2026-11-02) have none of these: evals.plan reads them as the legacy versions named
in harnesses.json ("legacy" entries), with the reason they were judged equal or not.

    uv run python -m evals.harness                       # current versions, and whether the files are registered
    uv run python -m evals.harness --register "note"     # register the current files as a new version
    uv run python -m evals.harness --same-as "note"      # register them as equal to the latest version
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import subprocess
import sys
from datetime import datetime
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "evals" / "harnesses.json"
CONFIGS = ROOT / "evals" / "configs.jsonl"

# What decides how a task runs, apart from the task itself and the model. Report code lives in these files too, so
# a change to it registers a version; --same-as says it can't move results.
FILES = {
    "bench": ["evals/bench.py", "evals/tasks/base.py", "evals/tasks/worlds.py", "evals/tasks/home.py"],
    "mail": ["evals/mail_eval.py", "evals/fake_gog.py", "evals/mailbox.py", "evals/gog_tools.json",
             "franziska/mail/agent.py", "franziska/mcp_server.py", "franziska/llm.py"],
}


def _h(data: bytes | str, n: int = 10) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()[:n]


def file_hashes(kind: str) -> dict[str, str]:
    return {f: _h((ROOT / f).read_bytes()) for f in FILES[kind]}


def fingerprint(kind: str) -> str:
    return _h(json.dumps(file_hashes(kind), sort_keys=True))


def _load() -> dict:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {"bench": [], "mail": []}


def _commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", *sum(FILES.values(), [])], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    return r.stdout.strip() + ("+dirty" if dirty else "")


def register(kind: str, note: str, same_as: str = "") -> dict:
    reg = _load()
    versions = reg.setdefault(kind, [])
    n = 1 + max((int(v["id"].rsplit("v", 1)[1]) for v in versions), default=0)
    entry = {"id": f"{kind}-v{n}", "hash": fingerprint(kind), "files": file_hashes(kind),
             "at": datetime.now().isoformat(timespec="seconds"), "commit": _commit(), "note": note}
    if same_as:
        entry["same_as"] = same_as
    versions.append(entry)
    REGISTRY.write_text(json.dumps(reg, indent=1) + "\n")
    current.cache_clear()
    return entry


@cache
def current(kind: str) -> str:
    """The registered version of the files as they are; an unregistered state is registered on the spot, as its own
    version (an eval never runs under a version it can't name)."""
    h = fingerprint(kind)
    for v in _load().get(kind, []):
        if v.get("hash") == h:
            return v["id"]
    prev = _load().get(kind, [])
    changed = []
    if prev and prev[-1].get("files"):
        changed = [f for f, x in file_hashes(kind).items() if prev[-1]["files"].get(f) != x]
    entry = register(kind, "unregistered change" + (f": {', '.join(changed)}" if changed else ""))
    print(f"harness: {kind} files changed since {prev[-1]['id'] if prev else 'nothing'} "
          f"({', '.join(changed) or '?'}): registered as {entry['id']}, a new version. If the change can't move "
          f"results, `python -m evals.harness --same-as` before running.", file=sys.stderr, flush=True)
    return entry["id"]


def canonical(kind: str, version: str) -> str:
    """A version after following its same_as chain: rows of equal versions compare."""
    by_id = {v["id"]: v for v in _load().get(kind, [])}
    seen = set()
    while version in by_id and by_id[version].get("same_as") and version not in seen:
        seen.add(version)
        version = by_id[version]["same_as"]
    return version


def legacy(kind: str) -> str:
    """The version unversioned rows (before 2026-11-02) are read as."""
    return next((v["id"] for v in _load().get(kind, []) if v.get("legacy")), f"{kind}-legacy")


@cache
def legacy_task_v(kind: str) -> dict[str, str]:
    """task → the task version unversioned rows ran, from the legacy entry (bench: the tasks as committed in
    ebe74d4). Empty when not recorded."""
    return next((v.get("task_v", {}) for v in _load().get(kind, []) if v.get("legacy")), {})


def _src(fn) -> str:
    try:
        return inspect.getsource(fn)
    except (TypeError, OSError):
        return repr(fn)


def task_version(task) -> str:
    """A bench Task or a mail Scenario: what it asks and how it's judged. The judge's and reference's own source
    counts, not helpers they call (a change to a shared helper is a harness change: register it)."""
    parts = [getattr(task, k, None) for k in ("name", "tier", "prompt", "turns", "system", "tags", "call_tool",
                                               "timeout_s", "life", "multi", "tests", "forbidden", "files", "tools")]
    parts.append(sorted((getattr(task, "agents", None) or {}).items()))
    for k in ("judge", "reference", "world"):
        v = getattr(task, k, None)
        parts.append(_src(v) if v is not None else None)
    return _h(json.dumps(parts, default=str, sort_keys=True))


@cache
def _known_configs() -> set[str]:
    if not CONFIGS.exists():
        return set()
    return {json.loads(line)["config"] for line in CONFIGS.read_text().splitlines() if line.strip()}


def config_id(spec: dict, **extra) -> str:
    """How a model is served, as an id; the details go to configs.jsonl once. `extra`: thinking, reasoning, ..."""
    from evals.mail_eval import LLAMA

    detail = {**{k: v for k, v in spec.items() if k not in ("price",)}, **extra}
    if "gguf" in detail:
        detail["llama"] = Path(LLAMA).name  # the llama.cpp build
    cid = "cfg-" + _h(json.dumps(detail, sort_keys=True, default=str), 8)
    if cid not in _known_configs():
        with CONFIGS.open("a") as f:
            f.write(json.dumps({"config": cid, "at": datetime.now().isoformat(timespec="seconds"), **detail},
                               default=str) + "\n")
        _known_configs.cache_clear()
    return cid


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--kind", choices=list(FILES), help="default: both")
    p.add_argument("--register", metavar="NOTE", help="register the current files as a new version")
    p.add_argument("--same-as", metavar="NOTE", help="register the current files as equal to the latest version")
    args = p.parse_args()
    for kind in [args.kind] if args.kind else list(FILES):
        h = fingerprint(kind)
        known = next((v for v in _load().get(kind, []) if v.get("hash") == h), None)
        if (args.register or args.same_as) and not known:
            prev = _load().get(kind, [])
            known = register(kind, args.register or args.same_as,
                             same_as=canonical(kind, prev[-1]["id"]) if args.same_as and prev else "")
        if known:
            eq = f" = {canonical(kind, known['id'])}" if known.get("same_as") else ""
            print(f"{kind}: {known['id']}{eq} ({known['at'][:16]}, {known['commit']}): {known['note']}")
        else:
            print(f"{kind}: files not registered (hash {h}); the next run registers them as a new version")


if __name__ == "__main__":
    main()
