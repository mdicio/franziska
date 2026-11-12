"""Logs and traces: what happened in a turn, a workflow, a job or a tap, end to end (COMMANDS.md, "Logs and traces").

Two places, one id:
- **the journal** (`logging`): the story and the failures, a couple of lines per unit of work. Every line written
  inside one carries `[<run id> <area>]`, e.g. `[3f2a91c0 life]`, so `journalctl … | grep 3f2a91c0` is that run.
- **traces.sqlite** (OpenTelemetry spans): everything inside the run. pydantic-ai's own instrumentation gives the agent
  run, each model request (messages in and out, reasoning, tokens, finish reason) and each tool call (args, result);
  our spans add what it can't see: the root (what started it), the router's decision, the GPU lock wait, the guards'
  rewrites and stops, event handlers. The run id is the trace id's first 8 hex digits.

The traces hold message content (money, mail), so they stay in the data dir like the turn log, are never opened by
the MCP server or the cloud path, are not backed up, and go after KEEP_DAYS. FRANZISKA_OTLP sends the same spans to an
OTLP endpoint as well, e.g. a local Phoenix (`uvx arize-phoenix serve`, http://localhost:6006/v1/traces) to browse them.

Without `setup` (tests, evals, the MCP server) every helper here is a no-op.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from franziska import db

log = logging.getLogger(__name__)

KEEP_DAYS = 30
FORMAT = "%(asctime)s %(levelname)s %(run)s%(name)s: %(message)s"
# Chatty at INFO and never what a bug is about: apscheduler writes two lines a minute per repeating job.
QUIET_LOGGERS = ("apscheduler", "httpx", "httpx2", "httpcore")  # httpx's INFO lines also carry the bot token in URLs

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    span_id  TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent   TEXT,              -- NULL: the root, what started the run
    service  TEXT NOT NULL,     -- franziska-bot | franziska-web | cli
    name     TEXT NOT NULL,     -- 'message', 'turn life', 'chat gemma-4…', 'execute_tool agenda', 'job nightly'
    start    TEXT NOT NULL,     -- local time, milliseconds
    ms       REAL NOT NULL,
    error    TEXT,              -- the status description when the span failed
    attrs    TEXT NOT NULL,     -- JSON
    events   TEXT               -- JSON [{name, at, attrs}]: guard rewrites and stops, exceptions
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS spans_start ON spans(start);
"""

# Left out of traces.sqlite (an OTLP viewer still gets them): the same on every request, and most of a run's bytes.
# Tool schemas are in the code; the instructions are on the run's agent span once.
SKIP_ATTRS = frozenset({"gen_ai.tool.definitions", "logfire.json_schema"})
REQUEST_SKIP = frozenset({"gen_ai.system_instructions"})  # on 'chat <model>' spans only

_tracer = trace.get_tracer("franziska")
_area: contextvars.ContextVar[str] = contextvars.ContextVar("area", default="")


def setup(service: str, traces_db: Path, otlp: str = "", level: int = logging.INFO) -> None:
    """Logging for this process and the tracer behind every span: call once, first thing in a main()."""
    logging.basicConfig(level=level, format=FORMAT)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_RunFilter())
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Phoenix files traces by project, from this attribute (not service.name): one project per service.
    provider = TracerProvider(resource=Resource.create({"service.name": service,
                                                        "openinference.project.name": service}))
    provider.add_span_processor(_SkipIdle(BatchSpanProcessor(SqliteExporter(traces_db))))
    if otlp:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(_SkipIdle(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp))))
    trace.set_tracer_provider(provider)
    from pydantic_ai import Agent, InstrumentationSettings

    # The request parameters repeat every tool's schema on every request (see also SKIP_ATTRS).
    Agent.instrument_all(InstrumentationSettings(include_model_request_parameters=False))


# ---- spans ----------------------------------------------------------------------------------------
@contextlib.contextmanager
def span(name: str, area: str | None = None, *, _root: bool = False, **attrs) -> Iterator[trace.Span]:
    """A span around a step. `area` (a route, a workflow, a job) tags the log lines written inside it."""
    token = _area.set(area) if area else None
    try:
        with _tracer.start_as_current_span(name, context=Context() if _root else None,
                                           attributes=clean(**attrs)) as s:
            yield s
    finally:
        if token:
            _area.reset(token)


@contextlib.contextmanager
def root(name: str, area: str | None = None, quiet: bool = False, **attrs) -> Iterator[trace.Span]:
    """A new run, never a child of whatever span is current: what started it (a message, a tap, a job). `quiet`: a
    periodic tick, dropped when it did nothing
    (no child span, no event, no error), so a reminder check every minute leaves no trace unless it sent one."""
    with span(name, area or name, _root=True, **attrs, **({"quiet": True} if quiet else {})) as s:
        yield s


def job(name: str, callback, quiet: bool = False):
    """A PTB job callback as a run of its own."""
    @functools.wraps(callback)
    async def run(context):
        started = time.monotonic()
        with root(f"job {name}", name, quiet=quiet):
            out = await callback(context)
            if not quiet:
                log.info("job %s done in %.1f s", name, time.monotonic() - started)
            return out
    return run


def event(name: str, **attrs) -> None:
    """Something worth seeing on the current span: a guard's rewrite, a reminder sent."""
    trace.get_current_span().add_event(name, clean(**attrs))


def attr(**attrs) -> None:
    trace.get_current_span().set_attributes(clean(**attrs))


def failed(error: BaseException | str) -> None:
    """The current span failed, though the exception was caught (a job that logs and carries on)."""
    s = trace.get_current_span()
    if isinstance(error, BaseException):
        s.record_exception(error)
    s.set_status(Status(StatusCode.ERROR, str(error)[:500]))


def run_id() -> str:
    """The current run's id ('3f2a91c0'), or '' outside one."""
    ctx = trace.get_current_span().get_span_context()
    return f"{ctx.trace_id:032x}"[:8] if ctx.trace_id else ""


def clean(**attrs) -> dict:
    """OTel takes str, bool, int, float and lists of one of them; None is left out, anything else is JSON."""
    out = {}
    for k, v in attrs.items():
        if v is None:
            continue
        if isinstance(v, (str, bool, int, float)) or (isinstance(v, Sequence) and not isinstance(v, (str, bytes))
                                                       and all(isinstance(x, str) for x in v)):
            out[k] = v
        else:
            out[k] = json.dumps(v, ensure_ascii=False, default=str)
    return out


class _RunFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rid = run_id()
        record.run = f"[{rid} {_area.get()}] " if rid else ""
        return True


# ---- storage --------------------------------------------------------------------------------------
class _SkipIdle(SpanProcessor):
    """Passes spans on, except the root of a quiet run that did nothing (see `root`)."""

    def __init__(self, inner: SpanProcessor):
        self.inner, self.children, self.lock = inner, Counter(), threading.Lock()

    def on_start(self, s, parent_context=None) -> None:
        if s.parent is not None:
            with self.lock:
                self.children[s.context.trace_id] += 1
        self.inner.on_start(s, parent_context)

    def on_end(self, s: ReadableSpan) -> None:
        if s.parent is None:
            with self.lock:
                children = self.children.pop(s.context.trace_id, 0)
            if s.attributes.get("quiet") and not children and not s.events and s.status.is_ok:
                return
        self.inner.on_end(s)

    def shutdown(self) -> None:
        self.inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.inner.force_flush(timeout_millis)


class SqliteExporter(SpanExporter):
    """Spans as rows of traces.sqlite. Called from the batch processor's one thread."""

    def __init__(self, path: Path):
        self.path = path
        self.con = None

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            if self.con is None:
                self.con = db.connect(self.path)
                self.con.executescript(SCHEMA)
            self.con.executemany("INSERT OR REPLACE INTO spans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 [_row(s) for s in spans])
            self.con.commit()
            return SpanExportResult.SUCCESS
        except Exception:
            log.exception("traces: write failed (%d spans)", len(spans))
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        if self.con is not None:
            self.con.close()


def _row(s: ReadableSpan) -> tuple:
    ctx = s.get_span_context()
    skip = SKIP_ATTRS | (REQUEST_SKIP if (s.attributes or {}).get("gen_ai.operation.name") == "chat" else frozenset())
    attrs = {k: v for k, v in (s.attributes or {}).items() if k not in skip}
    events = [{"name": e.name, "at": _local(e.timestamp), "attrs": dict(e.attributes or {})} for e in s.events]
    return (f"{ctx.span_id:016x}", f"{ctx.trace_id:032x}", f"{s.parent.span_id:016x}" if s.parent else None,
            str(s.resource.attributes.get("service.name", "?")), s.name, _local(s.start_time),
            ((s.end_time or s.start_time) - s.start_time) / 1e6,
            (s.status.description or "error") if s.status.status_code is StatusCode.ERROR else None,
            json.dumps(attrs, ensure_ascii=False, default=str),
            json.dumps(events, ensure_ascii=False, default=str) if events else None)


def _local(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9).isoformat(timespec="milliseconds")


def expire(path: Path, now: datetime) -> int:
    """Delete spans older than KEEP_DAYS (the nightly job); returns how many."""
    if not path.exists():
        return 0
    con = sqlite3.connect(path, timeout=10)
    try:
        n = con.execute("DELETE FROM spans WHERE start < ?",
                        ((now - timedelta(days=KEEP_DAYS)).isoformat(timespec="milliseconds"),)).rowcount
        con.commit()
        return n
    finally:
        con.close()
