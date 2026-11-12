"""The secretary's tools as plain methods: one implementation behind the Pydantic AI agent and the MCP server.

Methods return short text for a model to read and raise LifeError for input the model should correct
(the agent turns it into a retry, the MCP server into a tool error).

With `calendar` set, appointments live in Google Calendar (ids `c5`); tasks stay local (ids `12`). Without it,
appointments are local rows like tasks. Item ids reach the tools as text ('12', '#12', 'c5').
"""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx

from franziska.google.auth import GoogleAuthError
from franziska.google.calendar import Appointment, GoogleCalendar
from franziska.secretary.memory import NoteStore
from franziska.secretary.tasks import (DAY_END, DAY_START, PRIORITIES, Task, TaskStore, day_reminder, duration,
                                       due_reminder, first_reminder, iso, next_nag, normalise_words)

log = logging.getLogger(__name__)

Recur = Literal["daily", "weekly", "monthly", "yearly"]
Priority = Literal["high", "normal", "low"]
Clearable = Literal["due", "reminder", "recur", "notes", "minutes", "project"]


STALE_AFTER = timedelta(days=14)  # the weekly review asks about undated tasks older than this


class LifeError(ValueError):
    """A tool call the model should fix and retry (bad date, unknown id)."""


@dataclass
class Life:
    tasks: TaskStore
    notes: NoteStore
    timezone: str = "Europe/Rome"
    clock: Callable[[], datetime] | None = None  # tests pin the time; default: the wall clock in `timezone`
    calendar: GoogleCalendar | None = None
    # Guesses a new task's priority from its text when none is given (the bot's AnyJev decision); None: 'normal'.
    prioritize: Callable[[str], str | None] | None = None
    # Which reminder rule each task title falls under (Router.reminder_rules: titles, rules → index or None per title).
    # None: no model here (CLIs, MCP server, scripted evals), so only the '*' default applies.
    classify_rules: Callable[[list[str], list[str]], list[int | None]] | None = None

    def __post_init__(self):
        # One clock for everything Life writes: undo entries and the calendar mirror too (evals simulate a week).
        self.tasks.clock = self.now
        if self.calendar:
            self.calendar.clock = self.now

    @classmethod
    def open(cls, db: Path, timezone: str = "Europe/Rome", clock: Callable[[], datetime] | None = None,
             calendar: GoogleCalendar | None = None) -> "Life":
        return cls(TaskStore(db), NoteStore(db), timezone, clock, calendar)

    @classmethod
    def from_settings(cls, settings) -> "Life":
        """The bot's, MCP server's and CLI's Life: with Google Calendar when FRANZISKA_GOOGLE=1 and signed in."""
        calendar = None
        if settings.google:
            from franziska.google.auth import GoogleClient

            client = GoogleClient(settings.google_token)
            if client.configured:
                calendar = GoogleCalendar(client, settings.life_db, settings.google_calendar_id, settings.timezone)
        return cls.open(settings.life_db, settings.timezone, calendar=calendar)

    def now(self) -> datetime:
        now = self.clock() if self.clock else datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None)
        return now.replace(second=0, microsecond=0)

    def _future(self, field: str, dt: datetime | None) -> datetime | None:
        if dt is None:
            return None
        dt = dt.replace(tzinfo=None)
        if dt < self.now():
            raise LifeError(f"{field} {dt:%Y-%m-%d %H:%M} is in the past (now is {self.now():%A %Y-%m-%d %H:%M}). "
                            "Recheck the date.")
        return dt

    # ---- schedule -------------------------------------------------------------------------------
    def add_task(self, title: str, due_at: date | None = None, remind_at: datetime | None = None,
                 recur: Recur | None = None, notes: str | None = None, priority: Priority | None = None,
                 soft: bool = False, minutes: int | None = None, project: str | None = None) -> str:
        task = self.new_task(title, due_at, remind_at, recur, notes, priority, soft, minutes, project)
        guessed = "" if priority else f" (priority {task.priority} was guessed; the user can change it)"
        return f"Saved: {task.line()}{guessed}"

    def new_task(self, title: str, due_at: date | None = None, remind_at: datetime | None = None,
                 recur: Recur | None = None, notes: str | None = None, priority: Priority | None = None,
                 soft: bool = False, minutes: int | None = None, project: str | None = None) -> Task:
        """add_task for code that needs the task back (the finance assistant's bills).

        `due_at` is a day or a time. Without `remind_at`, the reminder follows from the date and priority
        (tasks.first_reminder); a repeating task's from its due day (tasks.day_reminder)."""
        remind_at = self._future("remind_at", remind_at)
        due_at = _day_or_time(due_at)
        if recur and not (due_at or remind_at):
            raise LifeError("A repeating task needs due_at or remind_at to know when it repeats.")
        if soft and not due_at:
            raise LifeError("A soft deadline needs due_at, the target day ('within a month' → a month from today). "
                            "With no time frame at all ('prima o poi', 'someday'): soft=false and no due_at.")
        if minutes is not None and minutes <= 0:
            raise LifeError("minutes is the estimated effort: a positive number, or leave it out.")
        priority = priority or self._guess_priority(title, notes)
        rule_id = self._rules_of([title])[0]
        now, at = self.now(), self.tasks.day_time(rule_id)
        if remind_at is None:
            remind_at = (day_reminder(iso(due_at), now, at) if recur
                         else first_reminder(now, priority, iso(due_at), soft, at))
        return self.tasks.add(title, now, due_at, remind_at, notes, recur=recur, priority=priority, soft=soft,
                              minutes=minutes, project=project, rule_id=rule_id)

    def _guess_priority(self, title: str, notes: str | None) -> str:
        guess = self.prioritize(f"{title}. {notes}" if notes else title) if self.prioritize else None
        return guess if guess in PRIORITIES else "normal"

    def edit_task(self, item_id: int | str, title: str | None = None, due_at: date | None = None,
                  soft: bool | None = None, priority: Priority | None = None, remind_at: datetime | None = None,
                  recur: Recur | None = None, notes: str | None = None, nag: bool | None = None,
                  clear: list[Clearable] | None = None, minutes: int | None = None, project: str | None = None) -> str:
        """Change any detail of a task; fields left out stay. A new date or priority moves the reminder with it
        unless `remind_at` is given."""
        task_id, appt = self._item(item_id)
        if appt:
            raise LifeError(f"{appt.line()} is a calendar appointment: use move_appointment to move it, cancel to delete it.")
        given = (title, due_at, soft, priority, remind_at, recur, notes, nag, minutes, project)
        if all(v is None for v in given) and not clear:  # "Updated" for nothing let the model think it did something
            raise LifeError(f"Nothing to change: pass only the fields that change. To mark #{task_id} done, use "
                            "complete; for a new appointment, add_appointment.")
        task, now = self.tasks.get(task_id), self.now()
        rule_id = task.rule_id
        if title and title.strip() != task.title:  # renamed: it may fall under another rule
            rule_id = self._rules_of([title.strip()])[0]
        at = self.tasks.day_time(rule_id)
        if task.kind == "event":
            raise LifeError(f"{task.line()} is an appointment: use move_appointment to move it.")
        fields: dict = {k: v for k, v in (("title", title and title.strip()), ("notes", notes), ("recur", recur),
                                          ("priority", priority), ("soft", soft), ("nag", nag),
                                          ("minutes", minutes), ("project", project and project.strip()))
                  if v is not None}
        if minutes is not None and minutes <= 0:
            raise LifeError("minutes is the estimated effort: a positive number (clear=['minutes'] removes it).")
        if rule_id != task.rule_id:
            fields["rule_id"] = rule_id
            old = self.tasks.day_time(task.rule_id)  # a reminder still at the old rule's time of day follows the new one
            if (not remind_at and task.remind_at and task.remind_at > iso(now)
                    and datetime.fromisoformat(task.remind_at).time() == old and at != old):
                fields["remind_at"] = datetime.combine(datetime.fromisoformat(task.remind_at).date(), at)
        if due_at:
            fields["due_at"] = _day_or_time(due_at)
        if remind_at:
            fields["remind_at"] = self._future("remind_at", remind_at)
        for c in clear or []:
            fields[{"due": "due_at", "reminder": "remind_at", "recur": "recur", "notes": "notes", "minutes": "minutes",
                    "project": "project"}[c]] = None
            if c == "due":
                fields["soft"] = False
        if fields.get("soft") and not (fields.get("due_at") if "due_at" in fields else task.due_at):
            raise LifeError("A soft deadline needs due_at, the target day.")
        recur_ = fields.get("recur", task.recur)
        due_ = iso(fields["due_at"]) if "due_at" in fields else task.due_at
        if "remind_at" not in fields and recur_ and due_ and ("due_at" in fields or not task.remind_at):
            fields["remind_at"] = day_reminder(due_, now, at)  # a repeating task reminds on its (new) due day
        if "remind_at" not in fields and not recur_:
            pri = fields.get("priority", task.priority)
            due = iso(fields["due_at"]) if "due_at" in fields else task.due_at
            soft_ = fields.get("soft", task.soft)
            if "due_at" in fields or "soft" in fields:
                fields["remind_at"] = first_reminder(now, pri, due, soft_, at)
            elif "priority" in fields:  # raised: nag sooner; lowered: the next reminder stays, then the new pace
                sooner = next_nag(now, pri, due if soft_ else None, at)
                waiting = due and not soft_ and due_reminder(due, now, at)  # no nags before the deadline's own reminder
                if not waiting and (task.remind_at or "9999") > iso(sooner) and (task.remind_at or pri == "high"):
                    fields["remind_at"] = sooner
        return f"Updated: {self.tasks.update(task_id, **fields).line()}"

    def bump_priority(self, item_id: int, step: int) -> str:
        """One level up (step=1) or down (-1), for the reminder buttons."""
        task = self.tasks.get(item_id)
        if task is None:
            raise LifeError(f"There is no item #{item_id}.")
        i = max(0, min(2, PRIORITIES.index(task.priority) - step))
        if PRIORITIES[i] == task.priority:
            return f"Already {task.priority} priority."
        return self.edit_task(item_id, priority=PRIORITIES[i])

    def open_list(self) -> str:
        """Every open task, by priority, overdue ones flagged. What the agent reads before acting on tasks."""
        return "\n".join(self._grouped(self.tasks.open_tasks())) or "No open tasks."

    def done_list(self, days: int = 7) -> str:
        done = self.tasks.done(self.now() - timedelta(days=max(1, days)))
        if not done:
            return f"Nothing completed in the last {days} days."
        return "\n".join(f"✅ {t.title} ({datetime.fromisoformat(t.done_at):%a %d %b})" for t in done)

    def _grouped(self, tasks: list[Task]) -> list[str]:
        now = iso(self.now())
        out = []
        for pri, label in (("high", "🔴 High priority:"), ("normal", "Open tasks:"), ("low", "⚪ Low priority:")):
            if group := [t for t in tasks if t.priority == pri]:
                out += [label] + [f"  {t.listed()}{' · OVERDUE' if t.due_end and t.due_end < now else ''}"
                                  for t in group]
        return out

    def add_appointment(self, title: str, starts_at: datetime, ends_at: datetime | None = None,
                        location: str | None = None, remind_minutes_before: int | None = 60,
                        recur: Recur | None = None, notes: str | None = None) -> str:
        starts_at = self._future("starts_at", starts_at)
        ends_at = ends_at and ends_at.replace(tzinfo=None)
        if ends_at and ends_at <= starts_at:
            raise LifeError("ends_at must be after starts_at.")
        if self.calendar:  # Google sends the reminder to the phone
            with _google():
                appt = self.calendar.add(title, starts_at, ends_at, location, remind_minutes_before, recur, notes)
            return f"Saved in Google Calendar: {appt.line()}"
        remind_at = None
        if remind_minutes_before is not None:
            remind_at = max(starts_at - timedelta(minutes=remind_minutes_before), self.now())
        task = self.tasks.add(title, self.now(), remind_at=remind_at, notes=notes, kind="event",
                              starts_at=starts_at, ends_at=ends_at, location=location, recur=recur)
        return f"Saved: {task.line()}"

    def agenda(self, start: date | None = None, days: int = 7) -> str:
        """Appointments and tasks from `start` (default today) for `days` days, plus overdue and other open tasks.

        A task sits on the day of its deadline, or of its reminder if it has no deadline.
        """
        now = self.now()
        day0 = datetime.combine(start or now.date(), time())
        end = day0 + timedelta(days=max(1, days))
        open_tasks = self.tasks.open_tasks()
        out = []
        overdue = [t for t in open_tasks if t.due_at and t.due_end < iso(now)]
        if overdue and day0.date() <= now.date():
            out += ["Overdue:"] + [f"  {t.listed()}" for t in overdue]
        out += self._by_day(day0, end)
        # Undated tasks, and tasks whose reminder already went off without a deadline: still open, not overdue.
        other = [t for t in open_tasks if t not in overdue and not (t.when and t.when >= iso(now))]
        if other:
            out += ["Other open tasks:"] + [f"  {t.listed()}" for t in other]
        if self.calendar and (synced := self.calendar.synced_at()) and synced < iso(now - timedelta(hours=1)):
            out.append(f"(Google Calendar last synced {synced.replace('T', ' ')}; it may be out of date.)")
        last = end - timedelta(days=1)
        return "\n".join(out) if out else f"Nothing scheduled from {day0:%a %d %b} to {last:%a %d %b}."

    def upcoming(self, day0: datetime, end: datetime) -> list:
        """Appointments (local and Google) and dated open tasks in [day0, end), by time: Tasks and Appointments."""
        now = self.now()
        self.tasks.roll_recurring_events(now)
        tasks = [t for t in self.tasks.open_tasks() if t.when and t.when >= iso(now)]
        items = self.tasks.events(day0, end) + [t for t in tasks if iso(day0) <= t.when < iso(end)]
        if self.calendar:
            items += self.calendar.events(day0, end)
        return sorted(items, key=lambda t: t.when)

    def _by_day(self, day0: datetime, end: datetime) -> list[str]:
        """Appointments and dated open tasks in [day0, end), as 'Monday 26 Oct:' headings with indented lines."""
        by_day: dict[str, list[str]] = {}
        for t in self.upcoming(day0, end):
            by_day.setdefault(datetime.fromisoformat(t.when).strftime("%A %d %b"), []).append(
                f"  {t.listed() if isinstance(t, Task) else t.line()}")
        return [ln for day, lines in by_day.items() for ln in (f"{day}:", *lines)]

    def brief(self) -> str:
        """The morning message: today, every other open task by priority, then the rest of the week. No LLM."""
        now = self.now()
        today = datetime.combine(now.date(), time())
        tomorrow = today + timedelta(days=1)
        out = [f"☀️ {now:%A %d %B}"]
        today_lines = self._by_day(today, tomorrow)
        out += today_lines[1:] if today_lines else ["  Nothing scheduled today."]
        week_end = tomorrow + timedelta(days=6)  # tasks listed by day (today, this week) are not repeated below
        shown = {t.id for t in self.tasks.open_tasks() if t.when and iso(now) <= t.when < iso(week_end)}
        if rest := self._grouped([t for t in self.tasks.open_tasks() if t.id not in shown]):
            out += [""] + rest
        if week := self._by_day(tomorrow, week_end):
            out += ["", "Later this week:"] + week
        return "\n".join(out)

    def evening(self) -> tuple[str, list[Task]]:
        """The evening check-in: what was done today, the open tasks that matter now (to tick off with buttons),
        and tomorrow. No LLM; the planning that may follow is a normal chat turn."""
        now = self.now()
        tomorrow = datetime.combine(now.date() + timedelta(days=1), time())
        end_today, end_tomorrow = iso(tomorrow - timedelta(minutes=1)), iso(tomorrow + timedelta(hours=24))
        done = self.tasks.done(datetime.combine(now.date(), time()))
        active = [t for t in self.tasks.open_tasks() if t.priority == "high"
                  or (t.due_at and t.due_end <= end_today)
                  or (not t.due_at and t.remind_at and t.remind_at <= end_tomorrow)]
        out = [f"🌙 Evening check-in, {now:%a %d %b}"]
        if done:
            out.append("Done today: " + ", ".join(f"✅ {t.title}" for t in done))
        if active:
            out += ["Still open (tap what you finished):"] + [f"  {t.line()}" for t in active]
        else:
            out.append("Nothing urgent is open. 🎉")
        if plan := self._by_day(tomorrow, tomorrow + timedelta(days=1)):
            out += ["", "Tomorrow:"] + plan[1:]
        out += ["", "Anything to add or move for tomorrow? Just write it."]
        return "\n".join(out), active

    def complete(self, item_id: int | str, stop: bool = False) -> str:
        task_id, appt = self._item(item_id)
        if appt:
            return f"{appt.line()} is a calendar appointment: nothing to complete. Use cancel to delete it."
        task = self.tasks.complete(task_id, self.now(), stop)
        return f"Next occurrence: {task.line()}" if task.recur else f"Done: {task.line()}"

    def reschedule(self, item_id: int | str, starts_at: datetime | None = None, ends_at: datetime | None = None,
                   due_at: datetime | None = None, remind_at: datetime | None = None) -> str:
        task_id, appt = self._item(item_id)
        starts_at = self._future("starts_at", starts_at)
        remind_at = self._future("remind_at", remind_at)
        if appt:
            if starts_at is None:
                raise LifeError(f"To move {appt.line()} give the new starts_at.")
            with _google():
                appt = self.calendar.move(appt, starts_at, ends_at and ends_at.replace(tzinfo=None))
            return f"Updated in Google Calendar: {appt.line()}"
        task = self.tasks.get(task_id)
        if starts_at and task.starts_at and remind_at is None and task.remind_at:
            # Moving an appointment keeps the reminder's lead time.
            lead = datetime.fromisoformat(task.starts_at) - datetime.fromisoformat(task.remind_at)
            remind_at = max(starts_at - lead, self.now())
        if starts_at and task.ends_at and ends_at is None:
            ends_at = starts_at + (datetime.fromisoformat(task.ends_at) - datetime.fromisoformat(task.starts_at))
        task = self.tasks.reschedule(task_id, due_at and due_at.replace(tzinfo=None), remind_at,
                                     starts_at=starts_at, ends_at=ends_at and ends_at.replace(tzinfo=None))
        return f"Updated: {task.line()}"

    def move_appointment(self, item_id: int | str, starts_at: datetime, ends_at: datetime | None = None) -> str:
        """reschedule for appointments only: a task id is refused, so the model edits tasks with edit_task."""
        task_id, _ = self._item(item_id)
        if task_id is not None and self.tasks.get(task_id).kind == "task":
            raise LifeError(f"#{task_id} is a task, not an appointment: use edit_task to change it.")
        return self.reschedule(item_id, starts_at, ends_at)

    def plan_overview(self, day: date | None = None) -> str:
        """Open tasks by priority, one day's agenda, and today's completions: the state a plan is made from."""
        now = self.now()
        day = day or now.date() + timedelta(days=1)
        start = datetime.combine(day, time())
        open_ = self.tasks.open_tasks()
        out = self._grouped(open_) or ["No open tasks."]
        out += ["", f"{day:%A %d %b}:"] + ([ln for ln in self._by_day(start, start + timedelta(days=1))[1:]]
                                           or ["  nothing scheduled"])
        slots = self.free_slots(day)
        free = sum(int((b - a).total_seconds() // 60) for a, b in slots)
        out += ["", f"Free time {day:%a %d %b} ({DAY_START:%H:%M}–{DAY_END:%H:%M}, around appointments): "
                + (", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in slots) + f" = {duration(free)}" if slots else "none")]
        estimated = [t for t in open_ if t.minutes]
        if estimated:
            out.append(f"Estimated open work: {duration(sum(t.minutes for t in estimated))} over {len(estimated)} "
                       f"task(s); {len(open_) - len(estimated)} without an estimate.")
        if done := self.tasks.done(datetime.combine(now.date(), time())):
            out += ["", "Done today (do not plan these): " + ", ".join(t.title for t in done)]
        return "\n".join(out)

    def free_slots(self, day: date | None = None, min_minutes: int = 15) -> list[tuple[datetime, datetime]]:
        """The gaps of at least `min_minutes` between appointments (local and Google) inside DAY_START..DAY_END of
        `day` (default today), from now on when `day` is today. All-day events don't block time."""
        now = self.now()
        day = day or now.date()
        lo, hi = datetime.combine(day, DAY_START), datetime.combine(day, DAY_END)
        if day == now.date():
            lo = max(lo, now)
        busy = []
        for item in self.upcoming(datetime.combine(day, time()), datetime.combine(day + timedelta(days=1), time())):
            if not getattr(item, "starts_at", None) or getattr(item, "all_day", False):
                continue  # a task, or an all-day event
            a = datetime.fromisoformat(item.starts_at)
            b = datetime.fromisoformat(item.ends_at) if item.ends_at else a + timedelta(hours=1)
            busy.append((a, b))
        slots, cursor = [], lo
        for a, b in sorted(busy):
            if a > cursor:
                slots.append((cursor, min(a, hi)))
            cursor = max(cursor, b)
        if cursor < hi:
            slots.append((cursor, hi))
        return [(a, b) for a, b in slots if (b - a) >= timedelta(minutes=min_minutes)]

    def free_time(self, day: date | None = None) -> str:
        """free_slots as text, for the agent."""
        day = day or self.now().date()
        slots = self.free_slots(day)
        if not slots:
            return f"No free time on {day:%a %d %b} between {DAY_START:%H:%M} and {DAY_END:%H:%M}."
        total = sum(int((b - a).total_seconds() // 60) for a, b in slots)
        return (f"Free on {day:%a %d %b}: " + ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in slots)
                + f" ({duration(total)} in all).")

    def projects_overview(self) -> str:
        """Every project with open tasks: its open and done steps, the estimated time left, and its next step."""
        rows = self.tasks.projects()
        if not rows:
            return "No projects. A task joins one with project='name' (add_task / edit_task)."
        open_ = self.tasks.open_tasks()
        out = []
        for name, n_open, n_done, minutes in rows:
            steps = [t for t in open_ if (t.project or "").lower() == name.lower()]
            nxt = next((t for t in steps if t.when), steps[0] if steps else None)
            left = f", ~{duration(minutes)} estimated" if minutes else ""
            out.append(f"{name}: {n_open} open, {n_done} done{left}"
                       + (f"\n  next: {nxt.line()}" if nxt else "")
                       + ("" if any(t.when for t in steps) else "\n  (no step has a date)"))
        return "\n".join(out)

    def cancel(self, item_id: int | str) -> str:
        task_id, appt = self._item(item_id)
        if appt:
            with _google():
                self.calendar.cancel(appt)
            return f"Removed from Google Calendar: {appt.line()}"
        return f"Removed: {self.tasks.delete(task_id).line()}"

    def _item(self, item_id: int | str) -> tuple[int | None, Appointment | None]:
        """'12' / '#12' / 12 → a task id; 'c5' / '#c5' → a mirrored calendar appointment. Unknown ids raise."""
        ref = str(item_id).strip().lstrip("#").lower()
        if ref.startswith("c") and ref[1:].isdigit():
            appt = self.calendar.get(int(ref[1:])) if self.calendar else None
            if appt is None:
                raise LifeError(f"There is no appointment #{ref}. Call agenda to find the right id.")
            return None, appt
        if not ref.isdigit() or self.tasks.get(int(ref)) is None:
            raise LifeError(f"There is no item #{ref}. Call agenda to find the right id.")
        return int(ref), None

    def snooze(self, item_id: int, minutes: int) -> str:
        """Re-arm an item's reminder `minutes` from now (used by the reminder buttons, no LLM)."""
        task = self.tasks.get(item_id)
        if task is None:
            raise LifeError(f"There is no item #{item_id}.")
        task = self.tasks.reschedule(item_id, remind_at=self.now() + timedelta(minutes=minutes), snooze=True)
        return f"Snoozed: {task.line()}"

    def due_tomorrow(self, item_id: int) -> str:
        """A missed deadline moved to tomorrow, same time of day (the 'still needed?' button); the reminder follows."""
        task = self.tasks.get(item_id)
        if task is None or not task.due_at:
            raise LifeError(f"There is no dated task #{item_id}.")
        day = self.now().date() + timedelta(days=1)
        return self.edit_task(item_id, due_at=datetime.combine(day, datetime.fromisoformat(task.due_at).time())
                              if len(task.due_at) > 10 else day)

    def keep_quiet(self, item_id: int) -> str:
        """'Still needed? Keep': open, no more reminders; the brief and the weekly review still list it."""
        task = self.tasks.get(item_id)
        if task is None:
            raise LifeError(f"There is no item #{item_id}.")
        return f"Kept: {task.title}. No more reminders; it stays in the morning brief and the weekly review."

    def snooze_tomorrow(self, item_id: int) -> str:
        """Re-arm the reminder for tomorrow at the task's time of day (button)."""
        return self.snooze_to(item_id, self.now().date() + timedelta(days=1))

    def snooze_to(self, item_id: int, day: date) -> str:
        """Re-arm the reminder for `day` at the task's time of day (the reminder's day buttons)."""
        task = self.tasks.get(item_id)
        when = datetime.combine(day, self.tasks.day_time(task.rule_id) if task else DAY_START)
        if when <= self.now():
            raise LifeError(f"{when:%a %d %b at %H:%M} is already past.")
        return self.snooze(item_id, int((when - self.now()).total_seconds() // 60))

    # ---- reminder rules -------------------------------------------------------------------------
    def _rules_of(self, titles: list[str]) -> list[int | None]:
        """The reminder rule (id) each title falls under, by the model (classify_rules); None for none, or when there is
        no model or no specific rule."""
        rules = [(i, w) for i, w, _ in self.tasks.rules() if w != "*"]
        if not rules or not titles or self.classify_rules is None:
            return [None] * len(titles)
        try:
            picked = self.classify_rules(titles, [w for _, w in rules])
        except Exception:  # the model is down: the task keeps the default time, nothing fails
            log.exception("reminder rules: no decision")
            return [None] * len(titles)
        return [None if k is None else rules[k][0] for k in picked]

    def reminder_rule(self, words: str, at: time | None) -> str:
        """A standing preference for the time of day tasks about something are reminded ('palestra' → 19:00): every
        task the model judges to be about it, new or open, is reminded (and nagged, and snoozed) at `at` on its day.
        '*' is every task's default. `at` None removes the rule."""
        words, before = normalise_words(words), self._day_times()
        self.tasks.set_rule(words, at)
        moved = self._regroup(before)
        about = "every task is" if words == "*" else f"tasks about '{words}' are"
        said = (f"Rule: {about} reminded at {at:%H:%M}." if at
                else f"Removed the rule for '{words}': those tasks are reminded at {self.tasks.day_time():%H:%M} again.")
        return said + (" Moved: " + "; ".join(moved) if moved else "") + "\n" + self.reminder_rules()

    def remove_reminder_rule(self, rule_id: int) -> str:
        """The web UI's Remove: the rule goes, its tasks go back to the default time."""
        before = self._day_times()
        row = self.tasks.delete_rule(rule_id)
        if row is None:
            return "Already gone."
        moved = self._regroup(before)
        return f"Removed: {row[0]} → {row[1]}." + (" Moved: " + "; ".join(moved) if moved else "")

    def _day_times(self) -> dict[int, time]:
        """Each open task's time of day, before a rule changes."""
        return {t.id: self.tasks.day_time(t.rule_id) for t in self.tasks.open_tasks()}

    def _regroup(self, before: dict[int, time]) -> list[str]:
        """After the rules changed: each open task's rule decided again, and a pending reminder still at its old time of
        day (`before`) moved to the new one, same day (a time the user asked for stays)."""
        now = self.now()
        open_ = self.tasks.open_tasks()
        moved = []
        for t, rule_id in zip(open_, self._rules_of([t.title for t in open_])):
            if rule_id != t.rule_id:
                self.tasks.set_rule_of(t.id, rule_id)
            old, new_at = before.get(t.id, DAY_START), self.tasks.day_time(rule_id)
            if not t.remind_at or t.remind_at <= iso(now) or new_at == old:
                continue
            when = datetime.fromisoformat(t.remind_at)
            new = datetime.combine(when.date(), new_at)
            if when.time() == old and new > now:
                self.tasks.reschedule(t.id, remind_at=new)
                moved.append(f"#{t.id} {t.title} → {new:%a %d %b %H:%M}")
        return moved

    def reminder_rules(self) -> str:
        rules = self.tasks.rules()
        if not rules:
            return f"No reminder rules: tasks are reminded at {DAY_START:%H:%M} on their day."
        return "Reminder rules: " + "; ".join(f"{'every task' if w == '*' else w} → {at}" for _, w, at in rules)

    def undo(self) -> str:
        """Revert the latest change to a task or local appointment (/undo). No LLM."""
        undone = self.tasks.undo()
        if undone is None:
            return "Nothing to undo."
        action, task = undone
        if task is None:
            return f"↩️ Undone ({action}): removed again."
        return f"↩️ Undone ({action}): {task.line()}"

    def weekly_review(self) -> tuple[str, list[Task]]:
        """Sunday: what got done this week, overdue and soft targets, and undated tasks older than STALE_AFTER
        (returned, for keep / next week / drop buttons). No LLM."""
        now = self.now()
        out = [f"🗓 Weekly review, {now:%a %d %b}"]
        done = self.tasks.done(now - timedelta(days=7))
        out.append(f"Done this week ({len(done)}): " + ", ".join(f"✅ {t.title}" for t in done) if done
                   else "Nothing ticked off this week.")
        today = iso(datetime.combine(now.date(), time()))
        open_ = self.tasks.open_tasks()
        if overdue := [t for t in open_ if t.due_end and t.due_end < today and not t.soft]:
            out += ["", "Overdue:"] + [f"  {t.line()}" for t in overdue]
        if soft := [t for t in open_ if t.soft and t.due_at]:
            out += ["", "Targets:"] + [f"  {t.line()}" for t in soft]
        undated = [name for name, *_ in self.tasks.projects()
                   if not any(t.when for t in open_ if (t.project or "").lower() == name.lower())]
        if undated:
            out += ["", "Projects with no dated next step: " + ", ".join(undated)]
        stale = self.tasks.stale(now - STALE_AFTER)
        if stale:
            out += ["", f"No date for {STALE_AFTER.days}+ days: keep, give it next Monday, or drop?"]
            out += [f"  {t.line()}" for t in stale]
        out += ["", "Anything to plan for next week? Just write it."]
        return "\n".join(out), stale

    # ---- events from the other assistants (events.py) --------------------------------------------
    def subscribe(self, bus) -> None:
        bus.on("bill.paid", self.on_bill_paid)

    def on_bill_paid(self, event: dict) -> None:
        """A tracked bill's payment is in the bank: complete its task. Idempotent: only while the task is still at
        the due date that was paid (a repeating one has moved on after the first run)."""
        task = self.tasks.get(event["task_id"])
        if task and not task.done_at and task.due_at and task.due_at[:10] == event["due"]:
            self.tasks.complete(task.id, self.now())

    # ---- memory ---------------------------------------------------------------------------------
    def remember(self, subject: str, fact: str) -> str:
        """Save a note. The same subject and fact again is not saved twice; another note on the same subject is
        named, so a correction can forget it."""
        same, others = self.notes.same_subject(subject, fact)
        if same:
            return f"Saved note {same.line()} (it was already saved)"
        note = self.notes.add(subject, fact, self.now())
        hint = "".join(f"\n(Another note has this subject: {o.line()}. If the new one replaces it, forget [{o.id}].)"
                       for o in others[:3])
        return f"Saved note {note.line()}{hint}"

    def recall(self, query: str) -> str:
        notes = self.notes.search(query)
        return "\n".join(n.line() for n in notes) if notes else f"No notes match {query!r}."

    def forget(self, note_id: int) -> str:
        note = self.notes.delete(note_id)
        if note is None:
            raise LifeError(f"There is no note [{note_id}]. Call recall to find the right id.")
        return f"Deleted note {note.line()}"


def _day_or_time(due: date | None) -> date | None:
    """A deadline as given: a bare day stays a day; a time at midnight means the day (models write 'T00:00')."""
    if isinstance(due, datetime):
        due = due.replace(tzinfo=None)
        return due.date() if due.time() == time() else due
    return due


@contextmanager
def _google():
    """Google failures become a LifeError the model can relay ('calendar unreachable'), not a crash."""
    try:
        yield
    except (httpx.HTTPError, GoogleAuthError) as e:
        raise LifeError(f"Google Calendar is unreachable right now ({type(e).__name__}: {str(e)[:150]}). "
                        "Tell the user; nothing was changed.") from e
