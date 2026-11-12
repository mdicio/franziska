"""The nightly backup (bot.py's 04:00 job, no model): every SQLite store but the traces, and the memory files, into
one dated folder.

SQLite files are copied with the backup API, so a store being written meanwhile is still copied consistently. The
Google token is not copied (it is a credential; signing in again recreates it). Only folders named like a date are
pruned, so anything else under the backup folder is left alone.

Run by hand: uv run --env-file .env python -m franziska.backup
"""

import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path

from franziska.config import Settings

DATED = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def target(settings: Settings) -> Path:
    return settings.backup_dir or settings.data_dir / "backup"


def run(settings: Settings, today: date) -> Path:
    """Write today's folder (replacing one written earlier today) and prune old ones; returns the folder."""
    dest = target(settings) / today.isoformat()
    tmp = dest.with_name(dest.name + ".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for db in sorted(settings.data_dir.glob("*.sqlite")):
        if db == settings.traces_db:  # debug data that expires in 30 days, and the largest file
            continue
        src, out = sqlite3.connect(f"file:{db}?mode=ro", uri=True), sqlite3.connect(tmp / db.name)
        try:
            src.backup(out)
        finally:
            out.close()
            src.close()
    if settings.memory_dir.is_dir():
        shutil.make_archive(str(tmp / "memory"), "gztar", settings.memory_dir)
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    days = sorted(p for p in target(settings).iterdir() if p.is_dir() and DATED.match(p.name))
    for old in days[:-settings.backup_keep]:
        shutil.rmtree(old)
    return dest


if __name__ == "__main__":
    print(run(Settings.from_env(), date.today()))
