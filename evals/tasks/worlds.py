"""The worlds of the 2026-10 agent tasks: a scratch workspace with file tools, a tiny calendar (Life) and the
multi-agent Office (calendar, tasks, notes, ledger). Kept exactly as they were, so those tasks stay comparable
across runs; newer tasks use the bot-like Home (home.py).
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from evals.tasks.base import ENV, tool


class Workspace:
    """A scratch directory with file tools; `run` only starts `python <script> [args]`."""

    TOOLS = [
        tool("list_files", "List the files in the workspace."),
        tool("read_file", "Read a file.", {"path": "relative path"}),
        tool("write_file", "Create or overwrite a file with the full content.", {"path": "relative path", "content": "file content"}),
        tool("run", "Run a Python script: `python script.py [args]`. Returns exit code, stdout and stderr.",
             {"command": "e.g. python test_x.py"}),
    ]

    def __init__(self, files: dict[str, str]):
        self.dir = Path(tempfile.mkdtemp(prefix="bench-ws-"))
        for name, text in files.items():
            (self.dir / name).write_text(text)

    def _path(self, rel: str) -> Path:
        p = (self.dir / rel).resolve()
        if self.dir.resolve() not in p.parents:
            raise ValueError("path outside the workspace")
        return p

    def call(self, name: str, a: dict) -> str:
        try:
            if name == "list_files":
                return "\n".join(sorted(p.name for p in self.dir.iterdir() if p.is_file() and p.name != "__pycache__")) or "(empty)"
            if name == "read_file":
                return self._path(a["path"]).read_text()
            if name == "write_file":
                self._path(a["path"]).write_text(a["content"])
                return f"wrote {len(a['content'])} chars to {a['path']}"
            if name == "run":
                argv = shlex.split(a["command"])
                if len(argv) < 2 or argv[0] not in ("python", "python3") or argv[1].startswith("-"):
                    return "error: only `python <script> [args]` is allowed"
                p = subprocess.run([sys.executable, *argv[1:]], cwd=self.dir, capture_output=True, text=True, timeout=30, env=ENV)
                return f"exit {p.returncode}\nstdout:\n{p.stdout[-2000:]}\nstderr:\n{p.stderr[-2000:]}"
        except subprocess.TimeoutExpired:
            return "error: timed out after 30 s"
        except (OSError, ValueError, KeyError) as e:
            return f"error: {type(e).__name__}: {e}"
        return f"error: unknown tool {name}"

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, *argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *argv], cwd=self.dir, capture_output=True, text=True, timeout=30, env=ENV)


class Life:
    """A tiny in-memory calendar + task list with the shape of the bot's `life` tools. Today is Wed 2026-11-04."""
    NOW = datetime(2026, 11, 4, 9, 0)

    def __init__(self):
        self.appts = {"a1": ("Dentist", "2026-11-06T10:00", "2026-11-06T11:00"),
                      "a2": ("Team lunch", "2026-11-06T12:30", "2026-11-06T13:30"),
                      "a3": ("Gym", "2026-11-07T09:00", "2026-11-07T10:00"),
                      "a4": ("Standup", "2026-11-09T09:30", "2026-11-09T09:45")}
        self.tasks = {"t1": {"title": "Buy a gift for Anna", "due": "2026-11-06", "done": False}}
        self.cancelled: list[str] = []
        self.created_appts: list[str] = []
        self.n = 10

    TOOLS = [
        tool("now", "The current local date and time."),
        tool("agenda", "Appointments and open tasks between two dates (inclusive), ids included.",
             {"from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD"}),
        tool("add_appointment", "Add a calendar appointment.", {"title": "title", "start": "YYYY-MM-DDTHH:MM", "end": "YYYY-MM-DDTHH:MM"}),
        tool("move_appointment", "Move an appointment to a new start; its length stays.", {"id": "appointment id, e.g. a1", "new_start": "YYYY-MM-DDTHH:MM"}),
        tool("add_task", "Add a task, optionally with a due date.", {"title": "title", "due": "YYYY-MM-DD or empty"}, ["title"]),
        tool("cancel", "Delete an appointment or task by id.", {"id": "id"}),
    ]

    def call(self, name: str, a: dict) -> str:
        try:
            if name == "now":
                return self.NOW.strftime("%Y-%m-%dT%H:%M (%A)")
            if name == "agenda":
                lo, hi = str(a["from_date"]), str(a["to_date"])
                rows = [f"#{i} {t} {s} → {e}" for i, (t, s, e) in sorted(self.appts.items(), key=lambda kv: kv[1][1])
                        if lo <= s[:10] <= hi]
                rows += [f"#{i} task: {t['title']} (due {t['due']})" for i, t in self.tasks.items()
                         if not t["done"] and t["due"] and lo <= t["due"] <= hi]
                return "\n".join(rows) or "nothing"
            if name == "add_appointment":
                self.n += 1
                i = f"a{self.n}"
                end = a.get("end") or (datetime.fromisoformat(a["start"]) + timedelta(hours=1)).isoformat(timespec="minutes")
                self.appts[i] = (a["title"], a["start"], end)
                self.created_appts.append(i)
                return f"added #{i}"
            if name == "move_appointment":
                t, s, e = self.appts[str(a["id"]).lstrip("#")]
                ns = datetime.fromisoformat(a["new_start"])
                ne = ns + (datetime.fromisoformat(e) - datetime.fromisoformat(s))
                self.appts[str(a["id"]).lstrip("#")] = (t, ns.isoformat(timespec="minutes"), ne.isoformat(timespec="minutes"))
                return f"moved #{str(a['id']).lstrip('#')} to {ns.isoformat(timespec='minutes')}"
            if name == "add_task":
                self.n += 1
                self.tasks[f"t{self.n}"] = {"title": a["title"], "due": a.get("due") or "", "done": False}
                return f"added #t{self.n}"
            if name == "cancel":
                i = str(a["id"]).lstrip("#")
                if i in self.appts:
                    del self.appts[i]
                elif i in self.tasks:
                    del self.tasks[i]
                else:
                    return f"error: no #{i}"
                self.cancelled.append(i)
                return f"cancelled #{i}"
        except (KeyError, ValueError) as e:
            return f"error: {type(e).__name__}: {e}"
        return f"error: unknown tool {name}"


class Office(Life):
    """Life plus notes, a ledger and a rule that appointments may not overlap: the world of the multi-agent tasks."""

    def __init__(self):
        super().__init__()
        self.appts |= {"a6": ("Project review", "2026-11-05T16:00", "2026-11-05T17:00"),
                       "a7": ("Call with Marco", "2026-11-05T11:00", "2026-11-05T11:30"),
                       "a8": ("Workshop", "2026-11-06T14:00", "2026-11-06T15:30")}
        self.start_appts = dict(self.appts)
        self.notes: list[str] = []
        self.tx = [("2026-10-03", "restaurants", "Trattoria Roma", 42.50), ("2026-10-09", "groceries", "Conad", 120.10),
                   ("2026-10-12", "restaurants", "Sushi Zen", 38.00), ("2026-10-15", "transport", "Trenitalia", 55.00),
                   ("2026-10-19", "groceries", "Esselunga", 98.30), ("2026-10-22", "restaurants", "Pizzeria Da Gigi", 27.90),
                   ("2026-10-27", "groceries", "Coop", 94.00), ("2026-10-30", "restaurants", "Osteria del Borgo", 61.20),
                   ("2026-11-02", "restaurants", "Bar Centrale", 20.00), ("2026-11-03", "groceries", "Conad", 31.00)]

    TOOLS = Life.TOOLS + [
        tool("save_note", "Save a free-text note.", {"text": "the note"}),
        tool("recall", "Search saved notes.", {"query": "words to look for"}),
        tool("transactions", "The user's card transactions of one month, optionally one category (restaurants, groceries, transport).",
             {"month": "YYYY-MM", "category": "category or empty"}, ["month"]),
    ]

    def _clash(self, start: str, end: str, skip: str = "") -> str:
        for i, (t, s, e) in self.appts.items():
            if i != skip and s < end and start < e:
                return f"#{i} {t} {s[11:]}–{e[11:]}"
        return ""

    def call(self, name: str, a: dict) -> str:
        try:
            if name == "add_appointment":
                end = a.get("end") or (datetime.fromisoformat(a["start"]) + timedelta(hours=1)).isoformat(timespec="minutes")
                if hit := self._clash(a["start"], end):
                    return f"error: not added, overlaps {hit}"
            if name == "move_appointment":
                i = str(a["id"]).lstrip("#")
                _, s, e = self.appts[i]
                ns = datetime.fromisoformat(a["new_start"])
                ne = ns + (datetime.fromisoformat(e) - datetime.fromisoformat(s))
                if hit := self._clash(ns.isoformat(timespec="minutes"), ne.isoformat(timespec="minutes"), i):
                    return f"error: not moved, overlaps {hit}"
            if name == "save_note":
                self.notes.append(str(a["text"]))
                return "saved"
            if name == "recall":
                words = str(a.get("query", "")).lower().split()
                return "\n".join(n for n in self.notes if any(w in n.lower() for w in words)) or "no matching notes"
            if name == "transactions":
                cat = str(a.get("category") or "").lower()
                rows = [f"{d} {c} {m} {amt:.2f}" for d, c, m, amt in self.tx
                        if d.startswith(str(a["month"])) and (not cat or c == cat)]
                return "\n".join(rows) or "no transactions"
        except (KeyError, ValueError) as e:
            return f"error: {type(e).__name__}: {e}"
        return super().call(name, a)


AGENTS = {"calendar": {"now", "agenda", "add_appointment", "move_appointment", "cancel"},
          "tasks": {"now", "agenda", "add_task", "cancel"}, "notes": {"save_note", "recall"}, "finance": {"transactions"}}
DELEGATE = tool("delegate", "Give a self-contained request to a specialised agent and get its answer. Agents: calendar "
                "(appointments: agenda, add, move, cancel), tasks (to-do list: read, add, cancel), notes (save and recall "
                "free-text notes), finance (the user's card transactions by month and category).",
                {"agent": "calendar, tasks, notes or finance",
                 "request": "what to do, with exact dates and every detail: the agent does not see this conversation"})
ORCH = ("You coordinate specialised agents for the user's personal assistant. You cannot read or change anything yourself: "
        "use the delegate tool, one self-contained request at a time, with exact dates (never 'tomorrow'). Today is "
        "Wednesday 2026-11-04, local time. Check what the agents answer, retry when one reports an error, and finish with "
        "a short reply to the user stating what was done and any figures asked for.")
SUB = ("You are the {name} agent of a personal assistant. Do the request with your tools and answer with a short factual "
       "result (ids, times, amounts). If a tool call fails, fix the problem and retry; if that is impossible, say why. "
       "Do not do anything beyond the request.")
