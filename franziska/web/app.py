"""The web UI: Franziska as an app (ROADMAP phase 3), a second adapter over the same core as the Telegram bot.

Run: uv run --env-file .env python -m franziska.web   (then http://127.0.0.1:8765)

What chat is bad at gets its own view: Today (agenda + open tasks to tick off), Money (balances and the month's
reports), Memory (what the agents learned, to delete). The chat is the bot's conversation logic (core.py): the same
routing, agents, guards, memory, turn log and ➕/💾 proposals. One user, one conversation; it is separate from the
Telegram chat's (its own history, chat id WEB_CHAT in the turn log).

A message's answer is streamed as NDJSON lines: `status` lines while it routes, runs a workflow or wakes the
desktop, then one `reply` with the text and its actions (buttons). Reads come straight from the services; the
few writes (✅ a task, undo, 🗑 a memory) call the same service methods as the bot's buttons.

Access: with FRANZISKA_WEB_TOKEN, a cookie set once by /login?token=…; without it, only a browser on this machine
(no proxy in between). The model still runs only where the bot's does; desktop off → the same choices as Telegram.
Reminders and the brief stay in Telegram for now (no push from here yet).

/world is the same chat in a 3D landscape (static/world/, docs/WEB.md §8): it reads only /api/world (who is on the
team, a few counts each) and the `route` on status lines (who is working now).

Data (/api/data) browses every SQLite store in the data dir, read-only (opened with mode=ro), plus the report PDFs
and the nightly backups. A file dropped on the page (/api/files) takes the bot's path: a bank or DEGIRO export is
imported, anything else is read on this machine and goes to an agent as untrusted text (Core.receive / document).

/sql/ is Datasette and /phoenix/ the Phoenix trace UI, both behind the same sign-in (web/tools.py). /api/context
lists the files the agents work from: what they learned (memory/*.md), the index of it each one gets, and their
standing instructions.

Workflows run from a form too (/api/workflows): the page draws it from the workflow's Params, so a new workflow or
param needs no change there. A run is a turn of the same conversation, logged as `/<name> key=value…`; `/<name>
words` typed in the chat runs it like the Telegram command.
"""

import asyncio
import contextlib
import httpx
import hmac
import json
import logging
import re
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from franziska import backup, code, instructions, memory, obs, workflows
from franziska.config import Settings
from franziska.core import DOCUMENT_REQUEST, Core, Reply
from franziska.finance.service import eur
from franziska.secretary.service import LifeError
from franziska.secretary.tasks import iso
from franziska.web import control, onboarding, tools

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
WEB_CHAT = 0  # the turn log's chat id for the web conversation (Telegram ids are positive)
COOKIE = "franziska"
HISTORY_SHOWN = 40  # turns reloaded into the chat view
WEEK_DAYS = 7
MODES = ("auto", "life", "money")
WORLD_DAYS = 7  # the world view shows how busy each assistant was over this many days
DATA_ROWS = 50  # a page of a table in the data browser
UPLOAD_MAX = 20 * 1024 * 1024  # bytes, as for a Telegram bot's download
RUNS_SHOWN = 3  # a workflow's latest runs, under its form
NEEDS = {"gmail": "Google sign-in (FRANZISKA_GOOGLE…)", "money": "the ledger", "market": "the market report"}
OPEN = ("/login", "/manifest.webmanifest", "/icon.svg", "/sw.js", "/static/")  # served without the cookie

Emit = Callable[[dict], None]


class Say(BaseModel):
    text: str


class Tap(BaseModel):
    id: str


class Mode(BaseModel):
    mode: str


class Rules(BaseModel):
    text: str


class ReminderRule(BaseModel):
    words: str
    at: time


class CodeJob(BaseModel):
    repo: str = ""
    request: str
    parent: int | None = None  # 💬 change: a follow-up on that job's branch


class Run(BaseModel):
    params: dict = {}  # from the form; validated against the workflow's Params


class Web:
    """The web adapter: one conversation's state (the same dict keys as Telegram's chat_data) over the core."""

    def __init__(self, core: Core):
        self.core = core
        self.state: dict = {}
        self.lock = asyncio.Lock()  # one turn at a time: a second tab waits instead of sharing history mid-run
        self.jobs: set[asyncio.Task] = set()  # a turn finishes (and is logged) even if the browser goes away

    # ---- chat -------------------------------------------------------------------------------------
    def stream(self, job: Callable[[Emit], Awaitable[None]], name: str, text: str = "") -> StreamingResponse:
        """Run `job` under the conversation lock, streaming what it emits as NDJSON lines. It is one run (obs.root),
        `name`d by what started it."""
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(event: dict | None) -> None:  # from the event loop or a workflow's worker thread
            loop.call_soon_threadsafe(queue.put_nowait, event)

        async def run() -> None:
            try:
                with obs.root(name, text=text[:200]):
                    async with self.lock:
                        await job(emit)
            except Exception as e:
                log.exception("web turn failed")
                emit(self.event(Reply(f"Something went wrong on my side ({type(e).__name__}: {e}).",
                                      stopped=f"error: {type(e).__name__}")))
            finally:
                emit(None)

        task = asyncio.create_task(run())
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)

        async def lines():
            while (event := await queue.get()) is not None:
                yield json.dumps(event, ensure_ascii=False) + "\n"
        return StreamingResponse(lines(), media_type="application/x-ndjson")

    def event(self, reply: Reply) -> dict:
        root = self.core.settings.sandbox_results_dir.resolve()

        def link(path: Path) -> str:
            try:
                relative = path.resolve().relative_to(root)
            except ValueError:
                return f"/api/reports/{quote(path.name)}"
            return f"/api/artifacts/{quote(str(relative))}"

        def shown(p: Path) -> str:  # a chart inline (app.js `rich`), anything else as a link
            image = p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and "/api/artifacts/" in link(p)
            return f"\n\n![{p.name}]({link(p)})" if image else f"\n\n[📄 {p.name}]({link(p)})"

        text = reply.text + "".join(shown(p) for p in reply.files)
        label = self.label(reply.route)
        if reply.handed_from:  # another assistant passed it on
            label = f"{self.label(reply.handed_from)} → {label}"
        return {"type": "reply", "text": text, "route": reply.route, "label": label,
                "actions": [{"id": a.id, "label": a.label} for a in reply.actions], "stopped": reply.stopped,
                "steps": reply.steps, "tokens": reply.tokens, "window": self.core.context_window()}

    def label(self, route: str | None) -> str | None:
        return "☁️ ChatGPT" if route == "cloud" else self.core.labels.get(route) if route else None

    async def say(self, text: str, emit: Emit) -> None:
        """A message: the desktop must be up, then the pinned route or the router's pick, then the turn."""
        core = self.core
        if command := re.match(r"/([A-Za-z_]+)(?:\s+|$)", text):  # '/mail_bills condominio': Telegram's command
            name, words = command[1], text[command.end():]
            if wf := core.workflows.get(name.lower()):
                await self.run(wf, emit, words)
            else:
                emit(self.event(Reply(f"There's no /{name}. Workflows: "
                                      + (", ".join(f"/{n}" for n in core.workflows) or "none (they need the router).")
                                      + "\nThe rest is plain words.")))
            return
        if not await core.desktop.is_up():
            emit(self.event(self.desktop_off(text)))
            return
        if core.mode(self.state) == "auto":
            emit({"type": "status", "text": "Choosing an assistant…"})
        choice = await core.choose(self.state, text)
        if choice.route is None:
            emit(self.event(core.ask_route(self.state, text, choice.options)))
            return
        await self.answer(choice.route, text, emit, choice.p)

    async def answer(self, route: str, text: str, emit: Emit, p: float | None = None, params=None,
                     said: str | None = None, tainted: bool = False) -> None:
        core = self.core
        if route in core.workflows:
            wf = core.workflows[route]
            emit({"type": "status", "text": f"{wf.title} · starting…", "route": route})
            reply = await core.workflow_turn(WEB_CHAT, self.state, wf, text, lambda line: emit(
                {"type": "status", "text": f"{wf.title} · {line}", "route": route}), params)
        else:
            emit({"type": "status", "text": f"{core.labels.get(route, route)} · thinking…", "route": route})
            reply = await core.turn(WEB_CHAT, self.state, route, text, p, said=said, tainted=tainted)
        emit(self.event(reply))

    async def file(self, path: Path, name: str, mime: str, caption: str, emit: Emit) -> None:
        """A dropped file: imported (the report is the reply), or read and given to an agent with the caption as the
        request. The document is someone else's text: only the caption counts as said, and writes become buttons."""
        core = self.core
        emit({"type": "status", "text": f"📎 Reading {name}…"})
        got = await asyncio.to_thread(core.receive, path, name, mime)
        if got.report is not None:
            core.turns.add(WEB_CHAT, "money", f"📎 {name}", got.report)  # so the chat shows it after a reload
            emit(self.event(Reply(got.report, "money", core.actions(self.state, got.proposals))))
            return
        if not await core.desktop.is_up():  # a document may hold money or mail data: never to the cloud
            emit(self.event(Reply(f"I read {name}, but the desktop is off, so no one can look at it now. Drop it "
                                  "again when it's up.", stopped="desktop off")))
            return
        message, route = await core.document(self.state, name, got.text, caption, mime, got.data)
        await self.answer(route, message, emit, said=caption, tainted=True)

    async def run(self, wf: workflows.Workflow, emit: Emit, words: str = "", params=None) -> None:
        """A workflow started by hand: from its form (`params`) or as /name words. Run is an explicit tap, so a
        desktop that is off is woken rather than offering ChatGPT (workflows never go to the cloud)."""
        desktop = self.core.desktop
        if not await desktop.is_up():
            if not desktop.can_wake:
                emit(self.event(Reply("This runs on the desktop's model, which is off.", wf.name, stopped="desktop off")))
                return
            emit({"type": "status", "text": "🖥️ Waking the desktop (1–2 min)…", "route": wf.name})
            if not await desktop.wake_and_wait(self.core.settings.wake_timeout_s):
                emit(self.event(Reply("The desktop didn't come up (check Wake-on-LAN, deploy/BOX.md).", wf.name,
                                      stopped="desktop off")))
                return
        text = words if params is None else " ".join(
            [f"/{wf.name}", *(f"{k}={v}" for k, v in params.model_dump(mode="json").items() if v not in (None, ""))])
        await self.answer(wf.name, text, emit, params=params)

    def desktop_off(self, text: str) -> Reply:
        """The local model is unreachable: money waits for it; anything else may go to ChatGPT if the user taps."""
        wake = {"wake": "🖥️ Wake desktop"} if self.core.desktop.can_wake else {}
        if self.core.mode(self.state) == "money":
            return Reply("💶 The desktop (local model) is off, and money questions never go to the cloud."
                         + (" Wake it and I'll answer." if wake else " Ask again when it's up."),
                         actions=self.core.buttons(self.state, "off", text, wake) if wake else [])
        return Reply("The desktop (local model) is off. ChatGPT can answer now; it sees your schedule, calendar and "
                     "notes, never money or mail.",
                     actions=self.core.buttons(self.state, "off", text, {"cloud": "☁️ ChatGPT now", **wake}))

    async def tap(self, action: str, emit: Emit) -> None:
        """A button on a reply: which assistant, desktop off, or a proposal to apply."""
        kind, _, rest = action.partition(":")
        n, _, arg = rest.partition(":")  # '<kind>:<n>:<key>', or 'wf:<n>'
        if kind == "route":
            text = self.core.take(self.state, kind, n)
            if text is None or arg not in self.core.labels:
                emit(self.event(Reply("That question expired; send the message again.")))
                return
            await self.answer(arg, text, emit)
        elif kind == "off":
            text = self.core.take(self.state, kind, n)
            if text is None:
                emit(self.event(Reply("That question expired; send the message again.")))
            elif arg == "cloud":
                emit({"type": "status", "text": "☁️ Asking ChatGPT…", "route": "cloud"})
                emit(self.event(await self.core.cloud_turn(WEB_CHAT, self.state, text)))
            else:
                emit({"type": "status", "text": "🖥️ Waking the desktop (1–2 min)…"})
                if await self.core.desktop.wake_and_wait(self.core.settings.wake_timeout_s):
                    await self.say(text, emit)
                else:
                    cloud = [] if self.core.mode(self.state) == "money" else \
                        self.core.buttons(self.state, "off", text, {"cloud": "☁️ ChatGPT now"})
                    emit(self.event(Reply("The desktop didn't come up (check Wake-on-LAN, deploy/BOX.md).",
                                          actions=cloud)))
        elif kind == "wf":
            result = await asyncio.to_thread(self.core.apply, self.state, n)
            emit({"type": "applied", "id": action, "text": result or "That button expired; run it again."})
        else:
            raise HTTPException(400, f"unknown action {action!r}")

    def history(self) -> list[dict]:
        return [{"at": t.at, "route": t.route, "label": self.label(t.route), "text": said(t.text), "reply": t.reply}
                for t in self.core.turns.recent(WEB_CHAT, HISTORY_SHOWN)]

    # ---- views ------------------------------------------------------------------------------------
    def today(self) -> dict:
        life = self.core.life
        now = life.now()
        day0 = datetime.combine(now.date(), time())
        open_tasks = life.tasks.open_tasks()
        upcoming = life.upcoming(day0, day0 + timedelta(days=WEEK_DAYS))
        dated = {t.id for t in upcoming if getattr(t, "kind", "event") == "task"}
        synced = life.calendar.synced_at() if life.calendar else None
        return {
            "now": iso(now),
            "week": [item(t, now) for t in upcoming],
            "open": [item(t, now) for t in open_tasks if t.id not in dated],
            "done": [t.title for t in life.tasks.done(day0)],
            "calendar_synced": synced,
        }

    def money(self, month: str | None) -> dict:
        money = self.core.money
        try:
            summary, budgets = money.month_summary(month), money.budget_status(month)
        except ValueError as e:  # MoneyError: a bad month
            raise HTTPException(400, str(e)) from e
        return {
            "month": month or money.today().strftime("%Y-%m"),
            "balances": [{"account": a, "on": d.isoformat(), "eur": eur(c)} for a, d, c in money.balances()],
            "freshness": money.freshness(),
            "summary": summary, "budgets": budgets, "subs": money.recurring_text(), "bills": money.bills(),
        }

    def workflows(self) -> list[dict]:
        """Every workflow with its form, whether it can run here (and what it lacks), and its latest runs."""
        core, out = self.core, []
        for wf in workflows.ALL:
            missing = [NEEDS.get(n, n) for n in wf.needs if getattr(core.ctx, n) is None]
            if not core.router:
                missing.insert(0, "the router (FRANZISKA_ROUTER=1)")
            out.append({"name": wf.name, "title": wf.title, "about": about(wf.description), "fields": fields(wf.params),
                        "ready": wf.name in core.workflows, "missing": missing,
                        "runs": core.turns.runs(wf.name, RUNS_SHOWN)})
        return out

    def glance(self, key: str) -> list[str]:
        """A few counts for a member's card in the world: how its corner looks, never what is in it."""
        core = self.core
        if key == "life":
            life = core.life
            now = life.now()
            ahead = life.upcoming(now, datetime.combine(now.date(), time.max))
            late = sum(1 for t in life.tasks.open_tasks() if t.kind == "task" and t.due_end and t.due_end < iso(now))
            timed = next((t for t in ahead if not getattr(t, "all_day", False) and t.when[11:16] != "23:59"), None)
            return [(f"{len(ahead)} left today" if ahead else "nothing left today") + (f", next at {timed.when[11:16]}" if timed else ""),
                    *([f"{late} overdue"] if late else [])]
        if key == "money":
            soon, guesses = len(core.money.expected(7)), len(core.money.review(10_000))
            return [f"{soon or 'no'} charge{'' if soon == 1 else 's'} expected this week",
                    *([f"{guesses} payee groups to confirm"] if guesses else []),
                    *(["bank data is old: send an export"] if core.money.freshness() else [])]
        if key in core.workflows:
            last = core.turns.runs(key, 1)
            if not last:
                return ["never run"]
            r = last[0]
            return [f"last run {r['at'].replace('T', ' ')}" + (f" · {max(1, round(r['ms'] / 1000))} s" if r["ms"] else "")
                    + (" · failed" if r["stopped"] else "")]
        return []

    def world(self) -> dict:
        """The 3D view's cast, at the level of who does what: Franziska, her assistants and workflows, how busy each
        has been. The page lays out its landscape from this alone, so a new route or workflow shows up by itself."""
        core = self.core
        said = {**core.assistants, **{w.name: w.description for w in core.workflows.values()}}
        turns = core.turns.counts(WORLD_DAYS)
        members = [{"key": key, "label": core.labels.get(key, key), "kind": "workflow" if key in core.workflows
                    else "assistant", "about": about(text), "turns": turns.get(key, 0),
                    "glance": self.safe_glance(key)}
                   for key, text in said.items()]
        return {"lead": {"name": "Franziska", "turns": sum(turns.values())}, "members": members, "days": WORLD_DAYS}

    # ---- data: the stores as tables, read-only ---------------------------------------------------
    def stores(self) -> dict:
        """Every SQLite store with its tables and row counts, the report PDFs and the nightly backups."""
        settings, out = self.core.settings, []
        for path in sorted(settings.data_dir.glob("*.sqlite")):
            with readonly(path) as con:
                tables = [{"name": t, "kind": kind, "rows": con.execute(f"SELECT count(*) FROM {quote(t)}").fetchone()[0],
                           "columns": [c[1] for c in con.execute(f"PRAGMA table_info({quote(t)})")]}
                          for t, kind in user_tables(con).items()]
            out.append({"name": path.stem, "bytes": size(path), "modified": stamp(path), "tables": tables})
        reports = sorted(settings.reports_dir.glob("*.pdf"), key=lambda f: f.stat().st_mtime, reverse=True) \
            if settings.reports_dir.is_dir() else []
        root = backup.target(settings)
        nights = sorted((d for d in root.iterdir() if d.is_dir() and backup.DATED.match(d.name)), reverse=True) \
            if root.is_dir() else []
        return {"stores": out, "data_dir": str(settings.data_dir),
                "reports": [{"name": f.name, "bytes": f.stat().st_size, "modified": stamp(f)} for f in reports],
                "backups": {"dir": str(root), "nights": [{"name": d.name, "bytes": sum(size(f) for f in d.rglob("*")
                                                                                       if f.is_file())}
                                                          for d in nights]},
                "memory": len(self.core.memory.entries(memory.SCOPES))}

    def rows(self, store: str, table: str, q: str = "", sort: str = "", desc: bool = True, offset: int = 0) -> dict:
        """A page of one table, newest first unless sorted: `q` matches any column's text."""
        path = self.core.settings.data_dir / f"{store}.sqlite"
        if "/" in store or not path.is_file():
            raise HTTPException(404, f"no store {store!r}")
        with readonly(path) as con:
            if table not in user_tables(con):
                raise HTTPException(404, f"no table {table!r} in {store}")
            info = con.execute(f"PRAGMA table_info({quote(table)})").fetchall()
            columns, types = [c[1] for c in info], [c[2] or "" for c in info]
            where, args = "", []
            if q.strip():
                where = " WHERE " + " OR ".join(f"CAST({quote(c)} AS TEXT) LIKE ?" for c in columns)
                args = [f"%{q.strip()}%"] * len(columns)
            total = con.execute(f"SELECT count(*) FROM {quote(table)}{where}", args).fetchone()[0]
            order = f" ORDER BY {quote(sort)} {'DESC' if desc else 'ASC'}" if sort in columns else " ORDER BY rowid DESC"
            sql = f"SELECT * FROM {quote(table)}{where}{{}} LIMIT ? OFFSET ?"
            try:
                found = con.execute(sql.format(order), [*args, DATA_ROWS, max(offset, 0)]).fetchall()
            except sqlite3.OperationalError:  # a view or a WITHOUT ROWID table: no default order
                found = con.execute(sql.format(""), [*args, DATA_ROWS, max(offset, 0)]).fetchall()
        return {"columns": columns, "types": types, "total": total, "offset": max(offset, 0), "limit": DATA_ROWS,
                "rows": [[f"<{len(v)} bytes>" if isinstance(v, bytes) else v for v in r] for r in found]}

    def context(self) -> list[dict]:
        """The text the agents work from, to read: each memory file as written on disk, what of it each agent gets
        in its instructions (the index), and the agents' standing instructions (a run adds the date and tools)."""
        out, root = [], self.core.settings.memory_dir
        for path in sorted(root.glob("*/*.md")):
            out.append({"id": f"memory/{path.parent.name}/{path.name}", "group": f"Memory · {path.parent.name}",
                        "name": path.stem, "text": path.read_text(errors="replace"), "modified": stamp(path),
                        "bytes": path.stat().st_size, "kind": "markdown"})
        for scope in ("life", "money", "mail"):
            out.append({"id": f"index/{scope}", "group": "What each agent is shown", "name": f"{scope} · memory index",
                        "text": self.core.memory.index(scope) or "(nothing learned yet: the profile and this scope are empty)",
                        "kind": "text"})
        for name in instructions.DEFAULTS:  # as the agent runs them: the user's edit (Memory → Rules) if any
            out.append({"id": f"instructions/{name}", "group": "Standing instructions", "name": name,
                        "text": instructions.text(name), "kind": "text"})
        return out

    def safe_glance(self, key: str) -> list[str]:
        try:
            return self.glance(key)
        except Exception:  # a store that can't be read must not take the world down
            log.exception("world: glance %s failed", key)
            return []

    def rules(self) -> dict:
        """The agents' standing rules (instructions.py) and the reminder-time rules (tasks.reminder_rules)."""
        return {"agents": [{"name": n, "title": instructions.TITLES.get(n, n), "text": instructions.text(n),
                            "default": d, "edited": instructions.edited(n),
                            "stale": instructions.stale(n)} for n, d in instructions.DEFAULTS.items()],
                "reminders": [{"id": i, "words": w, "at": at} for i, w, at in self.core.life.tasks.rules()],
                "day_start": self.core.life.tasks.day_time().strftime("%H:%M")}

    def memories(self) -> list[dict]:
        return [{"scope": e.scope, "name": e.name, "fact": e.description, "detail": e.body, "kind": e.kind,
                 "confirmed": e.confirmed, "last_used": e.last_used.isoformat(), "source": e.source}
                for e in self.core.memory.entries(memory.SCOPES)]


@contextlib.contextmanager
def readonly(path: Path) -> Iterator[sqlite3.Connection]:
    """Opened so that the browser can't change a store, whatever the query (WAL readers don't block the bot)."""
    con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


def user_tables(con: sqlite3.Connection) -> dict[str, str]:
    """The tables and views to browse (name → 'table' | 'view'): not SQLite's own, nor full-text indexes and their
    shadow tables."""
    rows = con.execute("SELECT name, sql, type FROM sqlite_master WHERE type IN ('table', 'view')"
                       " ORDER BY type, name").fetchall()
    virtual = [n for n, sql, _ in rows if (sql or "").upper().startswith("CREATE VIRTUAL")]
    return {n: kind for n, _, kind in rows if not n.startswith("sqlite_") and n not in virtual
            and not any(n.startswith(v + "_") for v in virtual)}


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def size(path: Path) -> int:
    """A store with its WAL: what it takes on disk."""
    return sum(f.stat().st_size for f in (path, path.with_name(path.name + "-wal")) if f.exists())


def stamp(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="minutes")


def said(text: str) -> str:
    """A document turn's text is the caption plus the document: the chat shows the caption and the file's name."""
    m = re.match(r"(.*?)\n\n\[([^\]\n]+)\]\n", text, re.S)
    if not m:
        return text
    return f"📎 {m[2]}" + ("" if m[1] == DOCUMENT_REQUEST else f"\n{m[1]}")


def about(description: str) -> str:
    """'mail_bills: find bills… (when to use it)' → 'Find bills…', for people rather than the router."""
    text = human(description.partition(": ")[2] or description)
    return text[:1].upper() + text[1:]


def human(text: str) -> str:
    """The model's wording for the person reading it: 'the user' → 'you', 'null' → 'empty'."""
    for a, b in (("the user's", "your"), ("the user has", "you have"), ("the user is", "you are"),
                 ("the user was", "you were"), ("the user", "you"), ("their own", "your own")):
        text = text.replace(a, b)
    return re.sub(r"\bnull\b", "empty", text)


def fields(params: type[BaseModel]) -> list[dict]:
    """A workflow's Params as form fields: kind integer | number | string | boolean | choice | date, with the
    default, bounds, choices and the description as help."""
    schema, out = params.model_json_schema(), []
    for name, prop in schema.get("properties", {}).items():
        kinds = prop.get("anyOf", [prop])
        main = next((k for k in kinds if k.get("type") != "null"), {})
        if "$ref" in main:  # an Enum class
            main = schema["$defs"][main["$ref"].rsplit("/", 1)[1]]
        kind = "choice" if "enum" in main else "date" if main.get("format") == "date" else main.get("type", "string")
        out.append({"name": name, "label": prop.get("title", name), "help": human(prop.get("description") or "") or None, "kind": kind,
                    "options": main.get("enum"), "default": prop.get("default"), "min": main.get("minimum"),
                    "max": main.get("maximum"), "optional": any(k.get("type") == "null" for k in kinds)})
    return out


def item(t, now: datetime) -> dict:
    """A Task (local) or an Appointment (Google mirror) for the Today view."""
    if hasattr(t, "ref"):  # google.calendar.Appointment
        return {"ref": t.ref, "kind": "event", "title": t.title, "when": t.when, "end": t.ends_at,
                "all_day": t.all_day, "location": t.location, "recur": "yes" if t.recurring else None,
                "priority": "normal", "overdue": False, "google": True}
    return {"ref": str(t.id), "kind": t.kind, "title": t.title, "when": t.when, "end": t.ends_at, "all_day": False,
            "location": t.location, "recur": t.recur, "priority": t.priority, "notes": t.notes,
            "due": t.due_at, "soft": t.soft,
            "overdue": bool(t.kind == "task" and t.due_end and t.due_end < iso(now)), "google": False}


def create_app(settings: Settings, core: Core | None = None) -> FastAPI:
    web = Web(core or Core(settings))
    phoenix = tools.Phoenix(settings.otlp)
    local_model = tools.LocalModel()
    # FastAPI's own spans (one per request, the UI polls /api/status) are off: a turn, tap or upload is its own run
    # (Web.stream, obs.root).
    app = FastAPI(title="Franziska", docs_url=None, redoc_url=None, openapi_url=None,
                  telemetry={"tracing": False, "metrics": False, "logs": False, "operation_spans": False})
    app.state.web = web

    def trusted(request: Request) -> bool:
        if settings.web_token:
            token = request.cookies.get(COOKIE, "")
            return hmac.compare_digest(token.encode(), settings.web_token.encode())
        # No token: only a browser on this machine, not one behind a proxy (tailscale serve adds this header), and
        # only for a local name: a web page whose domain re-resolves to 127.0.0.1 (DNS rebinding) sends its own Host.
        host = request.client.host if request.client else ""
        name = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]")
        return (host in ("127.0.0.1", "::1") and "x-forwarded-for" not in request.headers
                and name in ("127.0.0.1", "localhost", "::1"))

    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if trusted(request) or path.startswith(OPEN):
            response = await call_next(request)
            if path.startswith("/static/"):  # revalidate (ETag): a new version shows on the next load
                response.headers["Cache-Control"] = "no-cache"
            return response
        if path.startswith("/api/"):
            return JSONResponse({"detail": "not signed in"}, status_code=401)
        return HTMLResponse(LOGIN, status_code=401)

    @app.get("/login")
    async def login_link(request: Request, token: str = ""):
        """The link to open once per device: /login?token=…"""
        return signed_in(request, token) if token else HTMLResponse(LOGIN)

    @app.post("/login")
    async def login_form(request: Request):
        form = parse_qs((await request.body()).decode())
        return signed_in(request, form.get("token", [""])[0])

    def signed_in(request: Request, token: str):
        if not settings.web_token or not hmac.compare_digest(token.encode(), settings.web_token.encode()):
            return HTMLResponse(LOGIN.replace("<!--error-->", "<p class=err>Wrong token.</p>"), status_code=401)
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, token, max_age=365 * 86400, httponly=True, samesite="strict", secure=https)
        return response

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/world")
    async def world_page():
        return FileResponse(STATIC / "world.html", headers={"Cache-Control": "no-cache"})

    for name in ("manifest.webmanifest", "icon.svg", "sw.js"):
        app.add_api_route(f"/{name}", (lambda n=name: FileResponse(STATIC / n, headers={"Cache-Control": "no-cache"})),
                          methods=["GET"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount(tools.SQL_ROOT, tools.datasette(settings.data_dir), name="sql")

    @app.get("/api/settings")
    async def settings_page():
        return await control.overview(settings)

    @app.put("/api/settings")
    async def settings_save(body: dict):
        """{env: {KEY: value}, units: {llm: {"-m": file, ...}}}: validated, written, applied by a restart you choose."""
        try:
            saved = control.save(body.get("env") or {})
            for sid, flags in (body.get("units") or {}).items():
                if sid not in control.UNITS:
                    raise ValueError(f"unknown unit {sid}")
                await control.apply_units(sid, flags)
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))
        return saved

    @app.get("/api/setup")
    async def setup_page():
        """The Setup page (onboarding.py): each piece, whether it works, what it adds, how to turn it on."""
        return await onboarding.overview()

    @app.put("/api/setup")
    async def setup_save(body: dict):
        try:
            return await asyncio.to_thread(onboarding.save, body.get("env") or {})
        except (ValueError, OSError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/setup/{action}")
    async def setup_action(action: str):
        if action not in onboarding.ACTIONS:
            raise HTTPException(404, f"unknown action {action}")
        try:
            return await onboarding.ACTIONS[action]()
        except (ValueError, OSError, httpx.HTTPError) as e:
            raise HTTPException(400, str(e) if not isinstance(e, httpx.HTTPError) else "Telegram can't be reached")

    @app.post("/api/services/{sid}/restart")
    async def service_restart(sid: str):
        try:
            return await control.restart(sid)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/telemetry")
    async def telemetry():
        return await phoenix.status()

    @app.post("/api/telemetry/start")
    async def telemetry_start():
        return await phoenix.start()

    @app.post("/api/telemetry/stop")
    async def telemetry_stop():
        return await phoenix.stop()

    @app.get("/api/local-model")
    async def local_model_status():
        return await local_model.status()

    @app.post("/api/local-model/start")
    async def local_model_start():
        return await local_model.set_running(True)

    @app.post("/api/local-model/stop")
    async def local_model_stop():
        return await local_model.set_running(False)

    @app.get(tools.PHOENIX_ROOT)
    async def phoenix_home():
        return RedirectResponse(tools.PHOENIX_ROOT + "/")

    @app.api_route(tools.PHOENIX_ROOT + "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def phoenix_proxy(request: Request, path: str):
        return await phoenix.proxy(request)

    @app.get("/api/status")
    async def status():
        core = web.core
        return {"desktop": await core.desktop.is_up(), "can_wake": core.desktop.can_wake,
                "mode": core.mode(web.state), "modes": list(MODES) if core.router else ["life", "money"],
                "labels": core.labels, "busy": web.lock.locked(),
                "google": bool(core.life.calendar), "today": core.life.now().date().isoformat(),
                "tokens": web.state.get("context"), "window": await asyncio.to_thread(core.context_window)}

    @app.get("/api/reports/{name}")
    async def report(name: str):
        """A market report PDF (behind the same sign-in as everything else)."""
        path = settings.reports_dir / name
        if "/" in name or not name.endswith(".pdf") or not path.is_file():
            raise HTTPException(404, "no such report")
        return FileResponse(path, media_type="application/pdf")

    @app.get("/api/artifacts/{path:path}")
    async def artifact(path: str):
        """One regular artifact copied out of an isolated data-code run, behind the normal web sign-in."""
        root = settings.sandbox_results_dir.resolve()
        candidate = (root / path).resolve()
        if root not in candidate.parents or not candidate.is_file() or candidate.is_symlink():
            raise HTTPException(404, "no such artifact")
        return FileResponse(candidate, filename=candidate.name)

    # ---- coding jobs (code.py): the review page --------------------------------------------------------------------
    def job_json(job: code.Job) -> dict:
        return {**{k: getattr(job, k) for k in ("id", "repo", "request", "status", "origin", "parent", "summary",
                                                "tests", "stat", "error", "steps", "created", "started", "finished",
                                                "base_branch")},
                "title": job.title, "branch": job.branch, "review": job.status in code.REVIEW,
                "running": job.status in code.OPEN, "scratch": job.repo == code.SCRATCH,
                "self": web.core.code.is_self(job)}

    @app.get("/api/code")
    async def code_jobs():
        jobs = await asyncio.to_thread(web.core.code.recent, 30)
        return {"repos": list(web.core.code.repos()), "minutes": settings.code_minutes,
                "jobs": [job_json(j) for j in jobs]}

    @app.get("/api/code/{job_id}")
    async def code_job(job_id: int):
        job = web.core.code.get(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return {**job_json(job), "diff": await asyncio.to_thread(web.core.code.diff, job_id),
                "files": [f.name for f in web.core.code.files(job)]}

    @app.get("/api/code/{job_id}/files/{name}")
    async def code_file(job_id: int, name: str):
        job = web.core.code.get(job_id)
        found = {f.name: f for f in web.core.code.files(job)} if job else {}
        if name not in found:
            raise HTTPException(404, "no such file")
        return FileResponse(found[name], filename=name)

    @app.post("/api/code")
    async def code_start(body: CodeJob):
        try:
            job = await asyncio.to_thread(web.core.code.start, body.repo, body.request, "web", body.parent)
        except code.CodeError as e:
            raise HTTPException(400, str(e))
        return job_json(job)

    @app.post("/api/code/{job_id}/{action}")
    async def code_action(job_id: int, action: str):
        """merge, discard, stop; restart (after merging a change to Franziska itself): your taps."""
        if action == "restart":
            await asyncio.create_subprocess_exec("systemctl", "--user", "restart", "--no-block",
                                                 "franziska-bot.service", "franziska-web.service")
            return {"text": "Restarting the bot and the web UI; this page reconnects in a few seconds."}
        fn = {"merge": web.core.code.merge, "discard": web.core.code.discard, "stop": web.core.code.stop}.get(action)
        if fn is None:
            raise HTTPException(404, "no such action")
        return {"text": await asyncio.to_thread(fn, job_id)}

    @app.get("/api/world")
    async def world():
        return await asyncio.to_thread(web.world)

    @app.get("/api/chat")
    async def chat_history():
        return await asyncio.to_thread(web.history)

    @app.post("/api/chat")
    async def chat(say: Say):
        text = say.text.strip()
        if not text:
            raise HTTPException(400, "empty message")
        return web.stream(lambda emit: web.say(text, emit), "message", text)

    @app.post("/api/action")
    async def action(tap: Tap):
        return web.stream(lambda emit: web.tap(tap.id, emit), "tap", tap.id)

    @app.post("/api/new")
    async def new_conversation():
        """The web chat's /new and /clear: forget the recent turns of every assistant (tasks, notes and memory stay)
        and stop showing them after a reload."""
        web.core.restore(WEB_CHAT, web.state)
        web.core.conversations.reset(WEB_CHAT, web.state)
        web.core.turns.clear(WEB_CHAT)
        return {"tokens": None}

    @app.post("/api/mode")
    async def set_mode(mode: Mode):
        if mode.mode not in MODES or (mode.mode == "auto" and not web.core.router):
            raise HTTPException(400, f"unknown mode {mode.mode!r}")
        web.state["mode"] = mode.mode
        return {"mode": mode.mode}

    @app.get("/api/workflows")
    async def workflow_list():
        return await asyncio.to_thread(web.workflows)

    @app.post("/api/workflows/{name}")
    async def workflow_run(name: str, run: Run):
        wf = web.core.workflows.get(name)
        if wf is None:
            raise HTTPException(404, f"no workflow {name!r} here")
        try:
            params = wf.params.model_validate(run.params)
        except ValidationError as e:
            raise HTTPException(400, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e
        return web.stream(lambda emit: web.run(wf, emit, params=params), "workflow form", name)

    @app.get("/api/data")
    async def data():
        return await asyncio.to_thread(web.stores)

    @app.get("/api/data/{store}/{table}")
    async def data_rows(store: str, table: str, q: str = "", sort: str = "", desc: bool = True, offset: int = 0):
        return await asyncio.to_thread(web.rows, store, table, q, sort, desc, offset)

    @app.get("/api/context")
    async def context():
        return await asyncio.to_thread(web.context)

    @app.post("/api/files")
    async def upload(file: UploadFile, caption: str = Form("")):
        """A file dropped on the page: read in full into a temporary folder (deleted after), then the stream."""
        name = Path(file.filename or "file").name or "file"
        data = await file.read(UPLOAD_MAX + 1)
        if len(data) > UPLOAD_MAX:
            raise HTTPException(413, f"{name} is over {UPLOAD_MAX // 2**20} MB.")
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / name
        path.write_bytes(data)

        async def job(emit: Emit) -> None:
            try:
                await web.file(path, name, file.content_type or "", caption.strip(), emit)
            finally:
                tmp.cleanup()
        return web.stream(job, "document", f"{name} {caption}".strip())

    @app.get("/api/today")
    async def today():
        return await asyncio.to_thread(web.today)

    @app.post("/api/tasks/{ref}/done")
    async def done(ref: str):
        try:
            return {"result": await asyncio.to_thread(web.core.life.complete, ref)}
        except LifeError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/undo")
    async def undo():
        return {"result": await asyncio.to_thread(web.core.life.undo)}

    @app.get("/api/money")
    async def money(month: str | None = None):
        return await asyncio.to_thread(web.money, month)

    @app.get("/api/memory")
    async def memories():
        return await asyncio.to_thread(web.memories)

    @app.delete("/api/memory/{scope}/{name}")
    async def forget(scope: str, name: str):
        if scope not in memory.SCOPES:
            raise HTTPException(404, f"no scope {scope!r}")
        gone = await asyncio.to_thread(web.core.memory.forget, scope, name)
        return {"result": f"Forgot {scope}/{name}." if gone else f"{scope}/{name} was already gone."}

    @app.get("/api/rules")
    async def rules():
        return await asyncio.to_thread(web.rules)

    @app.put("/api/rules/agents/{name}")
    async def save_rules(name: str, body: Rules):
        if name not in instructions.DEFAULTS:
            raise HTTPException(404, f"no agent {name!r}")
        return {"result": await asyncio.to_thread(instructions.save, name, body.text)}

    @app.post("/api/rules/reminders")
    async def add_reminder_rule(body: ReminderRule):
        try:
            return {"result": await asyncio.to_thread(web.core.life.reminder_rule, body.words, body.at)}
        except LifeError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/rules/reminders/{rule_id}")
    async def delete_reminder_rule(rule_id: int):
        return {"result": await asyncio.to_thread(web.core.life.remove_reminder_rule, rule_id)}

    @app.post("/api/desktop/wake")
    async def wake():
        if not web.core.desktop.can_wake:
            raise HTTPException(400, "FRANZISKA_DESKTOP_MAC is not set.")
        return {"up": await web.core.desktop.wake_and_wait(web.core.settings.wake_timeout_s)}

    return app


LOGIN = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Franziska</title>
<link rel=stylesheet href=/static/style.css>
<body class=login><form method=post action=/login class=card>
<div class=brand><span class=mark>F</span> Franziska</div>
<p>Paste the access token (FRANZISKA_WEB_TOKEN) once on this device.</p><!--error-->
<input name=token type=password autocomplete=current-password placeholder=Token autofocus>
<button>Sign in</button></form></body></html>"""
