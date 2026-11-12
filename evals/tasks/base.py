"""What a bench task is, and the helpers every task module shares. How to write one: evals/README.md.

A task is one request with an automatic judge, never a judge model:
  - single-shot code (`tests`): one answer, its last ```python block run against hidden checks;
  - agent (`judge`): a tool loop on a world (a workspace of files, or a fake schedule / ledger / inbox), judged on the
    world's state afterwards and on the replies. `turns` adds follow-up user messages (the user answering a question);
    `agents` gives the main agent a tool to hand self-contained requests to sub-agents, each its own tool loop.
`reference` is a known-good answer (code, or a function making the right tool calls): `bench --check` runs every
reference through its judge, and the empty answer too, so a judge that is wrong shows up without a model.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

# A stale .pyc (same-second edit, same size) would score the previous version of a file the model just changed.
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}



# ---- scoring helpers --------------------------------------------------------------------------------------------------
HARNESS = '''
import json as _j
_r = []
def check(name, fn):
    try:
        ok = bool(fn()); err = "" if ok else "wrong result"
    except BaseException as e:
        ok, err = False, f"{type(e).__name__}: {str(e)[:80]}"
    _r.append((name, ok, err))
def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    except BaseException:
        return False
    return False
def done():
    print("@@RESULT@@" + _j.dumps({"passed": sum(o for _, o, _ in _r), "total": len(_r),
                                   "failed": [f"{n} ({e})" for n, o, e in _r if not o]}))
'''


def extract_code(text: str) -> str:
    blocks = re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S)
    return blocks[-1] if blocks else text


def run_tests(code: str, tests: str, forbidden: str = "") -> tuple[float, str]:
    if forbidden and re.search(forbidden, code):
        return 0.0, "used a forbidden construct"
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "_h.py").write_text(HARNESS)
        (d / "solution.py").write_text(code)
        (d / "tests.py").write_text("from _h import check, raises, done\nfrom solution import *\n" + tests + "\ndone()\n")
        try:
            p = subprocess.run([sys.executable, "tests.py"], cwd=d, capture_output=True, text=True, timeout=30, env=ENV)
        except subprocess.TimeoutExpired:
            return 0.0, "tests timed out (infinite loop?)"
    m = re.search(r"@@RESULT@@(.*)", p.stdout)
    if not m:
        return 0.0, (p.stderr.strip().splitlines() or ["no output"])[-1][:200]
    r = json.loads(m.group(1))
    return r["passed"] / r["total"], "; ".join(r["failed"])[:300]



SYS_CODE = ("You are an expert Python programmer. Reply with the complete solution in one ```python code block, "
            "standard library only. No tests, no usage examples outside the block.")


# ---- tools and judges ---------------------------------------------------------------------------------------------
def tool(name: str, desc: str, props: dict[str, str | dict] | None = None, required: list[str] | None = None) -> dict:
    """A function-tool schema. A property is a description (a string parameter) or a JSON schema of its own
    ({"type": "integer", "description": ...}); every property is required unless `required` says otherwise."""
    p = {k: v if isinstance(v, dict) else {"type": "string", "description": v} for k, v in (props or {}).items()}
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": p,
                                                            "required": required if required is not None else list(p)}}}


def _score(checks: dict[str, bool]) -> tuple[float, str]:
    return sum(checks.values()) / len(checks), ("failed: " + ", ".join(k for k, v in checks.items() if not v)) if not all(checks.values()) else ""


@dataclasses.dataclass
class Task:
    name: str
    tier: str
    what: str
    prompt: str
    tests: str = ""
    forbidden: str = ""
    files: dict[str, str] = dataclasses.field(default_factory=dict)  # agent: workspace seed
    judge: Callable | None = None  # agent: (workspace, final reply, life) -> (score, why)
    system: str = ""
    life: bool = False
    multi: bool = False  # an orchestrator that delegates to sub-agents over an Office
    timeout_s: int = 0  # 0: --task-timeout
    # ---- added 2026-11-01; the defaults keep the tasks above exactly as they were
    tags: tuple[str, ...] = ()  # areas, for --only and the report: life, money, mail, notes, code, workspace, ...
    world: Callable | None = None  # agent: makes the world (e.g. Home); default Office if multi, Life if life
    tools: tuple[str, ...] | None = None  # the main agent's own world tools; None: all (none when it has agents)
    agents: dict[str, tuple[str, ...]] | None = None  # sub-agent name -> its world tools (multi: default AGENTS)
    call_tool: str = "delegate"  # how the main agent reaches them: 'delegate' (an orchestrator) or 'ask' (a peer)
    turns: tuple[str, ...] = ()  # follow-up user messages, each sent after the previous final reply
    reference: str | Callable | None = None  # code: a solution; agent: fn(world or workspace) -> reply or [replies]


def check_task(task: Task, make_world: Callable[[Task], object], workspace: Callable[[Task], object]) -> list[str]:
    """No model: the reference must score 1.0 and the empty answer below 1.0. -> the problems found."""
    if task.reference is None:
        return ["no reference"]
    problems = []
    if task.tests:
        score, why = run_tests(task.reference, task.tests, task.forbidden)
        if score < 1:
            problems.append(f"reference scores {score:.2f}: {why}")
        if run_tests("", task.tests, task.forbidden)[0] >= 1:
            problems.append("the empty answer passes")
        return problems
    for label, ref in (("reference", task.reference), ("empty answer", None)):
        ws, world = workspace(task), make_world(task)
        replies = ref(world if world is not None else ws) if ref else [""] * (1 + len(task.turns))
        replies = [replies] if isinstance(replies, str) else list(replies)
        if world is not None:
            world.replies = replies
        try:
            score, why = task.judge(ws, replies[-1], world)
        except Exception as e:  # a judge that crashes on an odd state is a problem too
            score, why = 0.0, f"judge raised {type(e).__name__}: {e}"
            if label == "empty answer":
                problems.append(f"judge raises on the empty answer: {why}")
        ws.cleanup()
        if label == "reference" and score < 1:
            problems.append(f"reference scores {score:.2f}: {why}")
        if label == "empty answer" and score >= 1:
            problems.append("the empty answer passes")
    return problems
