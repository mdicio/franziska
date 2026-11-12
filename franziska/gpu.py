"""One local model request at a time on the GPU, across every process on this machine (DESIGN decision 17).

The 3060 has 12 GB: two local jobs at once (the bot's agent and a mail eval's llama-server, or the router and an
agent on two runtimes) either fail to load (`llama_init_from_model`, seen 2026-10-28 00:01) or spill layers to RAM.
So every request to a local model takes this lock: the pydantic-ai models through `Local` (llm.py), AnyJev
decisions in `jev_ollama`. Cloud models never take it and run in parallel.

Two ways to hold it:
- `use(owner)` around one request: waits up to WAIT_S for the request in front, then raises `Busy`.
- `hold(owner)` for a long job that owns the GPU (an eval run, a model sweep): requests from other processes fail
  at once with `Busy` naming the job, instead of waiting for hours; requests in this process pass through.

What it doesn't do: unload a model another runtime keeps resident. A llama-server left running holds its VRAM
whether or not it has the lock, so a second runtime is started per job and stopped after (as the evals do).
"""

import asyncio
import fcntl
import os
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic_ai.models.wrapper import WrapperModel

from franziska import obs

LOCK = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "franziska-gpu.lock"
WAIT_S = 180.0  # longer than one agent request (timeout_s 120) plus a router decision
SESSION = "session:"  # prefix of a hold()'s owner line in the lock file

_in_process = threading.Lock()  # flock is per open file, so threads of one process queue here first
_session: str | None = None  # this process holds the GPU for a long job


class Busy(RuntimeError):
    """The local GPU is taken by another job; the message says by whom."""


def holder() -> str:
    try:
        return LOCK.read_text().strip()
    except OSError:
        return ""


@contextmanager
def _locked(owner: str, wait_s: float) -> Iterator[None]:
    asked = time.monotonic()
    deadline = asked + wait_s
    if not _in_process.acquire(timeout=wait_s):
        raise Busy(f"the local model is busy in this process ({owner} waited {wait_s:.0f} s)")
    try:
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    other = holder()
                    if other.startswith(SESSION) or time.monotonic() > deadline:
                        raise Busy(f"the local model is busy: {other.removeprefix(SESSION) or 'another process'}")
                    time.sleep(0.2)
            obs.attr(gpu_wait_ms=round((time.monotonic() - asked) * 1000))
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{owner} (pid {os.getpid()}, since {datetime.now():%H:%M})".encode(), 0)
            try:
                yield
            finally:
                os.ftruncate(fd, 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        _in_process.release()


@contextmanager
def use(owner: str, wait_s: float = WAIT_S) -> Iterator[None]:
    """Around one local model request."""
    if _session:
        yield
        return
    with _locked(owner, wait_s):
        yield


@contextmanager
def hold(owner: str, wait_s: float = WAIT_S) -> Iterator[None]:
    """Own the GPU for a whole job (evals, sweeps); other processes' requests fail fast with Busy."""
    global _session
    with _locked(SESSION + owner, wait_s):
        _session = owner
        try:
            yield
        finally:
            _session = None


class Local(WrapperModel):
    """A pydantic-ai model on this machine's GPU: each request takes the lock (in a thread, so the loop runs on)."""

    async def request(self, messages, model_settings, model_request_parameters):
        lock = use(f"agent {self.model_name}")
        await asyncio.to_thread(lock.__enter__)
        try:
            return await super().request(messages, model_settings, model_request_parameters)
        finally:
            lock.__exit__(None, None, None)
