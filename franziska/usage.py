"""What the cloud model costs: one row per request (tokens and the billed cost), in data/usage.sqlite.

`Metered` wraps the cloud model of one route (llm.Models), so every request counts whoever made it: an agent turn,
a workflow's extract, an eval. Only counts and names are stored, never message text. `/usage` shows `report`.
OpenRouter returns each request's cost; DeepInfra doesn't, so its rows have tokens and a NULL cost.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.wrapper import WrapperModel

from franziska import db

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,      -- local time, seconds
    route      TEXT NOT NULL,      -- the route or workflow the request was for
    model      TEXT NOT NULL,      -- the model that answered: the local one when the cloud call fell back
    provider   TEXT,               -- OpenRouter's upstream provider, when it says
    tokens_in  INTEGER NOT NULL,
    cached_in  INTEGER NOT NULL,   -- of tokens_in, read from the provider's prompt cache
    tokens_out INTEGER NOT NULL,
    cost_usd   REAL                -- as billed; NULL when the provider doesn't say
);
CREATE INDEX IF NOT EXISTS usage_at ON usage(at);
"""


class UsageLog:
    def __init__(self, path: Path, clock: Callable[[], datetime] = datetime.now):
        self.clock = clock
        self.con, self.lock = db.connect(path), db.lock()
        self.con.executescript(SCHEMA)

    @db.locked
    def add(self, route: str, response: ModelResponse) -> None:
        """Record one request. Never raises: a failing log must not break the reply."""
        try:
            u, details = response.usage, response.provider_details or {}
            self.con.execute(
                "INSERT INTO usage (at, route, model, provider, tokens_in, cached_in, tokens_out, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.clock().isoformat(timespec="seconds"), route, response.model_name or "?",
                 details.get("downstream_provider"), u.input_tokens, u.cache_read_tokens, u.output_tokens,
                 details.get("cost")))
            self.con.commit()
        except Exception:
            log.exception("usage log: write failed (route=%s)", route)

    @db.locked
    def totals(self, since: datetime) -> list[tuple[str, str, int, int, int, float | None]]:
        """(route, model, requests, tokens in, tokens out, cost) since then, costliest first."""
        return self.con.execute(
            "SELECT route, model, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost_usd) FROM usage"
            " WHERE at >= ? GROUP BY route, model ORDER BY SUM(cost_usd) DESC NULLS LAST, SUM(tokens_in) DESC",
            (since.isoformat(timespec="seconds"),)).fetchall()


def report(usage: UsageLog, now: datetime) -> str:
    """The last 7 days and this month, per route and model."""
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for title, since in (("Last 7 days", now - timedelta(days=7)), (f"{now:%B}", month)):
        rows = usage.totals(since)
        if not rows:
            out.append(f"{title}: no cloud requests.")
            continue
        cost = sum(r[5] or 0 for r in rows)
        out.append(f"{title}: ${cost:.3f}" + (" (+ requests without a billed cost)" if any(r[5] is None for r in rows)
                                              else ""))
        for route, model, n, tokens_in, tokens_out, c in rows:
            if model.endswith(".gguf"):  # the cloud call failed and the local model answered
                model = f"{Path(model).stem} (local fallback)"
            billed = ("subscription" if model.startswith("claude") else "cost n/a") if c is None else f"${c:.3f}"
            out.append(f"  {route} · {model}: {n} req, {tokens_in / 1000:.0f}k in / {tokens_out / 1000:.1f}k out,"
                       f" {billed}")
    return "☁️ Cloud model usage\n" + "\n".join(out)


class Metered(WrapperModel):
    """The cloud model for one route: each response's tokens and cost go to the usage log."""

    def __init__(self, wrapped, route: str, usage: UsageLog):
        super().__init__(wrapped)
        self.route, self.usage = route, usage
        self.answered = ""  # the model that answered last: the cloud one, or the local one after a fallback

    async def request(self, messages, model_settings, model_request_parameters):
        response = await super().request(messages, model_settings, model_request_parameters)
        self.usage.add(self.route, response)
        self.answered = response.model_name or ""
        return response
