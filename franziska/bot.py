"""Telegram front end. Long polling: no open ports, works behind home NAT.

Run: uv run --env-file .env python -m franziska.bot

Each message is routed by an AnyJev decision on the local model (router.py) to the secretary (schedule + notes),
finance, or plain chat; when the router is unsure, the bot asks with buttons. /life and /money pin a chat to one
assistant, /auto goes back to routing. /think sends one turn to ChatGPT through pi (schedule + notes only).
With FRANZISKA_GOG_ACCOUNT, email requests ('find the contract Luca sent', 'reply that I accept') are a route too:
the local mail agent (mail/agent.py) with Gmail over `gog mcp` (search, read, drafts, labels, archive; never sends).

No LLM in these paths: reminders (60 s job, with Done/Snooze/priority buttons; open tasks keep coming back by
priority), the morning brief (today, open tasks by priority, the week, plus charges expected this week and budget
warnings), the evening check-in (open tasks with ✅ buttons; 📋 asks the secretary to plan tomorrow), the monthly
summary on the 1st, /today /week /tasks /done /notes /memory /month /budget /subs /bills /portfolio, .xlsx imports (which also tick off bills they show paid)
and DEGIRO .csv imports (invest/). /uncat shows the model's guess
for the biggest payee whose spending group is still a guess (from /categorise), to confirm or correct with a tap.
Agents remember what they learn (memory.py): each route's agent gets its scope's memory tools and an index of the
profile and its scope; a profile fact is saved only when the user taps 💾. Learned memories stay until forgotten.
Runs on an always-on box (deploy/BOX.md): models run on the desktop's Ollama, woken over the LAN when needed.
While the desktop is off, FRANZISKA_WHEN_OFF decides: ask (buttons), cloud (ChatGPT now) or wake (and wait).
With FRANZISKA_GOOGLE=1, appointments live in Google Calendar (synced every 5 min) and new Gmail is fetched and
triaged on the local model (never the cloud); mail that needs you is pushed and listed in the brief and /mail.

Only user ids in FRANZISKA_ALLOWED_IDS get answers; anyone else is told their id and nothing more.
Message text goes to Telegram's servers (it's a cloud messenger); models and data stay on the box and desktop,
except /think and desktop-off cloud turns, which go to OpenAI (schedule, calendar and notes only, never money/mail).
"""

import asyncio
import contextlib
import dataclasses
import functools
import logging
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pydantic_ai
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from franziska import backup, catchup, code, consolidate, gpu, health, housekeeping, memory, obs, usage, voice, workflows
from franziska.config import Settings
from franziska.core import Action, Core
from franziska.finance import categories
from franziska.finance.service import eur
from franziska.mail import client as mailbox
from franziska.secretary.service import LifeError

log = logging.getLogger(__name__)

REMINDER_INTERVAL_S = 60
GROUP_REMINDERS = 3  # more due at the same moment go out as one list
TELEGRAM_MAX = 4096
CAPTION_MAX = 1024  # a photo's or file's caption
PHOTO_TYPES = (".png", ".jpg", ".jpeg", ".webp")
PHOTO_MAX = 10 * 1024 * 1024
CALENDAR_SYNC_S = 5 * 60
LIBRARY_EVERY_S = 2 * 60  # the library's vectors are filled this often, when the chat is quiet
CODE_TICK_S = 20  # finished coding jobs are announced this often
CHANGE_S = 15 * 60  # after 💬 Change on a job, the next message is the change for this long
LIBRARY_QUIET_S = 90  # ...meaning no message for this long and no turn running (the 26B also uses the CPU)
NIGHTLY_AT = time(4, 0)  # memory expiry + turn-log retention, local time; no model, so it runs on the box too
REVIEW_AT = time(18, 0)  # Sundays: the weekly review
MEMORY_LIST = 20  # /memory shows at most this many entries, each with a 🗑 button
TYPING_EVERY_S = 4.5  # Telegram shows 'typing…' for ~5 s per call
# The root span of an update (obs.root), by handler; any other handler is a command, named by what was typed.
UPDATE_SPANS = {"on_text": "message", "on_voice": "voice", "on_document": "document", "on_button": "tap"}
PLAN_PROMPT = ("Help me plan tomorrow: look at my open tasks and tomorrow's agenda, propose the 3 things to do "
               "first, and tell me what could move or drop. Don't change anything yet.")

HELP = """\
Just write: I route each message to the right assistant.
📅 appointments, to-dos, reminders, notes ('remember the gate code is 4471')
💶 bank ledger questions, cash expenses, budgets, bills to pay ('condominium 864 € due 1 Jan');
   send your bank's export (.xlsx or .csv) to import it; a bank I don't know yet is mapped once, with your OK
📈 /portfolio: DEGIRO value, cash, gains and positions; send DEGIRO's .csv exports (Transactions, Account
   statement, Portfolio) to import them
📰 /markets [weekly]: the market report as a PDF (markets, rates, news on what you hold, your portfolio's day);
   it also comes on weekdays after the brief, the weekly edition on Mondays

/today /week: agenda · /tasks: open tasks by priority · /done: finished this week · /notes: saved notes
/memory [word]: what I learned about you and how you work, with 🗑 to forget
/undo: put back the latest change to a task or appointment · /new: start the conversation afresh
🎙 voice notes work like typed messages (transcribed on this machine)
Say 'fatto X', 'X è urgente', 'sposta X a venerdì': any task detail can be changed by chat
/month [YYYY-MM] · /weekly [YYYY-MM-DD]: a week's spending · /budget · /subs: monthly charges · /bills
/categorise: group new payees · /uncat: check guesses
/mail: emails that need you (triaged on the desktop's model)
/mail_bills [what]: bills and notices in your email ('condominio, ultimi 60 giorni'), or just ask for them
🖥 this computer ('quanto spazio libero ho?', 'trova la fattura nei download e mandamela', 'il bot ha errori?'):
   moving, trashing and restarting are buttons you tap
/code [repo] <what>: a coding job in a sandbox (no network); you review the branch, then ✅ merge or 💬 change.
   /code alone lists the jobs; 'scratch' is a new empty repository for a script
/think <question>: one deeper answer from ChatGPT (sees schedule, calendar + notes, not money or mail)
✉️ email requests ('trova la mail di Luca sul preventivo e rispondi che va bene') go to the local mail agent:
   it searches, reads, drafts replies and tidies; it never sends, the draft waits in Gmail for you
/desktop: is the GPU desktop up? · /desktop wake · /usage: the cloud model's tokens and cost
/housekeeping [run]: what the nightly cleanup removed, and what the data takes on disk
/life /money: pin this chat to one assistant · /auto: back to automatic routing
/pause: stop every assistant and scheduled push (reminders go on) · /resume
/whoami: your Telegram id"""


class Bot(Core):
    """The Telegram adapter: the core's services and turns (core.py), shown as messages and inline buttons."""

    # ---- access ---------------------------------------------------------------------------------
    def allowed(self, update: Update) -> bool:
        return update.effective_user is not None and update.effective_user.id in self.settings.allowed_ids

    async def guard(self, update: Update) -> bool:
        if self.allowed(update):
            return True
        log.warning("ignored update from user id %s", update.effective_user and update.effective_user.id)
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Not authorised. Your Telegram user id is {update.effective_user.id}; "
                "add it to FRANZISKA_ALLOWED_IDS.")
        return False

    # ---- commands without an LLM ----------------------------------------------------------------
    async def whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(f"Your Telegram user id is {update.effective_user.id}.")

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update):
            await update.effective_message.reply_text(HELP)

    async def pause_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/pause, /resume: the kill switch (Core.pause)."""
        if not await self.guard(update):
            return
        command = update.effective_message.text.lstrip("/").split()[0].split("@")[0]
        await update.effective_message.reply_text(self.pause() if command == "pause" else self.resume())

    def unless_paused(self, callback, skipped=None):
        """A job that is skipped while paused, returning `skipped` (the catch-up scheduler reads it: None is done,
        so /resume brings no backlog of briefs)."""
        @functools.wraps(callback)
        async def run(context):
            if self.paused():
                log.info("paused: %s skipped", callback.__name__)
                return skipped
            return await callback(context)
        return run

    async def set_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        command = update.effective_message.text.lstrip("/").split()[0].split("@")[0]
        if command == "auto" and not self.router:
            await update.effective_message.reply_text("The router is off (FRANZISKA_ROUTER=0). Use /life or /money.")
            return
        self.restore(update.effective_chat.id, context.chat_data)
        context.chat_data["mode"] = command
        self._save(update.effective_chat.id, context.chat_data)
        text = {"auto": "Automatic routing.", "life": "📅 Pinned to Life. /auto to undo.",
                "money": "💶 Pinned to Money. /auto to undo."}[command]
        await update.effective_message.reply_text(text)

    async def agenda(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        days = 1 if update.effective_message.text.startswith("/today") else 7
        await self.reply(update, self.life.agenda(days=days) + health.footer(self.health.problems(["calendar"])))

    async def tasks_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        done = update.effective_message.text.startswith("/done")
        await self.reply(update, self.life.done_list(7) if done else self.life.open_list())

    async def notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        query = " ".join(context.args or [])
        await self.reply(update, self.life.recall(query) if query else
                         "\n".join(n.line() for n in self.life.notes.recent(20)) or "No notes yet.")

    async def memory_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/memory [word]: what the agents learned, profile first, each with a 🗑 button. No model."""
        if not await self.guard(update):
            return
        word = " ".join(context.args or []).lower()
        found = [e for e in self.memory.entries(memory.SCOPES)
                 if word in f"{e.name} {e.description} {e.body}".lower()]
        if not found:
            await update.effective_message.reply_text(
                f"Nothing learned about '{word}'." if word else "I haven't learned anything yet.")
            return
        lines, scope = [], None
        for e in found[:MEMORY_LIST]:
            if e.scope != scope:
                scope = e.scope
                lines.append(f"\n{scope}:")
            lines.append(f"• {e.name}: {e.description}" + ("" if e.confirmed else " (?)")
                         + (f" (used {e.last_used:%d %b})" if e.kind == "cache" else ""))
        if any(not e.confirmed for e in found[:MEMORY_LIST]):
            lines.append("\n(?) = guessed once, not confirmed: it goes after 30 days unused")
        if len(found) > MEMORY_LIST:
            lines.append(f"\n(+{len(found) - MEMORY_LIST} more: /memory <word> to narrow)")
        rows = [[InlineKeyboardButton(f"🗑 {e.scope}/{e.name}"[:60], callback_data=f"mem:{e.scope}:{e.name}")]
                for e in found[:MEMORY_LIST]]
        await self.reply(update, "\n".join(lines).strip(), InlineKeyboardMarkup(rows))

    async def portfolio_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.guard(update):
            await self.reply(update, self.portfolio.overview())

    async def money_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        command = update.effective_message.text.lstrip("/").split()[0].split("@")[0]
        report = {"month": lambda: self.money.month_summary(context.args[0] if context.args else None),
                  "weekly": lambda: self.money.week_summary(context.args[0] if context.args else None),
                  "budget": self.money.budget_status, "subs": self.money.recurring_text,
                  "bills": self.money.bills}[command]
        try:
            await self.reply(update, report())
        except ValueError as e:  # MoneyError: a bad month
            await update.effective_message.reply_text(str(e))

    async def uncategorised(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The biggest payee whose spending group is the model's guess: confirm it or pick another (no model call)."""
        if not await self.guard(update):
            return
        skipped = context.chat_data.setdefault("uncat_skipped", set())
        todo = [u for u in self.money.review(len(skipped) + 1) if u[0] not in skipped]
        if not todo:
            context.chat_data["uncat_skipped"] = set()
            await update.effective_message.reply_text("Every payee's group is confirmed." if not skipped else
                                                      "No more guesses to check (skipped ones will come back).")
            return
        payee, count, cents, guess = todo[0]
        ranked = [guess, *(g for g in categories.GROUPS if g != guess)]
        actions = self.buttons(context.chat_data, "cat", (payee, ranked),
                               {**{str(i): ("✅ " if i == 0 else "") + c for i, c in enumerate(ranked)}, "skip": "skip"})
        await update.effective_message.reply_text(
            f"{payee}: {count} payment{'s' if count > 1 else ''}, {eur(cents)}. The model guessed {guess}.\n"
            "Right, or which group?", reply_markup=self.keyboard(actions, per_row=2))

    # ---- messages -------------------------------------------------------------------------------
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | None = None) -> None:
        if not await self.guard(update):
            return
        text = text or update.effective_message.text
        self.restore(update.effective_chat.id, context.chat_data)
        change = context.chat_data.pop("job_change", None)
        if change and time_now() - change[1] < CHANGE_S:  # the answer to 💬 Change: a follow-up job
            await self.start_job(update, None, text, parent=change[0])
            return
        if not await self.desktop.is_up():
            await self.desktop_off(update, context, text)
            return
        if self.mode(context.chat_data) == "auto":
            await update.effective_chat.send_action(ChatAction.TYPING)
        choice = await self.choose(context.chat_data, text)
        if choice.route:
            await self.answer(update, context, choice.route, text, p=choice.p)
            return
        out = self.ask_route(context.chat_data, text, choice.options)
        await update.effective_message.reply_text(out.text, reply_markup=self.keyboard(out.actions, per_row=4))

    async def answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE, route: str, text: str,
                     p: float | None = None, said: str | None = None, tainted: bool = False) -> None:
        if route in self.workflows:
            await self.run_workflow(update, context, self.workflows[route], text)
            return
        async with self.queued(update), typing(update.effective_chat):
            out = await self.turn(update.effective_chat.id, context.chat_data, route, text, p, said=said,
                                  tainted=tainted)
        pinned = self.mode(context.chat_data) != "auto"
        route = out.route or route
        if out.handed_from:  # another assistant passed it on: say who answers
            prefix = f"{self.labels[out.handed_from]} → {self.labels[route]} · "
        else:
            prefix = "" if out.stopped or pinned or route == "life" else self.labels[route] + " · "
        if out.files:
            await self.send_files(context.bot, update.effective_chat.id, prefix + out.text, out.files,
                                  self.keyboard(out.actions))
            return
        await self.reply(update, prefix + out.text, self.keyboard(out.actions))

    # ---- coding jobs (code.py) ------------------------------------------------------------------
    async def code_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/code: the latest jobs. /code <repo> <request>: start one there. /code <request>: ask which repository.
        /code #N <change>: a follow-up on job N's branch. Your own command, so it starts without a confirmation."""
        if not await self.guard(update):
            return
        self.restore(update.effective_chat.id, context.chat_data)
        args = context.args or []
        repos = self.code.repos()
        if not args:
            jobs = self.code.recent(8)
            names = ", ".join(repos)
            text = ("\n".join(j.line() for j in jobs) or "No coding jobs yet.") + (
                f"\n\n/code <repo> <what to do> starts one (repos: {names}); /code #N <change> continues job N.")
            buttons = [Action(f"job:show:{j.id}", f"🔍 #{j.id}") for j in jobs if j.status in code.REVIEW]
            buttons += [Action(f"job:stop:{j.id}", f"⏹ Stop #{j.id}") for j in jobs if j.status in code.OPEN]
            await self.reply(update, text, self.keyboard(buttons, per_row=3))
            return
        if args[0].startswith("#") and args[0][1:].isdigit():
            await self.start_job(update, None, " ".join(args[1:]), parent=int(args[0][1:]))
        elif args[0] in repos:
            await self.start_job(update, args[0], " ".join(args[1:]))
        else:
            request = " ".join(args)
            labels = {name: ("🧪 scratch" if name == code.SCRATCH else f"📦 {name}") for name in repos}
            await update.effective_message.reply_text(
                "Which repository?", reply_markup=self.keyboard(self.buttons(context.chat_data, "code", request,
                                                                             labels), per_row=3))

    async def start_job(self, update: Update, repo: str | None, request: str, parent: int | None = None) -> None:
        try:
            job = await asyncio.to_thread(self.code.start, repo or "", request, "telegram", parent)
        except code.CodeError as e:
            await update.effective_message.reply_text(str(e))
            return
        await update.effective_message.reply_text(
            f"🛠 Job #{job.id} started on {job.repo}: {job.title}\nI'll write when it's ready to review "
            f"(at most {self.settings.code_minutes} min).",
            reply_markup=self.keyboard([Action(f"job:stop:{job.id}", "⏹ Stop")]))

    def job_buttons(self, job: code.Job) -> list[Action]:
        if job.status not in code.REVIEW:
            return []
        merge = [] if job.repo == code.SCRATCH else [Action(f"job:merge:{job.id}", "✅ Merge")]
        return merge + [Action(f"job:change:{job.id}", "💬 Change"), Action(f"job:discard:{job.id}", "🗑 Discard")]

    async def send_job(self, bot, chat_id: int, job: code.Job) -> None:
        """A finished job: the report, its buttons, then the diff (and a scratch job's files)."""
        markup = self.keyboard(self.job_buttons(job), per_row=3)
        for i in range(0, len(text := self.code.report(job)), TELEGRAM_MAX):
            last = i + TELEGRAM_MAX >= len(text)
            await bot.send_message(chat_id, text[i:i + TELEGRAM_MAX], reply_markup=markup if last else None)
        if diff := self.code.diff(job.id):
            await bot.send_document(chat_id, diff.encode(), filename=f"job-{job.id}.diff")
        if files := self.code.files(job):
            await self.send_files(bot, chat_id, f"Files of job #{job.id}", files)

    async def announce_jobs(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Finished jobs, once each, whichever process started them (/code, the web UI, a 🛠 button, the CLI)."""
        jobs = await asyncio.to_thread(self.code.finished)
        for job in jobs:
            try:
                for user_id in self.settings.allowed_ids:
                    await self.send_job(context.bot, user_id, job)
            except TelegramError:
                log.exception("could not announce job #%d; next tick", job.id)
                return
            self.code.announced(job.id)

    async def job_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, job_id: int) -> None:
        query = update.callback_query
        job = self.code.get(job_id)
        if job is None:
            await query.edit_message_text(f"There is no job #{job_id}.")
            return
        if action == "show":
            await query.edit_message_reply_markup(None)
            await self.send_job(context.bot, update.effective_chat.id, job)
            return
        if action == "change":
            if job.status not in code.REVIEW:
                await query.edit_message_reply_markup(None)
                return
            context.chat_data["job_change"] = (job_id, time_now())
            await query.edit_message_reply_markup(None)
            await context.bot.send_message(update.effective_chat.id,
                                           f"💬 What should change in job #{job_id}? Write it in your next message.")
            return
        if action == "restart":
            await query.edit_message_text(f"{query.message.text}\n\n🔄 Restarting the web UI and the bot; back in a "
                                          "few seconds.")
            await asyncio.create_subprocess_exec("systemctl", "--user", "restart", "--no-block",
                                                 "franziska-web.service", "franziska-bot.service")
            return
        fn = {"merge": self.code.merge, "discard": self.code.discard, "stop": self.code.stop}.get(action)
        if fn is None:
            return
        result = await asyncio.to_thread(fn, job_id)
        job = self.code.get(job_id)
        buttons = self.job_buttons(job)
        if action == "merge" and job.status == "merged" and self.code.is_self(job):
            buttons = [Action(f"job:restart:{job_id}", "🔄 Restart bot + web")]
        await query.edit_message_text(f"{query.message.text}\n\n{result}"[-TELEGRAM_MAX:],
                                      reply_markup=self.keyboard(buttons, per_row=3))

    # ---- workflows ------------------------------------------------------------------------------
    async def workflow_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/<workflow> [what, in words]: run it directly, without routing."""
        if not await self.guard(update):
            return
        name = update.effective_message.text.lstrip("/").split()[0].split("@")[0]
        if not await self.desktop.is_up():
            await update.effective_message.reply_text("This runs on the desktop's model, which is off. /desktop wake")
            return
        await self.run_workflow(update, context, self.workflows[name], " ".join(context.args or []))

    async def run_workflow(self, update: Update, context: ContextTypes.DEFAULT_TYPE, wf: workflows.Workflow,
                           text: str) -> None:
        """Fill the params from the message, run it in a thread with a live status line, offer its proposals."""
        async with self.queued(update):
            note = await update.effective_message.reply_text(f"{wf.title} · starting…")
            loop = asyncio.get_running_loop()

            def progress(line: str) -> None:  # called from the worker thread
                asyncio.run_coroutine_threadsafe(note.edit_text(f"{wf.title} · {line}"), loop)

            out = await self.workflow_turn(update.effective_chat.id, context.chat_data, wf, text, progress)
        if out.stopped:
            await note.edit_text(out.text)
            return
        await note.edit_text(f"{wf.title} · done.")
        if out.files:
            await self.send_files(context.bot, update.effective_chat.id, out.text, out.files,
                                  self.keyboard(out.actions))
            return
        await self.reply(update, out.text, self.keyboard(out.actions))

    @staticmethod
    async def send_files(bot, chat_id: int, caption: str, files: list[Path],
                         markup: InlineKeyboardMarkup | None = None) -> None:
        """A reply with files (a workflow's PDFs, run_python's charts): the text as the first file's caption when it
        fits, else as messages before them. Images go as photos so a chart shows in the chat; buttons go on the
        text."""
        fits = len(caption) <= CAPTION_MAX
        if not fits:
            for i in range(0, len(caption), TELEGRAM_MAX):
                last = i + TELEGRAM_MAX >= len(caption)
                await bot.send_message(chat_id, caption[i:i + TELEGRAM_MAX], reply_markup=markup if last else None)
        for i, path in enumerate(files):
            extra = {"caption": caption, "reply_markup": markup} if fits and i == 0 else {}
            if path.suffix.lower() in PHOTO_TYPES and path.stat().st_size <= PHOTO_MAX:
                try:
                    with path.open("rb") as f:
                        await bot.send_photo(chat_id, f, **extra)
                    continue
                except TelegramError as e:  # too tall, too wide, …: send it as a file instead
                    log.warning("photo %s refused (%s): sending it as a document", path.name, e)
            with path.open("rb") as f:
                await bot.send_document(chat_id, f, filename=path.name, **extra)

    @contextlib.asynccontextmanager
    async def queued(self, update: Update):
        """This conversation's turn lock (Core.busy); says so when the message has to wait for the one before."""
        lock = self.busy(update.effective_chat.id)
        if lock.locked():
            await update.effective_message.reply_text("⏳ Still on your previous message; this one comes next.")
        async with lock:
            yield

    @staticmethod
    def keyboard(actions: list[Action], per_row: int = 1) -> InlineKeyboardMarkup | None:
        """The core's actions as inline buttons; a tap sends back the action id."""
        if not actions:
            return None
        buttons = [InlineKeyboardButton(a.label, callback_data=a.id) for a in actions]
        return InlineKeyboardMarkup([buttons[i:i + per_row] for i in range(0, len(buttons), per_row)])

    async def think(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """One turn on the cloud model via pi (pi/think.ts). Explicit by design: nothing is sent unless asked."""
        if not await self.guard(update):
            return
        prompt = " ".join(context.args or []).strip()
        if not prompt:
            await update.effective_message.reply_text("Usage: /think <question>. It goes to ChatGPT, with access "
                                                      "to your schedule, calendar and notes (not money or mail).")
            return
        await self.answer_cloud(update, context, prompt, prefix="🧠 ")

    async def answer_cloud(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                           prefix: str = "☁️ ") -> None:
        async with self.queued(update), typing(update.effective_chat):
            out = await self.cloud_turn(update.effective_chat.id, context.chat_data, text)
        await self.reply(update, out.text if out.stopped else prefix + out.text)

    # ---- desktop off ----------------------------------------------------------------------------
    async def desktop_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        """The local model is unreachable. Money waits for the desktop; otherwise FRANZISKA_WHEN_OFF decides."""
        pinned_money = context.chat_data.get("mode") == "money"
        if pinned_money and not self.desktop.can_wake:  # 'ask' would offer ChatGPT
            await update.effective_message.reply_text(
                "💶 The desktop (local model) is off, and money questions never go to the cloud. Ask again when "
                "it's up.")
            return
        policy = "wake" if pinned_money else self.settings.when_off
        if not self.desktop.can_wake and policy == "wake":
            policy = "ask"
        if policy == "cloud":
            if self.desktop.can_wake:  # keep a reference: a bare create_task can be garbage-collected mid-flight
                self._background = asyncio.create_task(self.desktop.wake_and_wait(self.settings.wake_timeout_s))
            await self.answer_cloud(update, context, text)
        elif policy == "wake":
            await self.wake_then_answer(update, context, text)
        else:
            choices = {"cloud": "☁️ ChatGPT now"} | ({"wake": "🖥️ Wake desktop"} if self.desktop.can_wake else {})
            actions = self.buttons(context.chat_data, "off", text, choices)
            await update.effective_message.reply_text(
                "The desktop (local model) is off. ChatGPT can answer now; it sees your schedule, calendar and "
                "notes, never money or mail.", reply_markup=self.keyboard(actions, per_row=2))

    async def wake_then_answer(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        note = await update.effective_message.reply_text(
            "🖥️ Waking the desktop, I'll answer when it's up (1–2 min)…")
        if await self.desktop.wake_and_wait(self.settings.wake_timeout_s):
            await note.edit_text("🖥️ Desktop is up.")
            await self.on_text(update, context, text=text)
        elif context.chat_data.get("mode") == "money":  # never offered to the cloud
            await note.edit_text("The desktop didn't come up (check Wake-on-LAN, deploy/BOX.md). Money questions wait "
                                 "for it.")
        else:
            actions = self.buttons(context.chat_data, "off", text, {"cloud": "☁️ ChatGPT now"})
            await note.edit_text("The desktop didn't come up (check Wake-on-LAN, deploy/BOX.md).",
                                 reply_markup=self.keyboard(actions))

    async def desktop_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        if context.args and context.args[0] == "wake":
            if not self.desktop.can_wake:
                await update.effective_message.reply_text("FRANZISKA_DESKTOP_MAC is not set.")
                return
            ok = await self.desktop.wake_and_wait(self.settings.wake_timeout_s)
            await update.effective_message.reply_text("🖥️ Desktop is up." if ok else "The desktop didn't come up.")
            return
        up = await self.desktop.is_up(fresh=True)
        hint = "" if up or not self.desktop.can_wake else " /desktop wake"
        await update.effective_message.reply_text(
            f"🖥️ Desktop {'up' if up else 'off'} ({self.settings.server_url}).{hint}")

    async def usage_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The cloud model's tokens and cost: last 7 days and this month, per route (no model)."""
        if not await self.guard(update):
            return
        if not self.models.usage:
            await update.effective_message.reply_text("No cloud model or Claude is set (FRANZISKA_CLOUD_MODEL, "
                                                      "FRANZISKA_CLAUDE_MODEL): all local.")
            return
        where = []
        if self.settings.cloud_model:
            where.append(f"{self.models.cloud_name} on: {', '.join(sorted(self.settings.cloud_routes)) or 'none'}")
        if self.models.claude_on:  # the subscription: tokens count against the plan's limits, nothing is billed
            on = ", ".join(sorted(self.models.claude_on))
            where.append(f"Claude ({self.models.claude_name}, subscription) on: {on}")
        await self.reply(update, usage.report(self.models.usage, self.life.now()) + "\n\n" + "\n".join(where))

    async def mail_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        if not self.mail:
            await update.effective_message.reply_text("Mail is off (needs FRANZISKA_GOOGLE=1 and a Google sign-in).")
            return
        since = (self.life.now() - timedelta(days=3)).isoformat(timespec="minutes")
        text = mailbox.digest(self.mail, unreported_only=False, mark=False, since=since) or "No email needs you."
        if pending := self.mail.pending_count():
            text += f"\n({pending} not triaged yet: waiting for the desktop.)"
        await self.reply(update, text)

    async def on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A bank statement (a known or mapped bank, Core.receive) or a DEGIRO export (.csv) is imported straight into
        the ledger; another bank's statement is offered for mapping. Any other file or photo is read locally
        (documents.py: text layer, then OCR) and its text goes through the router like a message, with the caption
        as the request."""
        if not await self.guard(update):
            return
        msg = update.effective_message
        if msg.photo:
            source, name, mime = msg.photo[-1], "photo.jpg", "image/jpeg"  # the largest size
        else:
            source, name, mime = msg.document, msg.document.file_name or "file", msg.document.mime_type or ""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / Path(name).name
            await (await source.get_file()).download_to_drive(path)
            await update.effective_chat.send_action(ChatAction.TYPING)
            got = await asyncio.to_thread(self.receive, path, name, mime)
        if got.report is not None:  # imported, or why not
            await self.reply(update, got.report, self.keyboard(self.actions(context.chat_data, got.proposals)))
            return
        if not await self.desktop.is_up():  # a document may hold money or mail data: never to the cloud
            await msg.reply_text("The desktop is off, so I can't look at the document now. Send it again later.")
            return
        log.info("document %s: %d chars", name, len(got.text))
        caption = (msg.caption or "").strip()
        self.restore(update.effective_chat.id, context.chat_data)
        message, route = await self.document(context.chat_data, name, got.text, caption, mime, got.data)
        # The document is someone else's text: only the caption counts as said by the user, and writes become buttons.
        await self.answer(update, context, route, message, said=caption, tainted=True)

    @functools.cached_property
    def voice(self) -> voice.Transcriber | None:
        return voice.Transcriber.from_settings(self.settings)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """A voice note (or audio, or a round video): transcribed on this machine's CPU (voice.py), shown back so a
        mishearing is visible, then handled like a typed message."""
        if not await self.guard(update):
            return
        msg = update.effective_message
        if self.voice is None:
            await msg.reply_text("Voice notes are off (FRANZISKA_VOICE_MODEL is empty). Type it, please.")
            return
        source = msg.voice or msg.audio or msg.video_note
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "voice"
            await (await source.get_file()).download_to_drive(path)
            async with typing(update.effective_chat):
                try:
                    text = await asyncio.to_thread(self.voice.transcribe, path)
                except Exception as e:
                    log.exception("transcription failed")
                    await msg.reply_text(f"I couldn't transcribe that ({type(e).__name__}). Type it, please.")
                    return
        if not text:
            await msg.reply_text("I couldn't make out any words in that voice note.")
            return
        await msg.reply_text(f"🎙 {text}")
        await self.on_text(update, context, text=text)

    async def new_conversation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/new: forget the recent turns of every assistant (tasks, notes and memory stay)."""
        if await self.guard(update):
            self.restore(update.effective_chat.id, context.chat_data)
            self.conversations.reset(update.effective_chat.id, context.chat_data)
            self.turns.clear(update.effective_chat.id)  # and the thread every assistant reads (Core._thread)
            await update.effective_message.reply_text("🆕 Fresh start: recent messages forgotten. Tasks, notes and "
                                                      "what I learned about you stay.")

    # ---- buttons --------------------------------------------------------------------------------
    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not self.allowed(update):
            await query.answer("Not authorised.")
            return
        await query.answer()
        self.restore(update.effective_chat.id, context.chat_data)
        kind, *args = query.data.split(":")
        if kind == "route":  # route:<n>:<route>
            text = self.take(context.chat_data, kind, args[0])
            if text is None or args[1] not in self.labels:
                await query.edit_message_text("That question expired; send the message again.")
                return
            await query.edit_message_text(f"{self.labels[args[1]]}")
            await self.answer(update, context, args[1], text)
        elif kind == "code":  # code:<n>:<repo>: the repository for a held /code request
            request = self.take(context.chat_data, kind, args[0])
            if request is None:
                await query.edit_message_text("That question expired; send /code again.")
                return
            await query.edit_message_text(f"Repository: {args[1]}")
            await self.start_job(update, args[1], request)
        elif kind == "mail":  # mail:ok|mute:<message id>, on a pushed email
            m = self.mail.get(args[1]) if self.mail else None
            if args[0] == "mute" and m:
                self.mail.mute(m.who, self.life.now().isoformat(timespec="minutes"))
                await query.edit_message_text(f"{query.message.text}\n\n🔇 No more pushes from {m.who}.")
            else:
                await query.edit_message_reply_markup(None)
        elif kind == "job":  # job:<action>:<id>
            await self.job_button(update, context, args[0], int(args[1]))
        elif kind == "off":  # off:<n>:cloud|wake
            text = self.take(context.chat_data, kind, args[0])
            if text is None:
                await query.edit_message_text("That question expired; send the message again.")
                return
            await query.edit_message_reply_markup(None)
            if args[1] == "cloud":
                await self.answer_cloud(update, context, text)
            else:
                await self.wake_then_answer(update, context, text)
        elif kind == "wf":
            result = await asyncio.to_thread(self.apply, context.chat_data, args[0])  # runs once
            if result is None:
                await query.edit_message_text(f"{query.message.text}\n\nThat button expired; run it again.")
                return
            await query.edit_message_text(f"{query.message.text}\n\n{result}",
                                          reply_markup=_without(query.message.reply_markup, query.data))
        elif kind == "mem":
            gone = self.memory.forget(args[0], args[1])
            result = f"🗑 Forgot {args[0]}/{args[1]}." if gone else f"{args[0]}/{args[1]} was already gone."
            await query.edit_message_text(f"{query.message.text}\n\n{result}",
                                          reply_markup=_without(query.message.reply_markup, query.data))
        elif kind == "rem":
            action, item_id = args[0], int(args[1])
            if action == "days":  # swap the buttons for the next days; the tap is rem:day:<id>:<date>
                today = self.life.now().date()
                days = [today + timedelta(days=n) for n in range(2, 9)]
                buttons = [InlineKeyboardButton(f"{d:%a %d}", callback_data=f"rem:day:{item_id}:{d}") for d in days]
                await query.edit_message_reply_markup(InlineKeyboardMarkup([buttons[:4], buttons[4:]]))
                return
            try:
                if action == "done":
                    result = self.life.complete(item_id)
                elif action == "keep":
                    result = self.life.keep_quiet(item_id)
                elif action == "redate":
                    result = self.life.due_tomorrow(item_id)
                elif action == "drop":
                    result = self.life.cancel(item_id)
                elif action == "tom":
                    result = self.life.snooze_tomorrow(item_id)
                elif action == "pri":
                    result = self.life.bump_priority(item_id, int(args[2]))
                elif action == "day":
                    result = self.life.snooze_to(item_id, date.fromisoformat(args[2]))
                else:
                    result = self.life.snooze(item_id, int(args[2]))
            except LifeError as e:
                result = str(e)
            markup = query.message.reply_markup
            if action != "pri":  # raised or lowered, still open and unsnoozed: the other buttons stay usable
                # A list of reminders (send_reminders) loses only this item's buttons.
                markup = _without_item(markup, item_id) if len(_items(markup)) > 1 else None
            await query.edit_message_text(f"{query.message.text}\n\n{result}", reply_markup=markup)
        elif kind == "ev":
            if args[0] == "plan":
                await query.edit_message_reply_markup(_without(query.message.reply_markup, query.data))
                if await self.desktop.is_up():
                    await self.answer(update, context, "life", PLAN_PROMPT)
                else:
                    await self.desktop_off(update, context, PLAN_PROMPT)
                return
            try:
                result = self.life.complete(int(args[1]))
            except LifeError as e:
                result = str(e)
            await query.edit_message_text(f"{query.message.text}\n{result}",
                                          reply_markup=_without(query.message.reply_markup, query.data))
        elif kind == "rv":  # weekly review: keep / next Monday / drop an undated task
            action, item_id = args[0], int(args[1])
            try:
                if action == "next":
                    today = self.life.now().date()
                    monday = today + timedelta(days=7 - today.weekday())
                    result = self.life.edit_task(item_id, due_at=monday)
                elif action == "drop":
                    result = self.life.cancel(item_id)
                else:
                    task = self.life.tasks.get(item_id)
                    result = f"Kept: {task.title}." if task else f"There is no item #{item_id}."
            except LifeError as e:
                result = str(e)
            markup = query.message.reply_markup
            for k in ("keep", "next", "drop"):
                markup = _without(markup, f"rv:{k}:{item_id}")
            await query.edit_message_text(f"{query.message.text}\n{result}", reply_markup=markup)
        elif kind == "cat":  # cat:<n>:<index in ranked>|skip
            held = self.take(context.chat_data, kind, args[0])
            if held is None:
                await query.edit_message_text("That question expired; send /uncat again.")
                return
            merchant, ranked = held
            if args[1] == "skip":
                context.chat_data.setdefault("uncat_skipped", set()).add(merchant)
                await query.edit_message_text(f"{merchant}: skipped. /uncat for the next one.")
                return
            result = self.money.set_payee_category(merchant, ranked[int(args[1])])
            await query.edit_message_text(f"{result} /uncat for the next one.")

    # ---- jobs (no LLM) --------------------------------------------------------------------------
    async def send_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Due reminders, each marked sent only once Telegram took it: a network failure retries next minute. More
        than GROUP_REMINDERS at once go out as one list (17 in the same second, realmonth 2026-10-30). A task past
        its deadline is asked about instead: still needed?"""
        now = self.life.now()
        due = self.life.tasks.due_reminders(now)
        if len(due) > GROUP_REMINDERS:
            text = "\n".join(f"⏰ {'Still needed? ' if t.missed(now) else ''}{t.line()}" for t in due)
            rows = [[InlineKeyboardButton(f"✅ Done: {t.title[:28]}", callback_data=f"rem:done:{t.id}"),
                     InlineKeyboardButton("Tomorrow", callback_data=f"rem:{_tomorrow(t, now)}:{t.id}")]
                    for t in due]
            await self._remind(context, due, now, text, InlineKeyboardMarkup(rows))
            return
        for task in due:
            if task.missed(now):
                text, rows = f"⏰ Still needed? {task.line()}", [[
                    InlineKeyboardButton("✅ Done", callback_data=f"rem:done:{task.id}"),
                    InlineKeyboardButton("Keep", callback_data=f"rem:keep:{task.id}"),
                    InlineKeyboardButton("Tomorrow", callback_data=f"rem:{_tomorrow(task, now)}:{task.id}"),
                    InlineKeyboardButton("Drop", callback_data=f"rem:drop:{task.id}"),
                ]]
            else:
                text, rows = f"⏰ {task.line()}", [[
                    InlineKeyboardButton("✅ Done", callback_data=f"rem:done:{task.id}"),
                    InlineKeyboardButton("+15m", callback_data=f"rem:snooze:{task.id}:15"),
                    InlineKeyboardButton("+1h", callback_data=f"rem:snooze:{task.id}:60"),
                    InlineKeyboardButton("Tomorrow", callback_data=f"rem:tom:{task.id}"),
                ]]
                if task.kind == "task":
                    rows.append([InlineKeyboardButton("⬆️ Priority", callback_data=f"rem:pri:{task.id}:1"),
                                 InlineKeyboardButton("⬇️ Priority", callback_data=f"rem:pri:{task.id}:-1"),
                                 InlineKeyboardButton("📅 Day…", callback_data=f"rem:days:{task.id}")])
            await self._remind(context, [task], now, text, InlineKeyboardMarkup(rows))

    async def _remind(self, context: ContextTypes.DEFAULT_TYPE, tasks: list, now: datetime, text: str,
                      buttons: InlineKeyboardMarkup) -> None:
        """One reminder message for `tasks`; they are marked sent once Telegram took it."""
        ids = ", ".join(f"#{t.id}" for t in tasks)
        try:
            for user_id in self.settings.allowed_ids:  # private chats: chat id == user id
                await context.bot.send_message(user_id, text, reply_markup=buttons)
        except (NetworkError, RetryAfter) as e:
            log.warning("reminder %s not delivered (%s); retrying next run", ids, e)
            return
        except TelegramError:  # refused for good (e.g. the chat is gone): don't retry every minute
            log.exception("reminder %s refused by Telegram", ids)
        for task in tasks:
            self.life.tasks.mark_sent(task, now)
            log.info("reminder #%s sent", task.id)
            obs.event("reminder sent", task=task.id)

    async def send_brief(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = self.life.brief()
        if problems := self.health.problems() + self.events.failures():  # what not to trust today, first
            text = "\n".join(problems) + "\n\n" + text
        if self.settings.brief_money and (lines := self.money.brief_lines()):
            text += "\n\n" + "\n".join(lines)
        if self.mail:
            since = (self.life.now() - timedelta(days=1)).isoformat(timespec="minutes")
            if mails := self.mail.urgent(since=since):
                text += "\n\n" + "\n".join(["📬 Email that needs you:"] + [m.line() for m in mails])
        for user_id in self.settings.allowed_ids:
            await context.bot.send_message(user_id, text)

    async def send_evening(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, active = self.life.evening()
        rows = [[InlineKeyboardButton(f"✅ {t.title[:40]}", callback_data=f"ev:done:{t.id}")] for t in active[:12]]
        rows.append([InlineKeyboardButton("📋 Plan tomorrow", callback_data="ev:plan")])
        for user_id in self.settings.allowed_ids:
            await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(rows))

    async def send_review(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Sunday: the weekly review, with keep / next Monday / drop buttons per stale undated task."""
        text, stale = self.life.weekly_review()
        rows = [[InlineKeyboardButton(f"✓ {t.title[:24]}", callback_data=f"rv:keep:{t.id}"),
                 InlineKeyboardButton("📅 Mon", callback_data=f"rv:next:{t.id}"),
                 InlineKeyboardButton("🗑", callback_data=f"rv:drop:{t.id}")] for t in stale[:10]]
        for user_id in self.settings.allowed_ids:
            await context.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(rows) if rows else None)

    async def undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/undo: the latest change to a task or local appointment, put back. No LLM."""
        if await self.guard(update):
            await self.reply(update, self.life.undo())

    async def send_week_summary(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mondays: last week's spending, what was charged, and what is expected this week. No model."""
        text = await asyncio.to_thread(self.money.week_summary)
        for user_id in self.settings.allowed_ids:
            await context.bot.send_message(user_id, text)

    async def send_markets(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Weekdays: the market report PDF; Mondays the weekly edition. Without the desktop's model it still comes,
        with headlines instead of summaries (market/brief.py)."""
        wf = workflows.BY_NAME["markets"]
        edition = "weekly" if self.life.now().weekday() == 0 else "daily"
        ctx = dataclasses.replace(self.ctx, model=self.models.for_route(wf.name), loop=asyncio.get_running_loop())
        try:
            report = await asyncio.to_thread(wf.run, ctx, wf.params(edition=edition))
        except Exception as e:
            log.exception("market report failed")
            for user_id in self.settings.allowed_ids:
                await context.bot.send_message(user_id, f"📈 The market report failed ({type(e).__name__}: {e}).")
            return
        self.turns.add(0, wf.name, f"(scheduled {edition})", report.text)
        for user_id in self.settings.allowed_ids:
            await self.send_files(context.bot, user_id, report.text, report.files)

    async def send_month_summary(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """On the 1st: last month's summary and budgets, once the month is over."""
        last_month = (self.money.today().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        text = "\n\n".join([self.money.month_summary(last_month), self.money.budget_status(last_month)])
        for user_id in self.settings.allowed_ids:
            await context.bot.send_message(user_id, text)

    async def nightly(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Housekeeping with no model (housekeeping.py), event handlers, then the backup."""
        results = await asyncio.to_thread(housekeeping.run, self, self.cleanup_log)
        log.info("housekeeping:\n%s", housekeeping.describe(results))
        if failed := [r for r in results if r.failed]:
            self.health.failed("housekeeping", "; ".join(f"{r.what}: {r.note}" for r in failed))
        else:
            self.health.ok("housekeeping")
        if n := await asyncio.to_thread(self.events.dispatch):
            log.info("events: %d pending handled", n)
        try:
            where = await asyncio.to_thread(backup.run, self.settings, self.life.now().date())
            log.info("backup written to %s", where)
            self.health.ok("backup")
        except Exception as e:
            log.exception("nightly backup failed")
            self.health.failed("backup", e)
            for user_id in self.settings.allowed_ids:
                await context.bot.send_message(user_id, "⚠️ Last night's backup failed; see the bot's log.")

    async def fill_library(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Describe the documents that came in without a description (Core.describe_new, the local model), then embed
        the library's new passages and the new turns on the CPU embedding server (library.py, turns.py), while nobody
        is chatting: a turn waits for no embedding, and a message that arrives stops the job after its batch."""
        def quiet() -> bool:
            return self.quiet(LIBRARY_QUIET_S)
        if quiet() and not self.paused():
            await self.describe_new(quiet)
            await asyncio.to_thread(self.library.fill, quiet=quiet)
            await asyncio.to_thread(self.turns.fill, quiet=quiet)  # past conversations by meaning (turns.py)

    async def consolidate(self, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """What the conversations taught and weekly digests (consolidate.py). False: the desktop is off, try again."""
        if not await self.desktop.is_up():
            return False
        try:
            done = await consolidate.run(self)
        except Exception as e:  # a model or parsing failure: logged, shown in the brief, tomorrow again
            log.exception("consolidation failed")
            self.health.failed("consolidate", e)
            return True
        if done:
            self.health.ok("consolidate")
        return done

    async def housekeeping_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/housekeeping: what the nightly cleanup removed lately and what the data takes; `/housekeeping run` does it now."""
        if not await self.guard(update):
            return
        if context.args and context.args[0] == "run":
            results = await asyncio.to_thread(housekeeping.run, self, self.cleanup_log)
            await self.reply(update, housekeeping.describe(results))
            return
        await self.reply(update, await asyncio.to_thread(housekeeping.report, self, self.cleanup_log))

    async def sync_calendar(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            n = await asyncio.to_thread(self.life.calendar.refresh, self.life.now())
            log.debug("calendar: %d events mirrored", n)
            self.health.ok("calendar")
        except Exception as e:  # agenda and brief use the last mirror, and say since when (health.py)
            log.exception("calendar sync failed; agenda uses the last mirror")
            self.health.failed("calendar", e)

    async def check_mail(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Fetch new headers (box), triage them if the desktop is up (local model only), push what clearly needs the
        user: one message each, with ✅ / 🔇 (mutes the sender's pushes)."""
        try:
            new = await asyncio.to_thread(mailbox.fetch_new, self.gmail, self.mail)
            me = await asyncio.to_thread(self.gmail.me)
            self.health.ok("mail")
        except Exception as e:  # /mail and the brief would look like 'no new mail': health says otherwise
            log.exception("mail fetch failed")
            self.health.failed("mail", e)
            return
        if new:
            log.info("mail: %d new", new)
            obs.event("mail fetched", new=new)
        if not self.router or not self.mail.pending_count() or not await self.desktop.is_up():
            return  # untriaged mail waits for the desktop; it is never sent to the cloud
        if self._triage is None:
            self._triage = await asyncio.to_thread(lambda: mailbox.Triage(self.router.backend))
        now = self.life.now().isoformat(timespec="minutes")
        with obs.span("mail triage", pending=self.mail.pending_count()):
            try:
                await asyncio.to_thread(mailbox.triage_pending, self.mail, self._triage, now, me=me)
            except gpu.Busy as e:  # an eval or a sweep holds the GPU: the pending mail waits for the next run
                log.info("mail triage waits: %s", e)
                return
        for m in self.mail.urgent(unreported_only=True):
            buttons = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Got it", callback_data=f"mail:ok:{m.id}"),
                                             InlineKeyboardButton("🔇 Not important", callback_data=f"mail:mute:{m.id}")]])
            try:
                for user_id in self.settings.allowed_ids:
                    await context.bot.send_message(user_id, f"📬 {m.sender}: {m.subject}\n{m.snippet}",
                                                   reply_markup=buttons)
            except (NetworkError, RetryAfter) as e:  # not marked: the next run pushes it again
                log.warning("mail push %s not delivered (%s)", m.id, e)
                return
            self.mail.mark_reported([m.id])

    @staticmethod
    async def reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        """Split at Telegram's limit; buttons go on the last part."""
        for i in range(0, len(text), TELEGRAM_MAX):
            last = i + TELEGRAM_MAX >= len(text)
            await update.effective_message.reply_text(text[i:i + TELEGRAM_MAX], reply_markup=markup if last else None)


def traced(callback):
    """A handler whose update is one run (obs.root): 'message', 'tap', … or the command typed ('/today')."""
    kind = UPDATE_SPANS.get(callback.__name__, "command")

    @functools.wraps(callback)
    async def run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, query = update.effective_message, update.callback_query
        text = query.data if query else (msg.text or msg.caption or "") if msg else ""
        name = text.split()[0].split("@")[0] if kind == "command" and text else kind
        with obs.root(name, kind, text=text[:200]):
            await callback(update, context)
    return run


@contextlib.asynccontextmanager
async def typing(chat):
    """Keep 'typing…' up while a turn runs: one send_action lasts ~5 s and a mail turn can take two minutes."""
    async def keep() -> None:
        while True:
            with contextlib.suppress(TelegramError):
                await chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(TYPING_EVERY_S)

    task = asyncio.create_task(keep())
    try:
        yield
    finally:
        task.cancel()


def time_now() -> float:
    """Monotonic seconds, for how long a pending 💬 change stays the meaning of the next message."""
    import time as _time

    return _time.monotonic()


def _tomorrow(task, now: datetime) -> str:
    """A reminder's Tomorrow button: a missed deadline moves to tomorrow ('redate'), anything else is snoozed."""
    return "redate" if task.missed(now) and task.due_at else "tom"


def _items(markup: InlineKeyboardMarkup | None) -> set[str]:
    """The item ids a reminder keyboard acts on ('rem:<action>:<id>…')."""
    rows = markup.inline_keyboard if markup else []
    return {b.callback_data.split(":")[2] for row in rows for b in row if b.callback_data.startswith("rem:")}


def _without_item(markup: InlineKeyboardMarkup | None, item_id: int) -> InlineKeyboardMarkup | None:
    """A reminder list's keyboard minus one item's buttons."""
    if markup is None:
        return None
    rows = [r for r in ([b for b in row if b.callback_data.split(":")[2:3] != [str(item_id)]]
                        for row in markup.inline_keyboard) if r]
    return InlineKeyboardMarkup(rows) if rows else None


def _without(markup: InlineKeyboardMarkup | None, data: str) -> InlineKeyboardMarkup | None:
    """The keyboard minus the button that was tapped."""
    if markup is None:
        return None
    rows = [[b for b in row if b.callback_data != data] for row in markup.inline_keyboard]
    return InlineKeyboardMarkup([r for r in rows if r])


def main() -> None:
    pydantic_ai.BANNER_ENABLED = False
    settings = Settings.from_env()
    obs.setup("franziska-bot", settings.traces_db, settings.otlp)
    if not settings.telegram_token:
        raise SystemExit("FRANZISKA_TELEGRAM_TOKEN is not set. See .env.example.")
    bot = Bot(settings)
    # Updates run concurrently: a two-minute mail turn no longer holds up /today or a reminder's buttons. Model turns
    # still go one at a time per chat (Core.busy) and on the GPU (gpu.py).
    app = Application.builder().token(settings.telegram_token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("whoami", bot.whoami))
    app.add_handler(CommandHandler(["start", "help"], bot.help))
    app.add_handler(CommandHandler(["auto", "life", "money"], bot.set_mode))
    app.add_handler(CommandHandler(["today", "week"], bot.agenda))
    app.add_handler(CommandHandler(["tasks", "done"], bot.tasks_cmd))
    app.add_handler(CommandHandler("notes", bot.notes))
    app.add_handler(CommandHandler("memory", bot.memory_cmd))
    app.add_handler(CommandHandler("undo", bot.undo))
    app.add_handler(CommandHandler(["pause", "resume"], bot.pause_cmd))
    app.add_handler(CommandHandler("new", bot.new_conversation))
    app.add_handler(CommandHandler(["month", "weekly", "budget", "subs", "bills"], bot.money_report))
    app.add_handler(CommandHandler("uncat", bot.uncategorised))
    app.add_handler(CommandHandler("portfolio", bot.portfolio_cmd))
    app.add_handler(CommandHandler("think", bot.think))
    app.add_handler(CommandHandler("desktop", bot.desktop_status))
    app.add_handler(CommandHandler("usage", bot.usage_cmd))
    app.add_handler(CommandHandler("housekeeping", bot.housekeeping_cmd))
    app.add_handler(CommandHandler("mail", bot.mail_cmd))
    app.add_handler(CommandHandler("code", bot.code_cmd))
    if bot.workflows:
        app.add_handler(CommandHandler(list(bot.workflows), bot.workflow_cmd))
    app.add_handler(CallbackQueryHandler(bot.on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, bot.on_voice))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, bot.on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.on_text))
    for handlers in app.handlers.values():  # every update is a run of its own (obs.py)
        for h in handlers:
            h.callback = traced(h.callback)
    # The repeating ticks are quiet runs: one that did nothing leaves no trace (obs.root).
    app.job_queue.run_repeating(obs.job("reminders", bot.send_reminders, quiet=True), interval=REMINDER_INTERVAL_S,
                                first=5)

    async def tell(text: str) -> None:
        for user_id in settings.allowed_ids:
            await app.bot.send_message(user_id, text)

    # Daily jobs go through the scheduler so a box that was off at their time runs them late (catchup.py).
    sched = catchup.Scheduler(app.job_queue, settings.data_dir / "jobs.sqlite", bot.life.now, settings.timezone, tell)
    H = timedelta(hours=1)
    sched.add(catchup.Job("nightly", bot.nightly, NIGHTLY_AT, 20 * H, quiet=True))
    sched.add(catchup.Job("consolidate", bot.unless_paused(bot.consolidate, False), time(4, 30), 20 * H, quiet=True))
    if bot.life.calendar:
        app.job_queue.run_repeating(obs.job("calendar", bot.sync_calendar, quiet=True), interval=CALENDAR_SYNC_S,
                                    first=1)
    app.job_queue.run_repeating(obs.job("code", bot.announce_jobs, quiet=True), interval=CODE_TICK_S, first=15)
    if bot.library.embedder:
        app.job_queue.run_repeating(obs.job("library", bot.fill_library, quiet=True), interval=LIBRARY_EVERY_S,
                                    first=60)
    if bot.mail:
        app.job_queue.run_repeating(obs.job("mail", bot.unless_paused(bot.check_mail), quiet=True),
                                    interval=settings.mail_every_min * 60, first=30)
    if settings.brief_at:
        hh, mm = map(int, settings.brief_at.split(":"))
        at = time(hh, mm)
        sched.add(catchup.Job("brief", bot.unless_paused(bot.send_brief), at, 12 * H))
        if settings.brief_money:
            sched.add(catchup.Job("month", bot.unless_paused(bot.send_month_summary), at, 7 * 24 * H, month_day=1))
            # PTB days: 0 = Sunday, 1 = Monday.
            sched.add(catchup.Job("week", bot.unless_paused(bot.send_week_summary), at, 3 * 24 * H, days=(1,)))
    if settings.market_at and settings.brief_money:
        hh, mm = map(int, settings.market_at.split(":"))
        sched.add(catchup.Job("markets", bot.unless_paused(bot.send_markets), time(hh, mm, 30), 10 * H, days=(1, 2, 3, 4, 5)))
    # Before the evening check-in, so its planning can follow.
    sched.add(catchup.Job("review", bot.unless_paused(bot.send_review), REVIEW_AT, 18 * H, days=(0,)))
    if settings.evening_at:
        hh, mm = map(int, settings.evening_at.split(":"))
        sched.add(catchup.Job("evening", bot.unless_paused(bot.send_evening), time(hh, mm), 3 * H))
    sched.start()
    # Retry the first requests forever: IPv6 is broken here and a slow start used to kill the bot.
    app.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
