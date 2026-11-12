"""Google Calendar as the home of appointments, with a local mirror for short ids and offline reads.

- Writes (add, move, cancel) go straight to Google, then update the mirror. Google sends the phone notifications.
- Reads (agenda, brief) come from the mirror, refreshed by a job every few minutes and after every write, over a
  window of [yesterday, +120 days] with recurring events expanded into single instances.
- Each event gets a small local id, shown as `#c5`, stable across refreshes (keyed by Google's event id). Tasks keep
  their own integer ids (`#12`); the two never collide.
- Moving or cancelling a recurring event acts on that one occurrence (Google's instance id), not the series.

Times in the mirror are naive local ISO strings, like the task store.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from franziska import db
from franziska.google.auth import GoogleClient

API = "https://www.googleapis.com/calendar/v3"
WINDOW_BACK, WINDOW_AHEAD = timedelta(days=1), timedelta(days=120)
RRULE = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_events (
    id          INTEGER PRIMARY KEY,
    google_id   TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT,
    all_day     INTEGER NOT NULL DEFAULT 0,
    location    TEXT,
    recurring   INTEGER NOT NULL DEFAULT 0,
    synced_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_sync (calendar_id TEXT PRIMARY KEY, synced_at TEXT NOT NULL);
"""


@dataclass(frozen=True)
class Appointment:
    id: int
    google_id: str
    title: str
    starts_at: str
    ends_at: str | None
    all_day: bool
    location: str | None
    recurring: bool

    @property
    def ref(self) -> str:
        return f"c{self.id}"

    @property
    def when(self) -> str:
        return self.starts_at

    def line(self) -> str:
        start = datetime.fromisoformat(self.starts_at)
        if self.all_day:
            span = f"{start:%a %d %b} (all day)"
        else:
            span = f"{start:%a %d %b %H:%M}" + (f"–{self.ends_at[11:16]}" if self.ends_at else "")
        bits = [f"#{self.ref} 📅 {self.title}", span]
        if self.location:
            bits.append(f"@ {self.location}")
        if self.recurring:
            bits.append("repeats")
        return " · ".join(bits)


class GoogleCalendar:
    def __init__(self, client: GoogleClient, path: Path, calendar_id: str = "primary", timezone: str = "Europe/Rome",
                 clock: Callable[[], datetime] | None = None):
        self.client, self.calendar_id, self.tz = client, calendar_id, ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(self.tz).replace(tzinfo=None))  # Life gives it its own
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)

    @property
    def _events_url(self) -> str:
        return f"{API}/calendars/{quote(self.calendar_id, safe='')}/events"

    # ---- time conversion ------------------------------------------------------------------------
    def _to_google(self, dt: datetime) -> dict:
        return {"dateTime": dt.replace(tzinfo=self.tz).isoformat(), "timeZone": str(self.tz)}

    def _from_google(self, t: dict) -> tuple[str, bool]:
        if "date" in t:
            return f"{t['date']}T00:00", True
        dt = datetime.fromisoformat(t["dateTime"]).astimezone(self.tz).replace(tzinfo=None)
        return dt.isoformat(timespec="minutes"), False

    # ---- writes (Google first, then the mirror) -------------------------------------------------
    def add(self, title: str, starts_at: datetime, ends_at: datetime | None = None, location: str | None = None,
            remind_minutes_before: int | None = 60, recur: str | None = None, notes: str | None = None
            ) -> Appointment:
        body = {
            "summary": title,
            "start": self._to_google(starts_at),
            "end": self._to_google(ends_at or starts_at + timedelta(hours=1)),
            "reminders": {"useDefault": False, "overrides": [] if remind_minutes_before is None else
                          [{"method": "popup", "minutes": remind_minutes_before}]},
        }
        if location:
            body["location"] = location
        if notes:
            body["description"] = notes
        if recur:
            body["recurrence"] = [f"RRULE:FREQ={RRULE[recur]}"]
        event = self.client.request("POST", self._events_url, json=body).json()
        if recur:  # the series itself is not listed; its instances are
            # Not centred on starts_at: refresh drops every row outside its window, so a series starting in a few
            # days would remove today's and tomorrow's events from the mirror until the next sync.
            self.refresh(min(starts_at, self.clock()))
            first = self._row("google_id LIKE ? ORDER BY starts_at LIMIT 1", (event["id"] + "_%",))
            if first:
                return first
        return self._upsert(event)

    def move(self, appt: Appointment, starts_at: datetime, ends_at: datetime | None = None) -> Appointment:
        if ends_at is None and appt.ends_at:
            ends_at = starts_at + (datetime.fromisoformat(appt.ends_at) - datetime.fromisoformat(appt.starts_at))
        body = {"start": self._to_google(starts_at), "end": self._to_google(ends_at or starts_at + timedelta(hours=1))}
        event = self.client.request("PATCH", f"{self._events_url}/{quote(appt.google_id, safe='')}", json=body).json()
        return self._upsert(event)

    def cancel(self, appt: Appointment) -> None:
        self.client.request("DELETE", f"{self._events_url}/{quote(appt.google_id, safe='')}")
        with self.lock:
            self.con.execute("DELETE FROM calendar_events WHERE id = ?", (appt.id,))
            self.con.commit()

    # ---- mirror ---------------------------------------------------------------------------------
    def refresh(self, now: datetime) -> int:
        """Re-read the window from Google; returns how many events it holds. Ids of known events are kept."""
        start, end = now - WINDOW_BACK, now + WINDOW_AHEAD
        params = {"singleEvents": "true", "orderBy": "startTime", "maxResults": "250",
                  "timeMin": start.replace(tzinfo=self.tz).isoformat(),
                  "timeMax": end.replace(tzinfo=self.tz).isoformat()}
        events, page = [], None
        while True:
            paging = {"pageToken": page} if page else {}
            body = self.client.request("GET", self._events_url, params={**params, **paging}).json()
            events += body.get("items", [])
            page = body.get("nextPageToken")
            if not page:
                break
        with self.lock:
            seen = {self._upsert(e, commit=False).google_id for e in events if e.get("status") != "cancelled"}
            stale = [r[0] for r in self.con.execute("SELECT google_id FROM calendar_events") if r[0] not in seen]
            self.con.executemany("DELETE FROM calendar_events WHERE google_id = ?", [(g,) for g in stale])
            self.con.execute("INSERT INTO calendar_sync VALUES (?, ?) ON CONFLICT(calendar_id) DO UPDATE SET"
                             " synced_at = excluded.synced_at", (self.calendar_id, now.isoformat(timespec="minutes")))
            self.con.commit()
        return len(seen)

    @db.locked
    def synced_at(self) -> str | None:
        row = self.con.execute("SELECT synced_at FROM calendar_sync WHERE calendar_id = ?",
                               (self.calendar_id,)).fetchone()
        return row[0] if row else None

    def events(self, start: datetime, end: datetime) -> list[Appointment]:
        """Mirrored events starting in [start, end); all-day events count from their day's midnight."""
        return self._rows("starts_at >= ? AND starts_at < ? ORDER BY starts_at, id",
                          (start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")))

    def get(self, local_id: int) -> Appointment | None:
        return self._row("id = ?", (local_id,))

    @db.locked
    def _rows(self, where: str, args: tuple) -> list[Appointment]:
        sql = ("SELECT id, google_id, title, starts_at, ends_at, all_day, location, recurring FROM calendar_events"
               f" WHERE {where}")
        return [Appointment(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6], bool(r[7]))
                for r in self.con.execute(sql, args)]

    def _row(self, where: str, args: tuple) -> Appointment | None:
        rows = self._rows(where, args)
        return rows[0] if rows else None

    @db.locked
    def _upsert(self, event: dict, commit: bool = True) -> Appointment:
        starts_at, all_day = self._from_google(event["start"])
        ends_at = self._from_google(event["end"])[0] if event.get("end") else None
        self.con.execute(
            "INSERT INTO calendar_events"
            " (google_id, title, starts_at, ends_at, all_day, location, recurring, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(google_id) DO UPDATE SET title = excluded.title,"
            " starts_at = excluded.starts_at, ends_at = excluded.ends_at, all_day = excluded.all_day,"
            " location = excluded.location, recurring = excluded.recurring, synced_at = excluded.synced_at",
            (event["id"], event.get("summary") or "(no title)", starts_at, ends_at, int(all_day), event.get("location"),
             int("recurringEventId" in event or "recurrence" in event), self.clock().date().isoformat()),
        )
        if commit:
            self.con.commit()
        return self._row("google_id = ?", (event["id"],))
