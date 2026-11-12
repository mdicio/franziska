"""Scheduled jobs that still run when the box was off at their time.

python-telegram-bot's run_daily / run_monthly fire only while the bot runs: a brief due at 07:30 is lost if the box
wakes at 09:00. Every such job is registered here instead. Each run records the slot it served (jobs.sqlite); at start
and every few minutes after (a suspend/resume doesn't restart the bot) `catch_up` runs the jobs whose latest slot has
no record, but only while it is still worth it (a job that returns False wasn't done and is tried again): the brief until the evening, the evening check-in for a few hours, the
weekly summary for three days. A job older than its grace is skipped for good, so a box that was off for a week doesn't
send seven briefs. A job seen for the first time is marked done, not run, so a new job or a restart never fires one.

Times are the box's local naive time (life.now); PTB gets the same wall time with the timezone attached.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from franziska import db, obs

log = logging.getLogger(__name__)

SCHEMA = "CREATE TABLE IF NOT EXISTS ran (job TEXT PRIMARY KEY, slot TEXT NOT NULL);"
CHECK_EVERY_S = 300


@dataclass(frozen=True)
class Job:
    name: str
    callback: Callable[..., Awaitable[None]]
    at: time
    grace: timedelta                   # how late a catch-up run still makes sense
    days: tuple[int, ...] | None = None  # PTB numbering, 0 = Sunday; None = every day
    month_day: int | None = None       # monthly on this day instead
    quiet: bool = False                # background work: run late without telling the user

    def latest_slot(self, now: datetime) -> datetime:
        """The most recent time at or before `now` this job was due."""
        for back in range(0, 63):
            day = (now - timedelta(days=back)).date()
            if self.month_day is not None and day.day != self.month_day:
                continue
            if self.days is not None and (day.weekday() + 1) % 7 not in self.days:
                continue
            slot = datetime.combine(day, self.at)
            if slot <= now:
                return slot
        raise ValueError(f"{self.name}: no slot in the last 62 days")


class Scheduler:
    def __init__(self, job_queue, path: Path, clock: Callable[[], datetime], timezone: str,
                 notify: Callable[[str], Awaitable[None]]):
        self.queue, self.clock, self.tz, self.notify = job_queue, clock, ZoneInfo(timezone), notify
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)
        self.jobs: list[Job] = []

    def add(self, job: Job) -> None:
        self.jobs.append(job)
        at = job.at.replace(tzinfo=self.tz)
        if job.month_day is not None:
            self.queue.run_monthly(self._run(job), at, day=job.month_day, name=job.name)
        else:
            self.queue.run_daily(self._run(job), at, days=job.days or tuple(range(7)), name=job.name)

    def start(self) -> None:
        """Catch up once now, then keep checking."""
        self.queue.run_repeating(obs.job("catch_up", self.catch_up, quiet=True), interval=CHECK_EVERY_S, first=20,
                                 name="catch_up")

    def _run(self, job: Job):
        async def run(context) -> None:
            previous = self._last(job)
            self._mark(job, job.latest_slot(self.clock()))
            if await job.callback(context) is False:  # couldn't do it now (the desktop is off): the next check retries
                self._restore(job, previous)
        return obs.job(job.name, run)

    @db.locked
    def _restore(self, job: Job, slot: str | None) -> None:
        if slot is None:
            self.con.execute("DELETE FROM ran WHERE job = ?", (job.name,))
        else:
            self.con.execute("UPDATE ran SET slot = ? WHERE job = ?", (slot, job.name))
        self.con.commit()

    @db.locked
    def _mark(self, job: Job, slot: datetime) -> None:
        self.con.execute("INSERT INTO ran (job, slot) VALUES (?, ?) ON CONFLICT(job) DO UPDATE SET slot = excluded.slot",
                         (job.name, slot.isoformat(timespec="seconds")))
        self.con.commit()

    @db.locked
    def _last(self, job: Job) -> str | None:
        row = self.con.execute("SELECT slot FROM ran WHERE job = ?", (job.name,)).fetchone()
        return row[0] if row else None

    def missed(self, now: datetime) -> list[Job]:
        out = []
        for job in self.jobs:
            slot = job.latest_slot(now)
            last = self._last(job)
            if last is None:  # first time seen: nothing was missed, start from here
                self._mark(job, slot)
            elif last < slot.isoformat(timespec="seconds") and now - slot <= job.grace:
                out.append(job)
            elif last < slot.isoformat(timespec="seconds"):
                log.info("catch-up: %s due %s is too old, skipped", job.name, slot)
                self._mark(job, slot)
        return out

    async def catch_up(self, context) -> None:
        late = self.missed(self.clock())
        if not late:
            return
        log.info("catch-up: running %s", ", ".join(j.name for j in late))
        obs.event("catch-up", jobs=[j.name for j in late])  # each job is a run of its own
        if shown := [j.name for j in late if not j.quiet]:
            await self.notify("🔌 I was offline at the usual time; catching up: " + ", ".join(shown) + ".")
        for job in late:
            try:
                await self._run(job)(context)
            except Exception:
                log.exception("catch-up: %s failed", job.name)
