"""Tasks and appointments for the secretary, in one table. Times are naive local ISO strings ('YYYY-MM-DDTHH:MM')
in the user's timezone; a task's deadline may be a bare day ('YYYY-MM-DD', due by the end of it).

- A task has an optional deadline (`due_at`); an event (appointment) has `starts_at`, optionally `ends_at`/`location`.
- Either can carry one reminder (`remind_at`), sent once by a deterministic job.
- `recur` ('daily'|'weekly'|'monthly'|'yearly') repeats an item. A recurring task moves to its next occurrence when
  completed; a recurring event moves once its start has passed. A reminder that was never acted on moves forward
  when the next occurrence's reminder time comes, so a missed week is skipped rather than nagged about forever.
- An open, non-recurring task nags: once its reminder goes off it is re-armed by priority (high every 2 h, normal
  daily, low every 2 days, at DAY_START, never after DAY_END) until done. A soft deadline ('within a month') nudges
  weekly until its target day, then nags by priority. `nag = 0` turns this off for one task.
- For planning, a task may carry an estimate (`minutes`) and a `project` (a free-text name grouping the steps
  towards one outcome); Life.plan_overview fits estimates into the day's free time.
"""

import calendar
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

from franziska import db

RECURRENCES = ("daily", "weekly", "monthly", "yearly")
PRIORITIES = ("high", "normal", "low")
DAY_START, DAY_END = time(8, 30), time(21, 0)  # nagging stays inside these hours
NAG_EVERY = {"high": timedelta(hours=2), "normal": timedelta(days=1), "low": timedelta(days=2)}
REMIND_BEFORE = timedelta(minutes=15)  # a deadline with a time is reminded this long before it
UNDATED_NAG_DAYS = 3  # an undated task is nagged this many days (high: every 2 h the first), then asked about

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'task' CHECK (kind IN ('task', 'event')),
    title      TEXT NOT NULL,
    notes      TEXT,
    starts_at  TEXT,                  -- events only
    ends_at    TEXT,                  -- events only, optional
    location   TEXT,                  -- events only, optional
    due_at     TEXT,                  -- tasks only: deadline, optional
    remind_at  TEXT,                  -- when to ping the user, optional
    reminded   INTEGER NOT NULL DEFAULT 0,
    recur      TEXT CHECK (recur IN ('daily', 'weekly', 'monthly', 'yearly')),
    done_at    TEXT,
    created_at TEXT NOT NULL,
    priority   TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal', 'low')),
    soft       INTEGER NOT NULL DEFAULT 0,  -- due_at is a target ('within a month'), not a hard deadline
    nag        INTEGER NOT NULL DEFAULT 1,  -- re-remind until done
    minutes    INTEGER,               -- tasks only: estimated effort, optional
    project    TEXT,                  -- tasks only: the project it is a step of, optional
    snoozed_from TEXT,                -- repeating items: the reminder time a snooze moved (their schedule stays)
    nagging_since TEXT,               -- when this run of reminders began; any change by the user ends it
    rule_id    INTEGER                -- tasks only: the reminder rule it follows (reminder_rules.id), decided when added
);
-- /undo: the row as it was before each change by the user, a tool or a button (not the reminder job's re-arming)
CREATE TABLE IF NOT EXISTS undo (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    action  TEXT NOT NULL,            -- added, edited, done, deleted, moved
    row     TEXT                      -- JSON of the whole row before; NULL when the change created it
);
-- Standing preferences for the time of day a task is reminded ('gym things always in the evening'): a task that follows
-- one (tasks.rule_id, an AnyJev decision on its title: Life.reminder_rule) is reminded, nagged and snoozed at `at`
-- instead of DAY_START. words '*' is every task's default.
CREATE TABLE IF NOT EXISTS reminder_rules (
    id    INTEGER PRIMARY KEY,
    words TEXT NOT NULL UNIQUE,       -- what the rule is about, as the user said it ('palestra'), lower case; or '*'
    at    TEXT NOT NULL               -- 'HH:MM'
);
"""
UNDO_KEEP = 50
# Columns added after the first release, for files created before them.
MIGRATIONS = {
    "priority": "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'"
                " CHECK (priority IN ('high', 'normal', 'low'))",
    "soft": "ALTER TABLE tasks ADD COLUMN soft INTEGER NOT NULL DEFAULT 0",
    "nag": "ALTER TABLE tasks ADD COLUMN nag INTEGER NOT NULL DEFAULT 1",
    "minutes": "ALTER TABLE tasks ADD COLUMN minutes INTEGER",
    "project": "ALTER TABLE tasks ADD COLUMN project TEXT",
    "snoozed_from": "ALTER TABLE tasks ADD COLUMN snoozed_from TEXT",
    "nagging_since": "ALTER TABLE tasks ADD COLUMN nagging_since TEXT",
    "rule_id": "ALTER TABLE tasks ADD COLUMN rule_id INTEGER",
}


@dataclass(frozen=True)
class Task:
    id: int
    kind: str
    title: str
    notes: str | None
    starts_at: str | None
    ends_at: str | None
    location: str | None
    due_at: str | None
    remind_at: str | None
    recur: str | None
    done_at: str | None
    priority: str = "normal"
    soft: bool = False
    nag: bool = True
    minutes: int | None = None
    project: str | None = None
    snoozed_from: str | None = None
    nagging_since: str | None = None
    rule_id: int | None = None

    @property
    def due_end(self) -> str | None:
        """The deadline as a time: a bare day ends at 23:59."""
        return end_of(self.due_at)

    @property
    def when(self) -> str | None:
        """The time the item is about: an event's start, else a task's deadline, else its reminder."""
        return self.starts_at or self.due_end or self.remind_at

    def missed(self, now: datetime) -> bool:
        """An open task the bot should ask 'still needed?' about instead of reminding: its deadline or target day is
        over (not the day itself: a reminder snoozed past a 09:00 deadline still comes as a reminder), or, undated,
        it has been nagged UNDATED_NAG_DAYS with no answer. A repeating one moves on instead."""
        if self.kind != "task" or self.recur or self.done_at:
            return False
        if self.due_at:
            return self.due_at[:10] < now.date().isoformat()
        return bool(self.nagging_since) and now.date() >= _ask_day(self.nagging_since)

    @property
    def occurrence(self) -> str | None:
        """A repeating item's current occurrence: `when`, with a snoozed reminder at its scheduled time."""
        return self.starts_at or self.due_end or self.snoozed_from or self.remind_at

    def line(self) -> str:
        mark = {"high": "🔴 ", "low": "⚪ "}.get(self.priority, "") if self.kind == "task" else "📅 "
        bits = [f"#{self.id} {mark}{self.title}"]
        if self.starts_at:
            bits.append(_fmt(self.starts_at) + (f"–{self.ends_at[11:]}" if self.ends_at else ""))
        if self.location:
            bits.append(f"@ {self.location}")
        if self.project:
            bits.append(f"project {self.project}")
        if self.kind == "task" and self.priority != "normal":
            bits.append(f"{self.priority} priority")
        if self.minutes:
            bits.append(f"~{duration(self.minutes)}")
        if self.due_at:
            bits.append(f"{'target' if self.soft else 'due'} {_fmt(self.due_at)}")
        if self.remind_at and not self.done_at:
            bits.append(f"reminder {_fmt(self.remind_at)}")
        if self.recur:
            bits.append(f"repeats {self.recur}")
        elif self.kind == "task" and not self.nag and not self.done_at:
            bits.append("no nagging")
        if self.done_at:
            bits.append("done")
        return " · ".join(bits)


def next_nag(now: datetime, priority: str, target: str | None = None, at: time = DAY_START) -> datetime:
    """When to remind about an open task next. Before a soft target day: the next Monday. Otherwise by priority,
    moved into at..DAY_END (`at`: the task's time of day, TaskStore.day_time)."""
    if target and target[:10] > now.date().isoformat():
        monday = now.date() + timedelta(days=7 - now.weekday())
        return datetime.combine(monday, at)
    nxt = now + NAG_EVERY.get(priority, NAG_EVERY["normal"])
    if priority != "high":
        return datetime.combine(nxt.date(), at)
    if nxt.date() > now.date() or nxt.time() > max(DAY_END, at):
        return datetime.combine(now.date() + timedelta(days=1), at)
    return max(nxt, datetime.combine(nxt.date(), at))


def first_reminder(now: datetime, priority: str, due: str | None, soft: bool, at: time = DAY_START) -> datetime | None:
    """The reminder a new (or re-dated) task gets when none was asked for: a deadline's (due_reminder) whatever the
    priority; else high priority nags from now, a soft target weekly before it; undated ones wait for the brief."""
    if due and not soft:
        return due_reminder(due, now, at) or next_nag(now, priority, at=at)
    if priority == "high":
        return next_nag(now, priority, at=at)
    if due:
        return next_nag(now, priority, due, at)
    return None


def due_reminder(due: str, now: datetime, at: time = DAY_START) -> datetime | None:
    """A deadline's own reminder, if it is still ahead: REMIND_BEFORE a due time (the time itself when that is
    closer), `at` on a bare due day."""
    if len(due) > 10:
        when = _dt(due)
        return next((t for t in (when - REMIND_BEFORE, when) if t > now), None)
    on_day = datetime.combine(date.fromisoformat(due), at)
    return on_day if on_day > now else None


def ask_at(task: Task, at: time = DAY_START) -> datetime:
    """When a task past its deadline gets its one 'still needed?': its time of day, the day after."""
    return datetime.combine(date.fromisoformat(task.due_at[:10]) + timedelta(days=1), at)


def day_reminder(due: str, now: datetime, at: time = DAY_START) -> datetime:
    """A repeating task's reminder: `at` on its due day (they don't nag; each occurrence reminds once). Added after
    that on the due day itself, it goes off now rather than skipping this occurrence."""
    return max(datetime.combine(date.fromisoformat(due[:10]), at), now)


def normalise_words(words: str) -> str:
    """'Palestra, gym ' → 'gym, palestra'; '*' and an empty string → '*'."""
    out = sorted({w.strip().lower() for w in words.split(",") if w.strip() and w.strip() != "*"})
    return ", ".join(out) or "*"


def duration(minutes: int) -> str:
    """90 → '1 h 30', 45 → '45 min'."""
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d}" if h and m else f"{h} h" if h else f"{m} min"


def advance(dt: datetime, recur: str, steps: int = 1) -> datetime:
    """`dt` moved forward by `steps` periods. Monthly/yearly keep the day, clamped to the month's last day."""
    if recur == "daily":
        return dt + timedelta(days=steps)
    if recur == "weekly":
        return dt + timedelta(weeks=steps)
    months = steps * (1 if recur == "monthly" else 12)
    y, m = divmod(dt.month - 1 + months, 12)
    y, m = dt.year + y, m + 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


class TaskStore:
    def __init__(self, path: Path, clock: Callable[[], datetime] = datetime.now):
        self.con, self.lock = db.connect(path), db.lock()
        self.clock = clock  # the undo log's timestamps; Life gives it its own clock
        self.con.executescript(SCHEMA)
        have = {r[1] for r in self.con.execute("PRAGMA table_info(tasks)")}
        for col, sql in MIGRATIONS.items():
            if col not in have:
                self.con.execute(sql)
        self.con.commit()

    _COLS = ("id, kind, title, notes, starts_at, ends_at, location, due_at, remind_at, recur, done_at, priority,"
             " soft, nag, minutes, project, snoozed_from, nagging_since, rule_id")
    _EDITABLE = ("title", "notes", "due_at", "remind_at", "recur", "priority", "soft", "nag", "minutes", "project",
                 "rule_id")

    def _rows(self, where: str, args: tuple = (), order: str = "COALESCE(starts_at, due_at, remind_at), id"):
        sql = f"SELECT {self._COLS} FROM tasks WHERE {where} ORDER BY {order}"
        return [Task(*r) for r in self.con.execute(sql, args)]

    @db.locked
    def add(self, title: str, now: datetime, due_at: date | None = None, remind_at: datetime | None = None,
            notes: str | None = None, *, kind: str = "task", starts_at: datetime | None = None,
            ends_at: datetime | None = None, location: str | None = None, recur: str | None = None,
            priority: str = "normal", soft: bool = False, nag: bool = True, minutes: int | None = None,
            project: str | None = None, rule_id: int | None = None) -> Task:
        cur = self.con.execute(
            "INSERT INTO tasks (kind, title, notes, starts_at, ends_at, location, due_at, remind_at, recur, created_at,"
            " priority, soft, nag, minutes, project, rule_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, title.strip(), notes, iso(starts_at), iso(ends_at), location, iso(due_at), iso(remind_at),
             recur, iso(now), priority, int(soft), int(nag), minutes, project and project.strip(), rule_id),
        )
        self._save(cur.lastrowid, "added", created=True)
        self.con.commit()
        return self.get(cur.lastrowid)

    @db.locked
    def get(self, task_id: int) -> Task | None:
        rows = self._rows("id = ?", (task_id,))
        return rows[0] if rows else None

    @db.locked
    def update(self, task_id: int, **fields) -> Task | None:
        """Set any of _EDITABLE (None clears a field). A new reminder time re-arms the reminder."""
        bad = set(fields) - set(self._EDITABLE)
        if bad:
            raise ValueError(f"not editable: {bad}")
        if not fields:
            return self.get(task_id)
        values = [iso(v) if isinstance(v, date) else int(v) if isinstance(v, bool) else v for v in fields.values()]
        sets = ", ".join(f"{k} = ?" for k in fields) + (", reminded = 0, snoozed_from = NULL" if "remind_at" in fields
                                                         else "") + ", nagging_since = NULL"
        self._save(task_id, "edited")
        self.con.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*values, task_id))
        self.con.commit()
        return self.get(task_id)

    @db.locked
    def done(self, since: datetime) -> list[Task]:
        """Tasks completed since `since`, latest first."""
        return self._rows("kind = 'task' AND done_at >= ?", (iso(since),), "done_at DESC")

    @db.locked
    def open_tasks(self, until: datetime | None = None) -> list[Task]:
        """Undone tasks (not events), soonest first; undated last. `until` keeps only those due/reminded by then."""
        where, args = "kind = 'task' AND done_at IS NULL", ()
        if until:
            where += " AND COALESCE(due_at, remind_at) <= ?"
            args = (iso(until),)
        return self._rows(where, args, "COALESCE(due_at, remind_at) IS NULL, COALESCE(due_at, remind_at), id")

    @db.locked
    def events(self, start: datetime, end: datetime) -> list[Task]:
        """Appointments starting in [start, end), in order."""
        return self._rows("kind = 'event' AND done_at IS NULL AND starts_at >= ? AND starts_at < ?",
                          (iso(start), iso(end)))

    @db.locked
    def complete(self, task_id: int, now: datetime, stop: bool = False) -> Task | None:
        """Mark done. A recurring item instead moves to its first occurrence after now, with its reminder re-armed;
        `stop` ends the repetition and marks it done."""
        task = self.get(task_id)
        if task is None or task.done_at:
            return task
        self._save(task_id, "done")
        if task.recur and not stop:
            self._shift(task, _steps(_dt(task.occurrence), task.recur, lambda t: t > now, minimum=1))
        else:
            self.con.execute("UPDATE tasks SET done_at = ?, recur = NULL, snoozed_from = NULL WHERE id = ?",
                             (iso(now), task_id))
        self.con.commit()
        return self.get(task_id)

    @db.locked
    def delete(self, task_id: int) -> Task | None:
        """Remove an item entirely (a cancelled appointment, a task added by mistake). Returns what was removed."""
        task = self.get(task_id)
        if task:
            self._save(task_id, "deleted")
            self.con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.con.commit()
        return task

    @db.locked
    def reschedule(self, task_id: int, due_at: datetime | None = None, remind_at: datetime | None = None, *,
                   starts_at: datetime | None = None, ends_at: datetime | None = None,
                   snooze: bool = False) -> Task | None:
        """Move times; fields left as None stay as they are. Any change re-arms the reminder. `snooze`: the new
        reminder is for this occurrence only, a repeating item keeps its schedule (snoozed_from)."""
        self._save(task_id, "moved")
        # SQLite evaluates every SET expression on the row as it was, so snoozed_from takes the old remind_at.
        snoozed = ("CASE WHEN recur IS NULL THEN NULL ELSE COALESCE(snoozed_from, remind_at) END" if snooze
                   else "CASE WHEN ? IS NULL THEN snoozed_from END")
        self.con.execute(
            "UPDATE tasks SET due_at = COALESCE(?, due_at), remind_at = COALESCE(?, remind_at),"
            f" starts_at = COALESCE(?, starts_at), ends_at = COALESCE(?, ends_at), reminded = 0,"
            f" snoozed_from = {snoozed}, nagging_since = NULL WHERE id = ?",
            (iso(due_at), iso(remind_at), iso(starts_at), iso(ends_at), *(() if snooze else (iso(remind_at),)),
             task_id),
        )
        self.con.commit()
        return self.get(task_id)

    @db.locked
    def roll_recurring_events(self, now: datetime) -> None:
        """Recurring appointments whose start has passed move to their next occurrence (events are never 'done')."""
        for task in self._rows("kind = 'event' AND recur IS NOT NULL AND done_at IS NULL AND starts_at < ?",
                               (iso(now),)):
            self._shift(task, _steps(_dt(task.starts_at), task.recur, lambda t: t >= now))
        self.con.commit()

    @db.locked
    def due_reminders(self, now: datetime) -> list[Task]:
        """Reminders whose time has come and that weren't sent yet. Nothing is marked: call mark_sent once each one
        is delivered, so a failed send is retried on the next run. Deterministic: no LLM in the reminder path."""
        self.roll_recurring_events(now)
        due = []
        for task in self._rows("done_at IS NULL AND remind_at <= ?", (iso(now),), "remind_at, id"):
            reminded = self.con.execute("SELECT reminded FROM tasks WHERE id = ?", (task.id,)).fetchone()[0]
            scheduled = task.snoozed_from or task.remind_at
            if reminded and task.recur and advance(_dt(scheduled), task.recur) <= now:
                # A recurring task left undone and its next occurrence is due: skip to the latest occurrence.
                rec = task.recur
                task = self._shift(task, _steps(_dt(scheduled), rec, lambda r, rec=rec: advance(r, rec) > now))
                reminded = 0
            if not reminded:
                due.append(task)
        self.con.commit()
        return due

    # ---- reminder rules -----------------------------------------------------------------------------
    @db.locked
    def rules(self) -> list[tuple[int, str, str]]:
        """(id, words, 'HH:MM'), the specific ones first, the '*' default last."""
        return self.con.execute("SELECT id, words, at FROM reminder_rules ORDER BY words = '*', id").fetchall()

    def day_time(self, rule_id: int | None = None) -> time:
        """The time of day a task following rule `rule_id` is reminded at: that rule's, else the '*' default, else
        DAY_START."""
        rows = dict((i, at) for i, w, at in self.rules() if i == rule_id or w == "*")
        at = rows.get(rule_id) or next((at for i, at in rows.items() if i != rule_id), None)
        return time.fromisoformat(at) if at else DAY_START

    @db.locked
    def set_rule(self, words: str, at: time | None) -> int | None:
        """Add or replace the rule for `words` (normalised: lower case, sorted, comma-separated); `at` None removes
        it. Returns the rule's id, None when removed."""
        words = normalise_words(words)
        if at is None:
            self.con.execute("UPDATE tasks SET rule_id = NULL WHERE rule_id IN (SELECT id FROM reminder_rules"
                             " WHERE words = ?)", (words,))
            self.con.execute("DELETE FROM reminder_rules WHERE words = ?", (words,))
            self.con.commit()
            return None
        self.con.execute("INSERT INTO reminder_rules (words, at) VALUES (?, ?) ON CONFLICT (words) DO UPDATE SET at = "
                         "excluded.at", (words, f"{at:%H:%M}"))
        self.con.commit()
        return self.con.execute("SELECT id FROM reminder_rules WHERE words = ?", (words,)).fetchone()[0]

    @db.locked
    def set_rule_of(self, task_id: int, rule_id: int | None) -> None:
        """Which rule a task follows, decided again after the rules changed: bookkeeping, not an undoable edit."""
        self.con.execute("UPDATE tasks SET rule_id = ? WHERE id = ?", (rule_id, task_id))
        self.con.commit()

    @db.locked
    def delete_rule(self, rule_id: int) -> tuple[str, str] | None:
        row = self.con.execute("SELECT words, at FROM reminder_rules WHERE id = ?", (rule_id,)).fetchone()
        self.con.execute("UPDATE tasks SET rule_id = NULL WHERE rule_id = ?", (rule_id,))
        self.con.execute("DELETE FROM reminder_rules WHERE id = ?", (rule_id,))
        self.con.commit()
        return row

    @db.locked
    def mark_sent(self, task: Task, now: datetime) -> Task:
        """A reminder was delivered: an open nagging task is re-armed by priority (it comes back until done), any
        other item is marked sent. A deadline isn't nagged before its own reminder (due_reminder); once it has
        passed, the task is asked about once (ask_at, the bot's 'still needed?'), then only the brief and the weekly
        review show it. An undated one: UNDATED_NAG_DAYS of reminders (high every 2 h the first day, then daily), then
        the same question."""
        if task.kind == "task" and task.nag and not task.recur and not task.missed(now):
            since = task.nagging_since or iso(now)
            pace = task.priority
            if not task.due_at and pace == "high" and since[:10] < now.date().isoformat():
                pace = "normal"
            at = self.day_time(task.rule_id)
            nxt = next_nag(now, pace, task.due_at if task.soft else None, at)
            if task.due_at and not task.soft:
                nxt = max(nxt, due_reminder(task.due_at, now, at) or nxt)
            if task.due_at and iso(nxt) > task.due_end:
                nxt = ask_at(task, at)
            if not task.due_at:
                nxt = min(nxt, datetime.combine(_ask_day(since), at))
            self.con.execute("UPDATE tasks SET remind_at = ?, reminded = 0, nagging_since = ? WHERE id = ?",
                             (iso(nxt), since, task.id))
        else:
            self.con.execute("UPDATE tasks SET reminded = 1 WHERE id = ?", (task.id,))
        self.con.commit()
        return self.get(task.id)

    @db.locked
    def take_due_reminders(self, now: datetime) -> list[Task]:
        """due_reminders, each marked sent at once (callers that can't fail to deliver: evals, CLIs)."""
        return [self.mark_sent(t, now) for t in self.due_reminders(now)]

    @db.locked
    def projects(self) -> list[tuple[str, int, int, int]]:
        """(project, open tasks, done tasks, open minutes estimated) for every project with an open task, by name."""
        return self.con.execute(
            "SELECT project, SUM(done_at IS NULL), SUM(done_at IS NOT NULL), SUM(CASE WHEN done_at IS NULL"
            " THEN COALESCE(minutes, 0) ELSE 0 END) FROM tasks WHERE kind = 'task' AND project IS NOT NULL"
            " GROUP BY project COLLATE NOCASE HAVING SUM(done_at IS NULL) > 0 ORDER BY project COLLATE NOCASE"
        ).fetchall()

    def _save(self, task_id: int, action: str, created: bool = False) -> None:
        """Keep the row as it is now, so undo() can put it back (uncommitted: the caller's change commits both)."""
        row = None
        if not created:
            cur = self.con.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            found = cur.fetchone()
            if found is None:
                return
            row = json.dumps(dict(zip([c[0] for c in cur.description], found)))
        self.con.execute("INSERT INTO undo (at, task_id, action, row) VALUES (?, ?, ?, ?)",
                         (iso(self.clock()), task_id, action, row))
        self.con.execute("DELETE FROM undo WHERE id <= (SELECT MAX(id) FROM undo) - ?", (UNDO_KEEP,))

    @db.locked
    def undo(self) -> tuple[str, Task | None] | None:
        """Revert the latest change: (action, the item as it is now, or None if the undo removed it). None: nothing
        to undo. Appointments in Google Calendar are not covered (only the local store)."""
        last = self.con.execute("SELECT id, task_id, action, row FROM undo ORDER BY id DESC LIMIT 1").fetchone()
        if last is None:
            return None
        undo_id, task_id, action, row = last
        if row is None:
            self.con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        else:
            old = json.loads(row)
            cols = ", ".join(old)
            self.con.execute(f"INSERT OR REPLACE INTO tasks ({cols}) VALUES ({', '.join('?' * len(old))})",
                             tuple(old.values()))
        self.con.execute("DELETE FROM undo WHERE id = ?", (undo_id,))
        self.con.commit()
        return action, self.get(task_id)

    @db.locked
    def stale(self, before: datetime) -> list[Task]:
        """Open tasks with no date at all, created before `before`: the weekly review asks about them."""
        return self._rows("kind = 'task' AND done_at IS NULL AND due_at IS NULL AND remind_at IS NULL"
                          " AND recur IS NULL AND created_at < ?", (iso(before),), "created_at, id")

    def _shift(self, task: Task, steps: int) -> Task:
        """Move every time of a recurring item forward by `steps` periods and re-arm its reminder at its scheduled
        time (a snooze was for the occurrence it moved, not the next)."""
        task = replace(task, remind_at=task.snoozed_from or task.remind_at)
        cols = ("starts_at", "ends_at", "due_at", "remind_at")
        new = [_moved(v, task.recur, steps) if (v := getattr(task, c)) else None for c in cols]
        self.con.execute(
            "UPDATE tasks SET starts_at = ?, ends_at = ?, due_at = ?, remind_at = ?, reminded = 0,"
            " snoozed_from = NULL WHERE id = ?",
            (*new, task.id),
        )
        return self.get(task.id)


def _steps(start: datetime, recur: str, ok, minimum: int = 0) -> int:
    """Fewest whole periods (at least `minimum`) after which `ok(start moved by them)` holds."""
    steps = minimum
    while not ok(advance(start, recur, steps)):
        steps += 1
    return steps


def _moved(s: str, recur: str, steps: int) -> str:
    """A stored time or bare day, `steps` periods later, in the same shape (a day-only deadline stays a day)."""
    moved = advance(_dt(s), recur, steps)
    return iso(moved.date() if len(s) == 10 else moved)


def iso(dt: date | None) -> str | None:
    """'YYYY-MM-DDTHH:MM' for a datetime, 'YYYY-MM-DD' for a bare day."""
    if not dt:
        return None
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None).isoformat(timespec="minutes")
    return dt.isoformat()


def end_of(s: str | None) -> str | None:
    """A bare day as its last minute; a time as is."""
    return f"{s}T23:59" if s and len(s) == 10 else s


def _ask_day(nagging_since: str) -> date:
    """The day an undated task nagged since then gets its 'still needed?'."""
    return date.fromisoformat(nagging_since[:10]) + timedelta(days=UNDATED_NAG_DAYS)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _fmt(s: str) -> str:
    return _dt(s).strftime("%a %d %b" if len(s) == 10 else "%a %d %b %H:%M")
