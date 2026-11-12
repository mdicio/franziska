"""Every bench task, in one list. How to add one: evals/README.md ("Adding a task").

Modules by kind (each has OLD, the 2026-10 tasks kept as they were, and NEW):
  code.py       single-shot: one answer, hidden checks
  workspace.py  coding agent in a scratch directory (list / read / write / run python)
  assistant.py  one assistant agent on a fake world (Life, or the bot-like Home: schedule, notes, money, mail)
  multi.py      a main agent that delegates to sub-agents (orchestrator) or asks a peer assistant (the bot's `ask`)
Worlds: worlds.py (Workspace, Life, Office: the 2026-10 ones, unchanged) and home.py (Home).

Tiers are the run order and the report's columns: easy, medium (single-shot), agent (tool loop), multi, hard.
Tags say what a task exercises (life, money, mail, notes, code, workspace, ask, guard, read, multi-turn, it, en);
`bench --only` takes task names, tiers and tags.
"""

from __future__ import annotations

from evals.tasks import assistant, code, multi, workspace
from evals.tasks.base import Task, check_task
from evals.tasks.worlds import Life, Office, Workspace

TIERS = ["easy", "medium", "agent", "multi", "hard"]  # run order: the costly hard ones last, so a budget cut hits them first

# Old tasks first, in their original order (the bench sorts by tier, stably), then the new ones.
TASKS: list[Task] = code.OLD + workspace.OLD + assistant.OLD + multi.OLD + code.NEW + workspace.NEW + assistant.NEW + multi.NEW

_names = [t.name for t in TASKS]
assert len(set(_names)) == len(_names), sorted(n for n in _names if _names.count(n) > 1)
assert all(t.tier in TIERS for t in TASKS), [t.name for t in TASKS if t.tier not in TIERS]


def make_world(task: Task):
    """The world an agent task runs in (None: a workspace task)."""
    if task.world is not None:
        return task.world()
    if task.multi:
        return Office()
    if task.life:
        return Life()
    return None


def make_workspace(task: Task) -> Workspace:
    return Workspace(task.files)


# The 2026-10 tasks were written before tags; theirs live here so the Task objects stay as they were.
OLD_TAGS = {"fix_inventory": ("workspace",), "build_wc": ("workspace",), "add_recurrence": ("workspace", "code"),
            "calendar_move": ("life", "en"), "ambiguous_booking": ("life", "en", "ask"),
            "ma_trip": ("life", "notes", "en"), "ma_budget": ("money", "life", "notes", "en"), "ma_taxslot": ("life", "en")}


def tags_of(task: Task) -> tuple[str, ...]:
    return task.tags or OLD_TAGS.get(task.name) or (("code",) if task.tests else ())


def check_all(tasks: list[Task]) -> list[tuple[str, list[str]]]:
    return [(t.name, check_task(t, make_world, make_workspace)) for t in tasks]


__all__ = ["TASKS", "TIERS", "Task", "check_all", "make_world", "make_workspace", "tags_of"]
