"""The secretary's tools (schedule + notes) as an MCP server over stdio, for pi, Claude Code, Codex or any MCP client.

    uv run --directory /path/to/franziska python -m franziska.mcp_server

Same `Life` methods and the same SQLite file as the Telegram bot. Finance is deliberately not exposed: clients of
this server may be cloud models, and the ledger stays local. The market reports are (market_report): they come from
market.sqlite, public data only (DESIGN decision 27), though their holdings section names what is held. A task added without a priority gets the bot's guess
(router.py, on the local model), unless the desktop is off; then it is 'normal'. Only the task's title and notes go
to the local model, never to the client.
"""

import os
from datetime import date, datetime, time
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from franziska.compute import Desktop
from franziska.config import Settings
from franziska.market import store as market_store
from franziska.router import Router
from franziska.secretary.service import Clearable, Life, LifeError, Priority, Recur

READ = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


# Times are plain strings, parsed here. A `datetime` parameter would be declared as JSON-schema `date-time`
# (RFC 3339, offset required), and strict clients (pi) then reject the local '2026-10-26T10:00' models naturally
# write, before the call ever reaches us. Seen live: two rejected calls, then the model claimed success anyway.
LocalTime = Annotated[str, Field(description="Local time, 'YYYY-MM-DDTHH:MM' (seconds and offsets are accepted)")]
LocalDate = Annotated[str, Field(description="Local date, 'YYYY-MM-DD'")]
DayOrTime = Annotated[str, Field(description="A local date 'YYYY-MM-DD' when only the day is known, else a local "
                                             "time 'YYYY-MM-DDTHH:MM'")]


def _time(value: str | None, tz: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(f"Could not read the time {value!r}; use 'YYYY-MM-DDTHH:MM' in local time.") from None
    return dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None) if dt.tzinfo else dt


def _day_or_time(value: str | None, tz: str) -> date | None:
    return _date(value) if value and len(value.strip()) == 10 else _time(value, tz)


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value.strip()[:10]) if value else None
    except ValueError:
        raise ToolError(f"Could not read the date {value!r}; use 'YYYY-MM-DD'.") from None


def _call(fn, *args) -> str:
    """LifeError → ToolError: mcp shows a ToolError's message to the model, but hides any other exception's."""
    try:
        return fn(*args)
    except LifeError as e:
        raise ToolError(str(e)) from e


def build_server(life: Life, market_db: Path | None = None) -> MCPServer:
    tz = life.timezone
    server = MCPServer(
        "life",
        instructions=(
            "The user's personal schedule (appointments, tasks, reminders) and saved notes. Times are local "
            f"({life.timezone}), ISO 'YYYY-MM-DDTHH:MM'; call now before resolving relative dates. "
            "'Remind me to X at T' is a task with remind_at, not an appointment. Appointments are things that happen "
            "at a time (dentist, dinner, meeting). Call agenda before stating what is planned and recall before "
            "stating a saved fact. Ids come from agenda (#12 a task, #c5 a Google Calendar appointment) and recall "
            "([n])."
        ),
    )

    @server.tool(annotations=READ)
    def now() -> str:
        """The current local date and time. Call it before resolving relative dates like 'tomorrow'."""
        return f"{life.now():%A %Y-%m-%d %H:%M} ({life.timezone})"

    @server.tool(annotations=READ)
    def agenda(start: LocalDate | None = None, days: int = 7) -> str:
        """Appointments and tasks from `start` (default today) for `days` days, plus overdue and undated tasks."""
        return _call(life.agenda, _date(start), days)

    @server.tool(annotations=WRITE)
    def add_appointment(title: str, starts_at: LocalTime, ends_at: LocalTime | None = None,
                        location: str | None = None, remind_minutes_before: int | None = 60,
                        recur: Recur | None = None, notes: str | None = None) -> str:
        """Add an appointment: something that happens at a time (dentist, dinner, meeting). A reminder is sent
        `remind_minutes_before` the start (null: none). For 'remind me to do X' use add_task with remind_at instead."""
        return _call(life.add_appointment, title, _time(starts_at, tz), _time(ends_at, tz), location,
                     remind_minutes_before, recur, notes)

    @server.tool(annotations=WRITE)
    def add_task(title: str, due_at: DayOrTime | None = None, soft: bool = False, priority: Priority | None = None,
                 remind_at: LocalTime | None = None, recur: Recur | None = None, notes: str | None = None) -> str:
        """Add a to-do, e.g. 'call the bank'. due_at is the deadline (a bare date when no time is given); soft=true
        when it is only a target ('within a month'). Reminders follow from the date and priority and repeat until
        done; give remind_at only for a time the user names. Leave priority out unless the user states it."""
        return _call(life.add_task, title, _day_or_time(due_at, tz), _time(remind_at, tz), recur, notes, priority,
                     soft)

    @server.tool(annotations=WRITE)
    def edit_task(item_id: int | str, title: str | None = None, due_at: DayOrTime | None = None,
                  soft: bool | None = None, priority: Priority | None = None, remind_at: LocalTime | None = None,
                  recur: Recur | None = None, notes: str | None = None, nag: bool | None = None,
                  clear: list[Clearable] | None = None) -> str:
        """Change a task; pass only what changes. nag=false stops repeated reminders. clear removes fields."""
        return _call(life.edit_task, item_id, title, _day_or_time(due_at, tz), soft, priority, _time(remind_at, tz),
                     recur, notes, nag, clear)

    @server.tool(annotations=READ)
    def open_tasks() -> str:
        """Every open task with id, priority, deadline and next reminder, grouped by priority."""
        return _call(life.open_list)

    @server.tool(annotations=READ)
    def plan_overview(day: LocalDate | None = None) -> str:
        """For planning: every open task by priority, the agenda of `day` (default tomorrow), what was done today."""
        return _call(life.plan_overview, _date(day))

    @server.tool(annotations=READ)
    def done_tasks(days: int = 7) -> str:
        """Tasks completed in the last `days` days."""
        return _call(life.done_list, days)

    @server.tool(annotations=WRITE)
    def complete(item_id: int | str) -> str:
        """Mark a task or appointment done (a repeating one moves to its next occurrence)."""
        return _call(life.complete, item_id)

    @server.tool(annotations=WRITE)
    def move_appointment(item_id: int | str, starts_at: LocalTime, ends_at: LocalTime | None = None) -> str:
        """Move an appointment to a new time (#c5 or #12). Tasks change with edit_task (due_at, remind_at)."""
        return _call(life.move_appointment, item_id, _time(starts_at, tz), _time(ends_at, tz))

    @server.tool(annotations=DELETE)
    def cancel(item_id: int | str) -> str:
        """Delete an appointment or task."""
        return _call(life.cancel, item_id)

    @server.tool(annotations=WRITE)
    def reminder_rule(words: str, at: Annotated[str, Field(description="Time of day 'HH:MM'")] | None = None) -> str:
        """A standing preference for the time of day tasks about something are reminded ('palestra' → 19:00; '*'
        every task). Which tasks it covers is decided by the local model. Leave `at` out to remove the rule."""
        return _call(life.reminder_rule, words, time.fromisoformat(at) if at else None)

    @server.tool(annotations=WRITE)
    def remember(subject: str, fact: str) -> str:
        """Save a fact the user wants kept (a code, a size, a document number, where something is)."""
        return _call(life.remember, subject, fact)

    @server.tool(annotations=READ)
    def recall(query: str) -> str:
        """Search saved notes by keywords."""
        return _call(life.recall, query)

    @server.tool(annotations=DELETE)
    def forget(note_id: int) -> str:
        """Delete a saved note."""
        return _call(life.forget, note_id)

    if market_db:
        @server.tool(annotations=READ)
        def market_report(edition: Literal["daily", "weekly"] = "daily", day: LocalDate | None = None) -> str:
            """A market report the bot made (daily on weekdays, the weekly edition on Mondays): markets, rates, FX,
            commodities, the news by topic, the holdings' news, what to watch, numbered sources with links. The
            latest one on or before `day` (default today). No portfolio values: those stay local."""
            return market_store.read_report(market_db, edition, _date(day) or life.now().date())

    return server


def main() -> None:
    settings = Settings.from_env()
    life = Life.from_settings(settings)
    if clock_file := os.environ.get("FRANZISKA_CLOCK_FILE"):  # evals: the simulated time, written each step
        life.clock = lambda: datetime.fromisoformat(Path(clock_file).read_text().strip())
    if settings.router:
        router, desktop = Router.from_settings(settings), Desktop.from_settings(settings)
        # Clients often work here because the desktop is off (pi on ChatGPT): don't wait on a model that isn't there.
        life.prioritize = lambda task: router.priority(task) if desktop.is_up_now() else None
        life.classify_rules = lambda tasks, rules: (router.reminder_rules(tasks, rules) if desktop.is_up_now()
                                                    else [None] * len(tasks))
    build_server(life, settings.market_db).run()


if __name__ == "__main__":
    main()
