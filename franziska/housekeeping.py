"""The nightly cleanup in one place: what each store keeps, how much, and what was removed (no model).

Every store used to expire on its own rule and say nothing. `run` gives each one its pass (turn log, memory cache,
handled events, mail rows, the document cache, report PDFs, SQLite files) and writes one line per pass to
maintenance.sqlite, so `/housekeeping` can answer 'what went, and why is the disk this size'. A pass that fails is
logged and does not stop the next. Quotas are constants here; the stores keep their own rules (memory.py's unused-for-60-
days, turns.py's KEEP_DAYS) and this calls them.

The backup is not here: it has its own alert (bot.py nightly).

    uv run --env-file .env python -m franziska.housekeeping        # run it now and print what it did
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from franziska import db, obs

log = logging.getLogger(__name__)

REPORTS_KEEP_DAYS = 60
REPORTS_MAX_BYTES = 150 * 1024 * 1024
MAIL_KEEP_DAYS = 180  # triaged mail rows (header + snippet); untriaged ones wait for the model
VACUUM_EVERY_DAYS = 7
VACUUM_WHEN = 0.25  # fraction of a file that is free pages
LOG_KEEP_DAYS = 180
ARTIFACTS_KEEP_DAYS = 30  # run_python's charts and tables: sent with the reply, linked from the web chat until then

SCHEMA = """
CREATE TABLE IF NOT EXISTS cleanup (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,      -- local time, minutes
    what    TEXT NOT NULL,      -- turns | memory | events | mail | docs | library | reports | sandbox | sqlite | vacuum
    removed INTEGER NOT NULL,   -- rows or files
    freed   INTEGER NOT NULL,   -- bytes, when it can say
    note    TEXT NOT NULL,      -- names removed, or the error
    failed  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS cleanup_at ON cleanup(at);
"""


@dataclass
class Result:
    what: str
    removed: int = 0
    freed: int = 0
    note: str = ""
    failed: bool = False


class Log:
    def __init__(self, path: Path, clock: Callable[[], datetime]):
        self.clock = clock
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)

    @db.locked
    def add(self, results: list[Result]) -> None:
        at = self.clock().isoformat(timespec="minutes")
        self.con.executemany("INSERT INTO cleanup (at, what, removed, freed, note, failed) VALUES (?, ?, ?, ?, ?, ?)",
                             [(at, r.what, r.removed, r.freed, r.note[:300], int(r.failed)) for r in results])
        cutoff = (self.clock() - timedelta(days=LOG_KEEP_DAYS)).isoformat(timespec="minutes")
        self.con.execute("DELETE FROM cleanup WHERE at < ?", (cutoff,))
        self.con.commit()

    @db.locked
    def last(self, what: str) -> str | None:
        row = self.con.execute("SELECT MAX(at) FROM cleanup WHERE what = ?", (what,)).fetchone()
        return row[0]

    @db.locked
    def totals(self, days: int) -> list[tuple[str, int, int, int]]:
        """(what, removed, freed, failures) over the last `days` days."""
        since = (self.clock() - timedelta(days=days)).isoformat(timespec="minutes")
        return self.con.execute("SELECT what, SUM(removed), SUM(freed), SUM(failed) FROM cleanup WHERE at >= ?"
                                " GROUP BY what ORDER BY what", (since,)).fetchall()


# ---- the passes ---------------------------------------------------------------------------------
def _size(paths) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def trim_folder(root: Path, pattern: str, keep_days: int, max_bytes: int, now: datetime) -> tuple[int, int]:
    """Delete files older than keep_days, then the oldest until the rest fits max_bytes: (files, bytes)."""
    if not root.is_dir():
        return 0, 0
    cutoff = (now - timedelta(days=keep_days)).timestamp()
    files = sorted(((f.stat().st_mtime, f.stat().st_size, f) for f in root.glob(pattern) if f.is_file()),
                   key=lambda t: t[0])
    total, gone, freed = sum(s for _, s, _ in files), 0, 0
    for mtime, size, f in files:
        if mtime >= cutoff and total <= max_bytes:
            break
        f.unlink(missing_ok=True)
        total, gone, freed = total - size, gone + 1, freed + size
    return gone, freed


def trim_dirs(root: Path, keep_days: int, now: datetime) -> tuple[int, int]:
    """Delete the subfolders of `root` not changed for keep_days: (folders, bytes)."""
    if not root.is_dir():
        return 0, 0
    cutoff, gone, freed = (now - timedelta(days=keep_days)).timestamp(), 0, 0
    for d in root.iterdir():
        if d.is_dir() and not d.is_symlink() and d.stat().st_mtime < cutoff:
            size = _size(d.rglob("*"))
            shutil.rmtree(d, ignore_errors=True)
            gone, freed = gone + 1, freed + size
    return gone, freed


def sqlite_files(data_dir: Path, now: datetime, log_: Log) -> list[Result]:
    """Fold each store's write-ahead log back into the file; about weekly, vacuum the ones that are mostly free pages."""
    out, checked = [], log_.last("vacuum")
    due = checked is None or datetime.fromisoformat(checked) <= now - timedelta(days=VACUUM_EVERY_DAYS)
    freed = vacuumed = 0
    for path in sorted(data_dir.glob("*.sqlite")):
        before = _size([path, Path(f"{path}-wal")])
        try:
            con = sqlite3.connect(path, timeout=5)
            try:
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if due:
                    pages, free = con.execute("PRAGMA page_count").fetchone()[0], con.execute(
                        "PRAGMA freelist_count").fetchone()[0]
                    if pages and free / pages >= VACUUM_WHEN and path.stat().st_size > 1024 * 1024:
                        con.execute("VACUUM")
                        vacuumed += 1
            finally:
                con.close()
        except sqlite3.Error as e:  # busy or locked: tomorrow
            out.append(Result("sqlite", note=f"{path.name}: {e}", failed=True))
            continue
        freed += max(0, before - _size([path, Path(f"{path}-wal")]))
    out.append(Result("sqlite", freed=freed))
    if due:
        out.append(Result("vacuum", removed=vacuumed, note=f"{vacuumed} file(s) vacuumed"))
    return out


def run(core, log_: Log) -> list[Result]:
    """Every pass, in order; what each removed is written to the log. `core`: core.Core."""
    now, settings = core.life.now(), core.settings
    results: list[Result] = []

    def step(what: str, job: Callable[[], Result | list[Result]]) -> None:
        try:
            got = job()
            results.extend(got if isinstance(got, list) else [got])
        except Exception as e:
            log.exception("housekeeping: %s failed", what)
            results.append(Result(what, note=f"{type(e).__name__}: {e}", failed=True))

    def memory_pass() -> Result:
        gone = core.memory.expire()
        return Result("memory", len(gone), note=", ".join(f"{e.scope}/{e.name}" for e in gone))

    def docs_pass() -> Result:
        before = _size(settings.doc_cache_dir.glob("*.txt")) if settings.doc_cache_dir.is_dir() else 0
        n = core.doc_cache.expire()
        after = _size(settings.doc_cache_dir.glob("*.txt")) if settings.doc_cache_dir.is_dir() else 0
        return Result("docs", n, before - after)

    def library_pass() -> Result:
        gone, freed = core.library.expire()
        return Result("library", len(gone), freed, ", ".join(gone))

    def reports_pass() -> Result:
        n, freed = trim_folder(settings.reports_dir, "*.pdf", REPORTS_KEEP_DAYS, REPORTS_MAX_BYTES, now)
        return Result("reports", n, freed)

    def sandbox_pass() -> Result:  # run_python's results, and any job folder a crash left behind
        n, freed = trim_dirs(settings.sandbox_results_dir, ARTIFACTS_KEEP_DAYS, now)
        jobs, jobs_freed = trim_dirs(settings.sandbox_jobs_dir, 1, now)
        return Result("sandbox", n + jobs, freed + jobs_freed)

    step("turns", lambda: Result("turns", core.turns.expire()))
    step("memory", memory_pass)
    step("events", lambda: Result("events", core.events.expire()))
    step("traces", lambda: Result("traces", obs.expire(settings.traces_db, now)))
    if core.mail:
        step("mail", lambda: Result("mail", core.mail.expire(MAIL_KEEP_DAYS, now)))
    step("docs", docs_pass)
    step("library", library_pass)
    step("reports", reports_pass)
    step("sandbox", sandbox_pass)
    step("sqlite", lambda: sqlite_files(settings.data_dir, now, log_))
    log_.add(results)
    return results


def describe(results: list[Result]) -> str:
    return "\n".join(f"{r.what}: {'FAILED ' + r.note if r.failed else f'{r.removed} removed'}"
                     + (f", {r.freed // 1024} KB freed" if r.freed else "")
                     + (f" ({r.note})" if r.note and not r.failed else "") for r in results)


def report(core, log_: Log, days: int = 7) -> str:
    """For /housekeeping: what went in the last `days` days, and what the data takes now."""
    settings = core.settings
    lines = [f"🧹 Housekeeping, last {days} days (last run {log_.last('sqlite') or 'never'}):"]
    for what, removed, freed, failed in log_.totals(days):
        lines.append(f"• {what}: {removed or 0} removed" + (f", {freed // 1024} KB freed" if freed else "")
                     + (f", {failed} failed" if failed else ""))
    if len(lines) == 1:
        lines.append("• nothing recorded yet (it runs nightly at 04:00)")

    def folder(path: Path) -> int:
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.is_dir() else 0

    stores = sum(f.stat().st_size for f in settings.data_dir.glob("*.sqlite*") if f.is_file())
    lines.append(f"\nOn disk: stores {stores // 1024} KB · documents {folder(settings.doc_cache_dir) // 1024} KB · "
                 f"reports {folder(settings.reports_dir) // 1024} KB · memory {folder(settings.memory_dir) // 1024} KB · "
                 f"backups {folder(settings.backup_dir or settings.data_dir / 'backup') // 1024} KB")
    lines.append(f"Library: {core.library.stats()}, originals {folder(settings.data_dir / 'library') // 1024} KB")
    return "\n".join(lines)


def main() -> None:
    from franziska.config import Settings
    from franziska.core import Core

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    core = Core(Settings.from_env())
    log_ = Log(core.settings.data_dir / "maintenance.sqlite", core.life.now)
    print(describe(run(core, log_)))
    print()
    print(report(core, log_))


if __name__ == "__main__":
    main()
