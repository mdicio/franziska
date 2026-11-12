"""A week of the user's life against the real bot, in-process (CHANGELOG §1). Run on demand, not in CI.

The bot is the real one (router, agents, guards, jobs) on the configured local model, with fake Telegram objects,
fresh stores in a scratch directory, Google and gog off, and a simulated clock that walks Monday 2 to Sunday
8 November 2026. Each step is a message, a job firing (brief, evening) or a button tap; the reminder job fires
before every step, as the 60 s job would have. Steps are judged on **state** (tasks, notes, bills, cash, memory
files), on the route, and on a few reply patterns. Replies are not graded for style.

    uv run --env-file .env python -m evals.week              # the whole week (~10-15 min on gemma4:12b)
    uv run --env-file .env python -m evals.week --until wed  # Monday to Wednesday
    uv run --env-file .env python -m evals.week --list       # the steps, without running anything
    uv run --env-file .env pytest -m week                    # the same, as an opt-in test that fails on any miss

It holds the GPU (gpu.hold), so the live bot answers "busy" meanwhile. Results: evals/runs/week-<time>/
(`steps.jsonl` with every reply and tool call, `summary.md`). A step marked `known` is a failure we already know
about; it is reported but doesn't fail the test.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from franziska import bot as botmod
from franziska import core, gpu, guards
from franziska.config import ROOT, Settings
from franziska.usage import UsageLog

MONDAY = datetime(2026, 11, 2)
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
USER = 42


# ---- steps --------------------------------------------------------------------------------------------------------
@dataclass
class Check:
    what: str
    fn: Callable[[World, Out], bool]


@dataclass
class Step:
    at: str  # 'mon 08:10', or 'd12 08:10': 12 days after MONDAY (a month)
    checks: list[Check]
    route: str | None = None  # expected route of a message ('life', 'money', 'chat', a workflow name)
    known: str = ""  # a known failure: reported, doesn't fail the run

    @property
    def when(self) -> datetime:
        day, hm = self.at.split()
        h, m = map(int, hm.split(":"))
        days = int(day[1:]) if day.startswith("d") else DAYS.index(day)
        return MONDAY + timedelta(days=days, hours=h, minutes=m)


@dataclass
class Say(Step):
    text: str = ""


@dataclass
class Job(Step):
    job: str = ""  # 'brief' | 'evening' | 'review' | 'expire' | 'consolidate', or a name for `run`
    run: Callable[[World], None] | None = None  # an eval's own job (seed a store mid-run), instead of a bot one


@dataclass
class Tap(Step):
    message: str = ""  # regex on the text of a message the bot sent (the latest match is tapped)
    button: str = ""  # regex on the button's label


@dataclass
class Cmd(Step):
    text: str = ""  # '/memory scarpe': a command and its args, answered without the router


@dataclass
class Doc(Step):
    name: str = ""  # a file the user sends (bot.on_document), with an optional caption
    data: bytes = b""
    mime: str = ""
    caption: str = ""


def say(at, text, route=None, *checks, known=""):
    return Say(at, list(checks), route, known, text=text)


def cmd(at, text, *checks, known=""):
    return Cmd(at, list(checks), known=known, text=text)


def job(at, name, *checks, run=None):
    return Job(at, list(checks), job=name, run=run)


def doc(at, name, data, route=None, *checks, mime="", caption="", known=""):
    return Doc(at, list(checks), route, known, name=name, data=data, mime=mime, caption=caption)


def tap(at, message, button, *checks, known=""):
    return Tap(at, list(checks), known=known, message=message, button=button)


# ---- checks -------------------------------------------------------------------------------------------------------
def _fields(row: dict, want: dict) -> bool:
    for k, v in want.items():
        got = row.get(k)
        if k in ("due_at", "remind_at", "starts_at"):  # prefix: '2026-11-06' matches a day or a time on it
            options = v if isinstance(v, tuple) else (v,)
            if not any((got is None) if o is None else (got or "").startswith(o) for o in options):
                return False
        elif k == "done":
            if bool(row["done_at"]) != v:
                return False
        elif got != v:
            return False
    return True


def task(pattern: str, **want) -> Check:
    """The latest task or event whose title matches (of that kind, when `kind` is wanted) has these fields (dates by
    prefix; done=True/False)."""
    return Check(f"/{pattern}/ {want}",
                 lambda w, o: (r := w.item(pattern, want.get("kind"))) is not None and _fields(r, want))


def count(pattern: str, n: int) -> Check:
    return Check(f"{n} item(s) /{pattern}/", lambda w, o: len(w.items(pattern)) == n)


def note(pattern: str) -> Check:
    return Check(f"note /{pattern}/", lambda w, o: bool(w.rows("SELECT subject || ' ' || body AS t FROM notes",
                                                                 pattern=pattern)))


def cash(cents: int, on: str) -> Check:
    return Check(f"cash {cents / 100:+.2f} € on {on}",
                 lambda w, o: bool(w.ledger("SELECT 1 FROM transactions WHERE account = 'cash' AND amount_cents = ? "
                                            "AND booked_on = ?", (cents, on))))


def bill(pattern: str, cents: int) -> Check:
    return Check(f"bill /{pattern}/ {cents / 100:.2f} €",
                 lambda w, o: any(re.search(pattern, r["payee"], re.I) and r["amount_cents"] == cents
                                  for r in w.ledger("SELECT payee, amount_cents FROM bills")))


def memory(scope: str, pattern: str, present: bool = True) -> Check:
    def fn(w, o):
        hit = any(re.search(pattern, f"{e.name} {e.description} {e.body}", re.I) for e in w.bot.memory.entries([scope]))
        return hit == present
    return Check(f"{'' if present else 'no '}{scope} memory /{pattern}/", fn)


def reply(pattern: str, present: bool = True) -> Check:
    return Check(f"reply should {'have' if present else 'not have'} /{pattern}/",
                 lambda w, o: bool(re.search(pattern, "\n".join(o.replies), re.I)) == present)


def button(pattern: str) -> Check:
    return Check(f"button /{pattern}/", lambda w, o: any(re.search(pattern, b, re.I) for b in o.buttons))


def no_writes() -> Check:
    return Check("nothing changed", lambda w, o: o.before == w.fingerprint())


def either(*checks: Check) -> Check:
    return Check(" or ".join(c.what for c in checks), lambda w, o: any(c.fn(w, o) for c in checks))


# ---- the week -----------------------------------------------------------------------------------------------------
# Expected values follow the rules in the code: a day-only deadline is a bare date and reminds at 08:30 on the due
# day, a timed one 15 min before; an undated high-priority task nags every 2 hours from now. Wording mixes Italian,
# English, typos and follow-ups.
WEEK: list[Step] = [
    # Monday
    job("mon 07:30", "brief", reply(r"\S")),
    say("mon 08:10", "ricordami di pagare il bollo auto entro venerdì", "life",
        task("bollo", kind="task", due_at="2026-11-06", done=False), count("bollo", 1)),
    say("mon 08:12", "ricordmi di chiamre il comercialista domani mattina", "life",
        task("comm?ercialista", due_at="2026-11-03")),
    say("mon 08:13", "anzi, dopodomani", "life",
        task("comm?ercialista", due_at="2026-11-04"), count("comm?ercialista", 1)),
    say("mon 09:00", "dentista giovedì alle 15:30", "life",
        task("dentist", kind="event", starts_at="2026-11-05T15:30")),
    say("mon 12:30", "devo rinnovare la carta d'identità prima o poi", "life",
        task("carta d.identit|identity card", due_at=None, done=False)),
    say("mon 12:31", "è urgente", "life",
        task("carta d.identit|identity card", priority="high"), count("carta d.identit|identity card", 1)),
    say("mon 18:00", "ho speso 25 euro dal barbiere, in contanti", "money", cash(-2500, "2026-11-02")),
    say("mon 18:02", "quanto ho speso in contanti questa settimana?", "money", reply(r"25"), no_writes()),
    job("mon 20:00", "evening", button(r"carta|identit"), button(r"Plan tomorrow")),  # high priority shows; bollo not yet
    tap("mon 20:01", r"Evening", r"Plan tomorrow", reply(r"comm?ercialista|carta|bollo"), no_writes()),
    # Tuesday
    tap("tue 08:31", r"⏰.*(carta|identit)", r"\+1h", task("carta d.identit|identity card", remind_at="2026-11-03T09:3")),
    say("tue 10:00", "fatto la carta d'identità! ho preso appuntamento in comune per il 20 novembre alle 9", "life",
        task("carta d.identit|identity card", kind="task", done=True),
        task("comune|anagrafe|carta|identit", kind="event", starts_at="2026-11-20T09:00")),
    say("tue 11:00", "ricordati che il codice del cancello della cantina è 4471", "life",
        note("4471"), memory("life", "4471", present=False)),
    # a payment is a task and a bill, whichever route gets it (life has add_bill too)
    say("tue 19:00", "devo pagare la rata del condominio di 410 euro entro il 1 dicembre", None,
        bill("condomin", 41000), task("condomin", due_at="2026-12-01"), count("condomin", 1)),
    # Wednesday
    say("wed 09:00", "sposta il dentista a venerdì stessa ora", "life",
        task("dentist", starts_at="2026-11-06T15:30"), count("dentist", 1)),
    say("wed 09:05", "qual è il codice della cantina?", "life", reply(r"4471"), no_writes()),
    say("wed 13:00", "spiegami la differenza tra ETF e fondi comuni", "chat", no_writes(),
        known="router sends this to money (README, 2026-10-25)"),
    say("wed 18:00", "il commercialista mi ha detto di richiamarlo lunedì, rimandalo", "life",
        either(task("comm?ercialista", due_at="2026-11-09"), task("comm?ercialista", remind_at="2026-11-09")),
        count("comm?ercialista", 1)),
    # Thursday
    say("thu 08:00", "cosa ho oggi?", "life", reply(r"dentist", present=False), no_writes()),
    say("thu 10:00", "smettila di ricordarmi il bollo, lo faccio domani sera alle 21", "life",
        task("bollo", remind_at="2026-11-06T21:00", done=False)),
    say("thu 15:00", "I need to buy a birthday present for Luca by next Saturday, not urgent", "life",
        task("luca", due_at=("2026-11-07", "2026-11-14"), priority="low")),
    say("thu 15:05", "abito in via Roma 12, scala B interno 7", "life",
        memory("profile", "roma 12", present=False), button("💾")),
    tap("thu 15:06", r"", r"💾", memory("profile", "roma 12")),
    # Friday
    tap("fri 21:01", r"⏰.*bollo", r"Done", task("bollo", done=True)),
    say("fri 21:05", "ho pagato il bollo", "life", count("bollo", 1), task("bollo", done=True)),
    # Saturday
    say("sat 10:00", "cosa devo fare questo weekend?", "life", no_writes()),
    say("sat 10:05", "aiutami a pianificare la prossima settimana", "life", no_writes()),
    say("sat 10:10", "quali bollette devo ancora pagare?", "money", reply(r"410"), no_writes()),
    say("sat 17:00", "preso il regalo per Luca", "life", task("luca", done=True), count("luca", 1)),
    # Sunday
    job("sun 18:00", "review", reply(r"Weekly review"), reply(r"carta|identit")),
    job("sun 20:00", "evening", reply(r"Evening check-in")),
    say("sun 20:05", "cosa ho fatto questa settimana?", "life", reply(r"carta|identit"), reply(r"bollo"), no_writes()),
]


# ---- the fake Telegram side ---------------------------------------------------------------------------------------
@dataclass
class Sent:
    text: str
    markup: object = None

    @property
    def buttons(self) -> list[tuple[str, str]]:
        rows = getattr(self.markup, "inline_keyboard", None) or []
        return [(b.text, b.callback_data) for row in rows for b in row]


class Chat:
    """What the bot sent (with buttons), and fake updates to talk to it."""

    def __init__(self):
        self.sent: list[Sent] = []
        self.log: list[str] = []  # every text sent or edited, in order: a step's replies are its slice
        self.context = SimpleNamespace(chat_data={}, args=[], bot=SimpleNamespace(send_message=self._send,
                                                                                   send_document=self._document,
                                                                                   send_photo=self._photo))

    def _add(self, text: str, markup=None) -> MagicMock:
        msg = Sent(text, markup)
        self.sent.append(msg)
        self.log.append(text)

        async def edit_text(text, reply_markup=None, **_):
            msg.text, msg.markup = text, reply_markup
            self.log.append(text)

        handle = MagicMock(text=text, reply_markup=markup)
        handle.edit_text = edit_text
        return handle

    async def _send(self, chat_id, text, reply_markup=None, **_):
        return self._add(text, reply_markup)

    async def _document(self, chat_id, document, filename=None, caption=None, **_):
        return self._add(f"[file {filename}]\n{caption or ''}")

    async def _photo(self, chat_id, photo, caption=None, reply_markup=None, **_):
        return self._add(f"[file {Path(getattr(photo, 'name', 'photo')).name}]\n{caption or ''}", reply_markup)

    def update(self, text: str = "", query=None) -> MagicMock:
        u = MagicMock()
        u.effective_user.id = u.effective_chat.id = USER
        u.effective_message.text = text

        async def reply_text(text, reply_markup=None, **_):
            return self._add(text, reply_markup)

        u.effective_message.reply_text = reply_text
        u.effective_chat.send_action = AsyncMock()
        u.callback_query = query
        return u

    def document(self, name: str, data: bytes, mime: str = "", caption: str = "") -> MagicMock:
        u = self.update()
        msg = u.effective_message
        msg.photo, msg.caption = [], caption
        msg.document.file_name, msg.document.mime_type = name, mime

        async def download_to_drive(path):
            Path(path).write_bytes(data)

        msg.document.get_file = AsyncMock(return_value=SimpleNamespace(download_to_drive=download_to_drive))
        return u

    def tap(self, message: str, label: str) -> MagicMock | None:
        """A callback update for the latest message matching `message` with a button matching `label`."""
        for msg in reversed(self.sent):
            if not re.search(message, msg.text, re.I | re.S):
                continue
            for text, data in msg.buttons:
                if re.search(label, text, re.I):
                    q = MagicMock(data=data)
                    q.answer = AsyncMock()
                    q.message.text, q.message.reply_markup = msg.text, msg.markup

                    async def edit_message_text(text, reply_markup=None, msg=msg, **_):
                        msg.text, msg.markup = text, reply_markup
                        self.log.append(text)

                    async def edit_message_reply_markup(reply_markup=None, msg=msg, **_):
                        msg.markup = reply_markup

                    q.edit_message_text, q.edit_message_reply_markup = edit_message_text, edit_message_reply_markup
                    return self.update(query=q)
        return None


# ---- running it ---------------------------------------------------------------------------------------------------
@dataclass
class Out:
    replies: list[str] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    route: str | None = None
    before: tuple = ()
    guards: list[guards.Guard] = field(default_factory=list)  # one per agent run in the step


class World:
    """The bot on a scratch data dir with a simulated clock, and read access to its stores for the checks."""

    def __init__(self, settings: Settings, live: bool = False):
        self.live = live  # evals/live.py: the real clock and the real stores
        self.now = datetime.now().replace(second=0, microsecond=0) if live else MONDAY
        # The mail agent's `life` MCP server is a subprocess on its own clock: it reads the simulated one from this
        # file (mcp_server.main). Set before the Bot, whose toolsets copy the environment.
        self.clock_file = settings.data_dir / "clock"
        if not live:
            os.environ["FRANZISKA_CLOCK_FILE"] = str(self.clock_file)
            self.clock_file.write_text(self.now.isoformat())
        self.bot = botmod.Bot(settings)
        self.bot.life.clock = self.bot.money.clock = lambda: self.now
        self.bot.desktop.is_up = AsyncMock(return_value=True)
        # The router's 15-min follow-up window sees the simulated gaps; a turn's duration (turn log) the real one.
        self._real0 = time.time()
        core.clock = SimpleNamespace(time=lambda: self.now.timestamp() + time.time() - self._real0)
        self.chat = Chat()
        self.guards: list[guards.Guard] = []
        world = self

        class Recording(guards.Guard):  # bot.py builds one per agent run; keep them for the tool calls
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                world.guards.append(self)

        guards.Guard = Recording
        answer = self.bot.answer

        async def recording_answer(update, context, route, text, **kw):
            self.route = route
            await answer(update, context, route, text, **kw)

        self.bot.answer = recording_answer
        self.route = None
        self.life_db, self.ledger_db = settings.life_db, settings.ledger_db

    def rows(self, sql: str, args=(), pattern: str | None = None, db: Path | None = None) -> list[dict]:
        con = sqlite3.connect(db or self.life_db)
        con.row_factory = sqlite3.Row
        try:
            out = [dict(r) for r in con.execute(sql, args)]
        finally:
            con.close()
        return [r for r in out if re.search(pattern, r["t"], re.I)] if pattern else out

    def ledger(self, sql: str, args=()) -> list[dict]:
        return self.rows(sql, args, db=self.ledger_db)

    def items(self, pattern: str) -> list[dict]:
        return [r for r in self.rows("SELECT * FROM tasks ORDER BY id") if re.search(pattern, r["title"], re.I)]

    def item(self, pattern: str, kind: str | None = None) -> dict | None:
        found = [r for r in self.items(pattern) if kind in (None, r["kind"])]
        return found[-1] if found else None

    def fingerprint(self) -> tuple:
        return (json.dumps(self.rows("SELECT * FROM tasks ORDER BY id")),
                json.dumps(self.rows("SELECT id, subject, body FROM notes ORDER BY id")),
                json.dumps(self.ledger("SELECT * FROM bills ORDER BY id")),
                json.dumps(self.ledger("SELECT id FROM transactions ORDER BY id")))

    async def step(self, s: Step) -> Out:
        if self.live:  # no reminder job: it would mark the user's real reminders as sent to this fake chat
            self.now = datetime.now().replace(second=0, microsecond=0)
        else:
            self.now, self._real0 = s.when, time.time()
            self.clock_file.write_text(self.now.isoformat())
            await self.bot.send_reminders(self.chat.context)  # the 60 s job, caught up to now
            if self.bot.life.calendar:  # the 5-min calendar sync (real_copy runs): the mirror is never stale
                await asyncio.to_thread(self.bot.life.calendar.refresh, self.now)
        start, n_sent, n_guards = len(self.chat.log), len(self.chat.sent), len(self.guards)
        out = Out(before=self.fingerprint())
        self.route = None
        if isinstance(s, Say):
            await botmod.traced(self.bot.on_text)(self.chat.update(s.text), self.chat.context)
        elif isinstance(s, Doc):
            await botmod.traced(self.bot.on_document)(self.chat.document(s.name, s.data, s.mime, s.caption),
                                                      self.chat.context)
        elif isinstance(s, Job) and s.run:
            s.run(self)
        elif isinstance(s, Job):
            await {"brief": self.bot.send_brief, "evening": self.bot.send_evening,
                    "expire": self.bot.nightly, "review": self.bot.send_review,
                   "consolidate": self.bot.consolidate}[s.job](self.chat.context)
        elif isinstance(s, Cmd):
            name, *self.chat.context.args = s.text.lstrip("/").split()
            handler = {"today": "agenda", "week": "agenda", "tasks": "tasks_cmd", "done": "tasks_cmd",
                       "notes": "notes", "memory": "memory_cmd", "new": "new_conversation", "undo": "undo",
                       "month": "money_report", "weekly": "money_report", "budget": "money_report",
                       "subs": "money_report", "bills": "money_report", "mail": "mail_cmd"}[name]
            try:
                await botmod.traced(getattr(self.bot, handler))(self.chat.update(s.text), self.chat.context)
            finally:
                self.chat.context.args = []
        else:
            update = self.chat.tap(s.message, s.button)
            if update is None:
                out.replies = [f"(no message /{s.message}/ with a button /{s.button}/ to tap)"]
                return out
            await botmod.traced(self.bot.on_button)(update, self.chat.context)
        out.replies = self.chat.log[start:]
        out.buttons = [b for m in self.chat.sent[n_sent:] for b, _ in m.buttons]
        out.route = self.route
        out.guards = self.guards[n_guards:]
        return out


STEP_TIMEOUT_S = 90  # one message; a slower step is a failure (the user would have given up)
BUDGET_S = 20 * 60  # the whole run; past it the remaining steps are skipped, not run


def run(until: str = "sun", model: str = "", log=print, steps: list[Step] | None = None, name: str = "week",
        budget_s: float = BUDGET_S, step_timeout_s: float = STEP_TIMEOUT_S, setup=None,
        live: bool = False, cloud: str = "", real_copy: bool = False, claude: str = "",
        claude_routes: frozenset[str] = frozenset({"all"})) -> list[dict]:
    """The steps in order; returns one row per step (also written to evals/runs/<name>-<time>/).

    `setup(world)` runs once before the first step (e.g. seed memory files). Time is capped twice: a step past
    `step_timeout_s` fails, and once the run has spent `budget_s` the remaining steps are reported as skipped.
    `live`: the real stores, Google and gog as configured, the real clock (evals/live.py); `until` is ignored.
    `real_copy`: a scratch copy of the real stores, Google and gog as configured, the simulated clock
    (evals/realweek.py). Calendar writes are real: the caller removes them.
    `cloud`: 'openrouter:<id>' or 'deepinfra:<id>' answers on every route (the router stays local). Synthetic data
    only: a live run follows FRANZISKA_CLOUD_ROUTES.
    `claude`: a Claude model on the subscription ('sonnet', 'haiku', …) for `claude_routes` (default every route
    llm.CLAUDE_OK allows; money, mail and the workflows stay local). The router stays local.
    """
    if cloud and live:
        raise ValueError("cloud on every route is for synthetic data only; live runs follow FRANZISKA_CLOUD_ROUTES")
    base = Settings.from_env()
    tmp = base.data_dir if live else Path(tempfile.mkdtemp(prefix=f"{name}-"))
    settings = dataclasses.replace(base, data_dir=tmp, google=False, gog_account="", mail_every_min=0,
                                   allowed_ids=frozenset({USER}), model=model or base.model)
    if real_copy:
        copy_stores(base.data_dir, tmp)
        # The real sandbox venv (read-only to jobs): without it run_python is off in the copy.
        settings = dataclasses.replace(base, data_dir=tmp, mail_every_min=0, allowed_ids=frozenset({USER}),
                                       model=model or base.model, sandbox_venv=base.sandbox_venv_dir)
    if live:
        settings = dataclasses.replace(base, mail_every_min=0, allowed_ids=frozenset({USER}),
                                       model=model or base.model)
    if cloud:
        settings = dataclasses.replace(settings, cloud_model=cloud, cloud_everywhere=True)
    if claude:
        settings = dataclasses.replace(settings, claude_model=claude, claude_routes=claude_routes)
    label = f"{cloud} (cloud, every route)" if cloud else settings.model
    if settings.claude_model and settings.claude_routes:  # passed in, or FRANZISKA_CLAUDE_ROUTES from .env
        label = f"Claude {settings.claude_model} on {', '.join(sorted(settings.claude_routes))}, else {label}"
    steps = WEEK if steps is None else steps
    if not live and until != "sun":
        steps = [s for s in steps if DAYS.index(s.at.split()[0]) <= DAYS.index(until)]
    run_dir = ROOT / "evals" / "runs" / f"{name}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    if real_copy:  # spans of every step into the copy's traces.sqlite (obs.py), as the bot records them
        from franziska import obs

        obs.setup(f"{name}-eval", settings.traces_db, settings.otlp)
    rows = []
    jsonl = (run_dir / "steps.jsonl").open("w")  # row by row: a long run that dies keeps what it did
    started, began = time.time(), datetime.now()
    loop = asyncio.new_event_loop()  # one loop for the run: the mail agent's MCP servers live on it
    with gpu.hold(f"{name} eval"):
        world = World(settings, live)
        if setup:
            setup(world)
        log(f"{name} on {label}, data in {tmp}, budget {budget_s / 60:.0f} min\n")
        for s in steps:
            if time.time() - started > budget_s:
                rows.append({"at": s.at, "step": _label(s), "ok": False, "known": s.known, "skipped": True,
                             "failed": ["skipped: over the run's time budget"], "route": None, "seconds": 0,
                             "calls": [], "guard": [], "replies": []})
                jsonl.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
                log(f"⏭  {s.at}  skipped (budget)  {_label(s)}")
                continue
            t0, began_at = time.time(), datetime.now().isoformat(timespec="milliseconds")
            try:
                out = loop.run_until_complete(asyncio.wait_for(world.step(s), step_timeout_s))
                error = ""
            except TimeoutError:
                out, error = Out(), f"timeout: over {step_timeout_s:.0f} s"
            except Exception as e:  # a crash is a failed step, not a failed run
                out, error = Out(), f"{type(e).__name__}: {e}"
            failed = [c.what for c in s.checks if not _safe(c, world, out)]
            if s.route and out.route != s.route:
                failed.insert(0, f"route {out.route} (expected {s.route})")
            if error:
                failed.insert(0, error)
            calls = [(c.name, c.ok, c.args[:2000], (c.result or "")[:1500]) for g in out.guards for c in g.calls]
            stops = [g.reason for g in out.guards if g.reason]
            retries = [r for g in out.guards for r in g.retries]  # what the model was sent back for (not stops)
            row = {"at": s.at, "step": _label(s), "ok": not failed, "known": s.known, "failed": failed,
                   "route": out.route, "seconds": round(time.time() - t0, 1), "calls": calls, "guard": stops,
                   "retries": retries,
                   "replies": out.replies, "buttons": out.buttons, "real": [began_at, datetime.now().isoformat(timespec="milliseconds")]}
            rows.append(row)
            jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
            jsonl.flush()
            mark = "✅" if row["ok"] else ("⚠️" if s.known else "❌")
            log(f"{mark} {s.at}  {row['seconds']:5.1f}s  {out.route or '':6} {_label(s)}")
            for f in failed:
                log(f"      ✗ {f}")
            for g in stops:
                log(f"      🛡 {g}")
            if not row["ok"]:
                log("      ↳ " + " | ".join(out.replies)[:300].replace("\n", " "))
    jsonl.close()
    text = summary(rows, label, name, world.bot.models.usage, began)
    (run_dir / "summary.md").write_text(text + "\n")
    log("\n" + text + f"\n\n{run_dir}")
    return rows


def copy_stores(src: Path, dst: Path) -> None:
    """The real data dir's stores (SQLite through the backup API: they are open in the bot, in WAL mode), memory
    files and Google token into `dst`. Caches and report PDFs stay behind."""
    for db in src.glob("*.sqlite"):
        with sqlite3.connect(db) as a, sqlite3.connect(dst / db.name) as b:
            a.backup(b)
    for name in ("memory", "instructions"):  # instructions: the user's edited rules, which production runs on
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name)
    for f in src.glob("*.json"):
        shutil.copy2(f, dst / f.name)


def _safe(c: Check, w: World, o: Out) -> bool:
    try:
        return bool(c.fn(w, o))
    except Exception:
        return False


def _label(s: Step) -> str:
    if isinstance(s, (Say, Cmd)):
        return s.text
    if isinstance(s, Job):
        return f"[{s.job} job]"
    if isinstance(s, Doc):
        return f"[{s.name}]" + (f" {s.caption}" if s.caption else "")
    return f"[tap /{s.button}/ on /{s.message}/]"


def summary(rows: list[dict], model: str, name: str = "week", usage: UsageLog | None = None,
            since: datetime = datetime.min) -> str:
    said = [r for r in rows if not r["step"].startswith(("[", "/")) and not r.get("skipped")]
    unexpected = [r for r in rows if not r["ok"] and not r["known"]]
    lines = [f"| {name} on {model} | |", "|---|---|",
             f"| steps passed | {sum(r['ok'] for r in rows)}/{len(rows)} |",
             f"| skipped (time budget) | {sum(bool(r.get('skipped')) for r in rows)} |",
             f"| total seconds | {sum(r['seconds'] for r in rows):.0f} |",
             f"| unexpected failures | {len(unexpected)} |",
             f"| known failures | {sum(not r['ok'] and bool(r['known']) for r in rows)} |",
             f"| guard stops | {sum(bool(r['guard']) for r in rows)} |",
             f"| rejected tool calls | {sum(not c[1] for r in rows for c in r['calls'])} |"]
    if said:
        lines.append(f"| median seconds per message | {statistics.median(r['seconds'] for r in said):.1f} |")
        lines.append(f"| max seconds per message | {max(r['seconds'] for r in said):.1f} |")
    if usage and (totals := usage.totals(since)):
        cost = sum(t[5] or 0 for t in totals)
        lines.append(f"| cloud requests · tokens in / out | {sum(t[2] for t in totals)} · "
                     f"{sum(t[3] for t in totals) / 1000:.0f}k / {sum(t[4] for t in totals) / 1000:.1f}k |")
        lines.append(f"| cloud cost (as billed) | ${cost:.3f} |")
    return "\n".join(lines)


def failures(rows: list[dict]) -> str:
    return "\n".join(f"{r['at']} {r['step']}: {'; '.join(r['failed'])}" for r in rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--until", default="sun", choices=DAYS, help="last day to run (state carries over, so from Monday)")
    p.add_argument("--model", default="", help="override FRANZISKA_MODEL for this run")
    p.add_argument("--cloud", default="", help="'openrouter:<id>' or 'deepinfra:<id>' on every route (synthetic data)")
    p.add_argument("--claude", default="", help="a Claude model on the subscription ('sonnet', 'haiku', …)")
    p.add_argument("--claude-routes", default="all", help="routes on Claude (llm.CLAUDE_OK; default: all of them)")
    p.add_argument("--list", action="store_true", help="print the steps and their checks, run nothing")
    p.add_argument("--budget-min", type=float, default=BUDGET_S / 60, help="stop starting steps after this long")
    args = p.parse_args()
    if args.list:
        for s in WEEK:
            print(f"{s.at}  {s.route or '':6} {_label(s)}" + (f"   (known: {s.known})" if s.known else ""))
            for c in s.checks:
                print(f"            · {c.what}")
        return
    rows = run(args.until, args.model, budget_s=args.budget_min * 60, cloud=args.cloud, claude=args.claude,
               claude_routes=frozenset(r.strip() for r in args.claude_routes.split(",") if r.strip()))
    sys.exit(1 if any(not r["ok"] and not r["known"] for r in rows) else 0)


if __name__ == "__main__":
    main()
