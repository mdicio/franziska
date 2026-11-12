"""Coding jobs (DESIGN §12 decisions 46–48, PROPOSALS part 3 C3): you ask for a change, a local agent makes it in a
sandbox, and you review a branch.

    /code franziska aggiungi un comando /uptime        (Telegram; or the web UI's Code view, or a 🛠 button)
    python -m franziska.code start scratch "uno script che rinomina le foto per data"
    python -m franziska.code list | show <id> | merge <id> | discard <id>

**A job, not a chat turn.** A job is a row in code.sqlite and a systemd user unit of its own
(`franziska-code-<id>`) that runs `python -m franziska.code run <id>`: it survives a restart of the bot (a job may be
changing the bot), runs one at a time (`runner.lock`), and the bot only announces what finished (`Jobs.finished`).
Only you start one: `/code`, the web form, or a tap on a button the pc assistant proposed in an untainted turn.

**Where the work happens.** An allowlisted repository (FRANZISKA_CODE_REPOS) is cloned (`--no-hardlinks`, without
a checkout: no host git ever touches a tree the job wrote) into `code_dir/jobs/<id>/repo`; 'scratch' is a new empty
repository. A clone and not a worktree: a worktree's git metadata lives in the original repository's .git, which
would have to be writable from the sandbox. The clone has no `.env` (it is not tracked) and no remote.

**Every command in the sandbox.** The agent has one tool, `bash` (mini-swe-agent's shape: each command a fresh
process, a linear history), and each command runs through `Sandbox.shell`: an empty bwrap root, no network, the
clone at /repo, the project's venv and its interpreter read-only at their own paths, a fresh HOME, cgroup limits.
The loop itself (this process) runs on the host and only talks to the local model, through the GPU lock per
request, so the bot's turns interleave with a job's steps.

**What comes back.** The sandbox commits whatever the agent left and writes a git bundle; the host fetches it into
the original repository as `franziska/job-<id>` (scratch: `code_dir/scratch.git`). Diff, stat and files are made
by host git from there, with no external diff or textconv, so nothing the job wrote into its own .git (a config, an
alias) decides what you are shown. The tests run in the sandbox. Merge, discard and 💬 change (a follow-up job on
the same branch) are your taps; nothing pushes, deploys or restarts by itself.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ThinkingPart, ToolCallPart, ToolReturnPart
from pydantic_ai.usage import UsageLimits

from franziska import db, llm
from franziska.config import ROOT, Settings
from franziska.sandbox import SYSTEMD_RUN, Limits, Sandbox, SandboxError

log = logging.getLogger(__name__)

SCRATCH = "scratch"
UNIT = "franziska-code-{}"
BRANCH = "franziska/job-{}"
OPEN = ("queued", "running")
REVIEW = ("ready",)  # waits for merge, change or discard
DONE = ("ready", "empty", "failed", "stopped")  # announced once
COMMAND_LIMITS = Limits(memory="4G", cpu_quota="400%", tasks=256, timeout_s=300, file_size="512M")
OUTPUT_CHARS = 8000  # of one command's transcript given to the model: its head and its tail
KEEP_OUTPUTS = 6  # the latest command outputs stay whole in the history; older ones shrink to OLD_CHARS
OLD_CHARS = 400
REPEATS = 3  # the same command this many times in a row: the agent is told to change approach
MAX_TOKENS: int | None = None  # one response: no cap, the context window is the limit (the request timeout stops a loop)
FILES_SENT = 4  # a scratch job with at most this many files sends them; more go as one zip
FILE_SENT_MAX = 2 * 1024 * 1024
DIFF_CHARS = 400_000  # of the diff kept for review
REQUEST_CHARS = 4000
PAUSED_FILE = "paused"  # core.PAUSED_FILE: /pause stops a job between two commands

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    repo        TEXT NOT NULL,          -- a FRANZISKA_CODE_REPOS name, or 'scratch'
    request     TEXT NOT NULL,
    status      TEXT NOT NULL,          -- queued running ready empty failed stopped merged discarded replaced
    origin      TEXT NOT NULL,          -- telegram | web | cli | button
    parent      INTEGER,                -- a follow-up (💬 change) continues this job's branch
    base        TEXT,                   -- the commit the diff is against
    base_branch TEXT,                   -- the original repository's branch at the start: merge goes there
    summary     TEXT NOT NULL DEFAULT '',
    tests       TEXT NOT NULL DEFAULT '',
    stat        TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    steps       INTEGER NOT NULL DEFAULT 0,
    created     TEXT NOT NULL,
    started     TEXT,
    finished    TEXT,
    announced   INTEGER NOT NULL DEFAULT 0
);
"""


class CodeError(RuntimeError):
    """A job can't be started, merged or discarded; the message is for the user."""


class Stop(Exception):
    """The job ends before the agent says it's done: out of time, paused, stopped. The work so far is kept."""


@dataclass
class Job:
    id: int
    repo: str
    request: str
    status: str
    origin: str
    parent: int | None
    base: str | None
    base_branch: str | None
    summary: str
    tests: str
    stat: str
    error: str
    steps: int
    created: str
    started: str | None
    finished: str | None
    announced: int

    @property
    def branch(self) -> str:
        return BRANCH.format(self.id)

    @property
    def title(self) -> str:
        first = self.request.strip().splitlines()[0] if self.request.strip() else ""
        return first[:70] + ("…" if len(first) > 70 else "")

    def line(self) -> str:
        icon = {"queued": "⏳", "running": "⚙️", "ready": "🔍", "empty": "∅", "failed": "❌", "stopped": "⏹",
                "merged": "✅", "discarded": "🗑", "replaced": "↪️"}.get(self.status, "·")
        return f"{icon} #{self.id} {self.repo} · {self.status} · {self.title}"


def _git(cwd: Path, *args: str, check: bool = True, timeout: float = 120) -> str:
    """Host git, on a repository whose config is ours or the user's (never on a tree a job wrote)."""
    out = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout,
                         env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and out.returncode:
        raise CodeError(f"git {args[0]} failed: {(out.stderr or out.stdout).strip()[:500]}")
    return out.stdout


class Jobs:
    """The job store and everything done on the host: start, announce, review, merge, discard."""

    def __init__(self, settings: Settings, now=None):
        self.settings = settings
        self.dir = settings.code_dir
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.now = now or (lambda: datetime.now(ZoneInfo(settings.timezone)))
        self.con = db.connect(self.dir / "code.sqlite")
        self.con.row_factory = sqlite3.Row
        self.lock = db.lock()  # the bot uses the store from its loop and from threads (a merge tap)
        self.con.executescript(SCHEMA)

    # ---- the store ---------------------------------------------------------------------------------------------
    def repos(self) -> dict[str, Path | None]:
        """Where a job may work: the allowlist, then scratch (None: a new empty repository)."""
        return {**self.settings.code_repos, SCRATCH: None}

    @db.locked
    def get(self, job_id: int) -> Job | None:
        row = self.con.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job(**dict(row)) if row else None

    @db.locked
    def recent(self, n: int = 10) -> list[Job]:
        return [Job(**dict(r)) for r in self.con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (n,))]

    @db.locked
    def _set(self, job_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.con:
            self.con.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def folder(self, job_id: int) -> Path:
        return self.dir / "jobs" / str(job_id)

    def target(self, job: Job) -> Path:
        """The repository the result is fetched into: the original one, or the host's scratch repository."""
        if job.repo == SCRATCH:
            scratch = self.dir / "scratch.git"
            if not scratch.exists():
                subprocess.run(["git", "init", "-q", "--bare", str(scratch)], check=True)
            return scratch
        path = self.settings.code_repos.get(job.repo)
        if path is None:
            raise CodeError(f"{job.repo} is no longer in FRANZISKA_CODE_REPOS.")
        return path

    # ---- start ---------------------------------------------------------------------------------------------------
    def start(self, repo: str, request: str, origin: str, parent: int | None = None, launch: bool = True) -> Job:
        request = request.strip()
        if not request:
            raise CodeError("Say what the job should do.")
        if len(request) > REQUEST_CHARS:
            raise CodeError(f"The request is over {REQUEST_CHARS} characters; shorten it.")
        if parent is not None:
            before = self.get(parent)
            if before is None or before.status not in REVIEW:
                raise CodeError(f"Job #{parent} has no branch to change (it is {before.status if before else 'gone'}).")
            repo = before.repo
        if repo not in self.repos():
            names = ", ".join(self.repos())
            raise CodeError(f"{repo!r} isn't a repository I may change. Allowed: {names} (FRANZISKA_CODE_REPOS).")
        if repo != SCRATCH and not (self.settings.code_repos[repo] / ".git").exists():
            raise CodeError(f"{self.settings.code_repos[repo]} is not a git repository.")
        problem = Sandbox(self.settings.sandbox_venv_dir, self.settings.sandbox_jobs_dir).problem()
        if problem:
            raise CodeError(f"The sandbox isn't ready, so no code runs: {problem}")
        with self.lock, self.con:
            cur = self.con.execute("INSERT INTO jobs (repo, request, status, origin, parent, created) "
                                   "VALUES (?, ?, 'queued', ?, ?, ?)",
                                   (repo, request, origin, parent, self.now().isoformat(timespec="seconds")))
        job = self.get(cur.lastrowid)
        if launch:
            self._launch(job.id)
        log.info("code job #%d queued on %s (%s)", job.id, repo, origin)
        return job

    def _launch(self, job_id: int) -> None:
        """The runner as a transient user unit: outlives the bot, killed at the wall clock. It gets Franziska's
        settings from this process's environment, minus anything that looks like a credential: it needs none."""
        env = [f"--setenv={k}={v}" for k, v in os.environ.items()
               if k.startswith("FRANZISKA_") and not re.search(r"TOKEN|KEY|SECRET|PASS", k)]
        seconds = (self.settings.code_minutes + 10) * 60  # the loop stops itself at code_minutes; this is the backstop
        cmd = [SYSTEMD_RUN, "--user", "--quiet", "--collect", f"--unit={UNIT.format(job_id)}",
               f"--working-directory={ROOT}", "-p", f"RuntimeMaxSec={seconds}", *env,
               sys.executable, "-m", "franziska.code", "run", str(job_id)]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode:
            self._set(job_id, status="failed", error=f"could not start: {out.stderr.strip()[:300]}",
                      finished=self.now().isoformat(timespec="seconds"))
            raise CodeError(f"Could not start job #{job_id}: {out.stderr.strip()[:300]}")

    def stop(self, job_id: int) -> str:
        job = self.get(job_id)
        if job is None or job.status not in OPEN:
            return f"Job #{job_id} isn't running."
        subprocess.run(["systemctl", "--user", "stop", UNIT.format(job_id)], capture_output=True, timeout=60)
        job = self.get(job_id)
        if job.status in OPEN:  # killed before it could record why
            self._set(job_id, status="stopped", error="stopped by you", announced=1,
                      finished=self.now().isoformat(timespec="seconds"))
            shutil.rmtree(self.folder(job_id) / "repo", ignore_errors=True)
        return f"⏹ Job #{job_id} stopped."

    # ---- the bot's tick ----------------------------------------------------------------------------------------
    def reap(self) -> None:
        """A job whose unit is gone but which never recorded an end (killed at the backstop, the machine rebooted)."""
        for job in [j for j in self.recent(20) if j.status in OPEN]:
            state = subprocess.run(["systemctl", "--user", "is-active", UNIT.format(job.id)], capture_output=True,
                                   text=True, timeout=10).stdout.strip()
            if state in ("active", "activating", "reloading"):
                continue
            fresh = self.get(job.id)
            if fresh and fresh.status in OPEN:
                self._set(job.id, status="failed", error=f"the runner ended without a result ({state or 'gone'})",
                          finished=self.now().isoformat(timespec="seconds"))
                shutil.rmtree(self.folder(job.id) / "repo", ignore_errors=True)

    def finished(self) -> list[Job]:
        """Jobs that ended and haven't been announced; `announced` marks them."""
        self.reap()
        with self.lock:
            rows = self.con.execute(
                "SELECT * FROM jobs WHERE announced = 0 AND status IN ('ready', 'empty', 'failed', 'stopped') "
                "ORDER BY id").fetchall()
        return [Job(**dict(r)) for r in rows]

    def announced(self, job_id: int) -> None:
        self._set(job_id, announced=1)

    def report(self, job: Job) -> str:
        """The job's result in a few lines, for Telegram and the CLI."""
        head = {"ready": f"🛠 Job #{job.id} on {job.repo} is ready for review",
                "empty": f"🛠 Job #{job.id} on {job.repo} changed nothing",
                "failed": f"❌ Job #{job.id} on {job.repo} failed",
                "stopped": f"⏹ Job #{job.id} on {job.repo} stopped"}.get(job.status, job.line())
        parts = [f"{head} ({job.steps} steps).", f"Asked: {job.title}"]
        if job.summary:
            parts.append(job.summary[:1500])
        if job.error:
            parts.append(f"⚠️ {job.error}")
        if job.stat:
            parts.append(job.stat.strip()[-800:])
        if job.tests:
            parts.append(f"Tests: {job.tests.strip().splitlines()[-1][:200]}")
        if job.status == "ready" and job.repo != SCRATCH:
            parts.append(f"Branch {job.branch} in {self.target(job)} (based on {job.base_branch or 'a detached HEAD'}).")
        return "\n\n".join(parts)

    # ---- review ------------------------------------------------------------------------------------------------
    def diff(self, job_id: int) -> str:
        try:
            return (self.folder(job_id) / "diff.patch").read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def files(self, job: Job) -> list[Path]:
        """What a scratch job made: its files when few and small, else one zip."""
        out = self.folder(job.id) / "files"
        return sorted(p for p in out.iterdir() if p.is_file()) if out.is_dir() else []

    def merge(self, job_id: int) -> str:
        """Merge the job's branch into the branch it started from, in the original repository, if that is checked out
        and has no uncommitted changes to tracked files. A conflict leaves everything as it was."""
        job = self.get(job_id)
        if job is None or job.status not in REVIEW:
            return f"Job #{job_id} has nothing to merge ({job.status if job else 'no such job'})."
        if job.repo == SCRATCH:
            return "A scratch job has no repository to merge into: its files came with the report."
        repo = self.target(job)
        current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip()
        if not job.base_branch or current != job.base_branch:
            return (f"Not merged: {repo.name} is on {current or 'a detached HEAD'}, the job started from "
                    f"{job.base_branch or 'a detached HEAD'}. Check that branch out, then tap again.")
        if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
            return f"Not merged: {repo.name} has uncommitted changes. Commit or stash them, then tap again."
        out = subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "--no-edit", "-m",
                              f"Merge Franziska job #{job.id}: {job.title}", job.branch],
                             capture_output=True, text=True, timeout=120)
        if out.returncode:
            subprocess.run(["git", "-C", str(repo), "merge", "--abort"], capture_output=True, timeout=60)
            return f"Not merged, it conflicts with {job.base_branch}; nothing changed.\n{out.stdout.strip()[-600:]}"
        _git(repo, "branch", "-d", job.branch, check=False)
        self._set(job_id, status="merged")
        shutil.rmtree(self.folder(job_id) / "repo", ignore_errors=True)
        note = (" The running bot and web UI still run the old code until you restart them."
                if repo.resolve() == ROOT.resolve() else "")
        return f"✅ Job #{job.id} merged into {job.base_branch}.{note}"

    def discard(self, job_id: int) -> str:
        job = self.get(job_id)
        if job is None:
            return f"There is no job #{job_id}."
        if job.status in OPEN:
            self.stop(job_id)
        if job.status in ("merged", "discarded"):
            return f"Job #{job_id} is already {job.status}."
        if job.base:
            _git(self.target(job), "branch", "-D", job.branch, check=False)
        self._set(job_id, status="discarded", announced=1)
        shutil.rmtree(self.folder(job_id), ignore_errors=True)
        return f"🗑 Job #{job_id} discarded."

    def is_self(self, job: Job) -> bool:
        """The job changed Franziska itself: merging it wants a restart to take effect."""
        path = self.settings.code_repos.get(job.repo)
        return path is not None and path.resolve() == ROOT.resolve()


# ---- the runner (python -m franziska.code run <id>, in its own unit) -------------------------------------------------
@dataclass
class Workspace:
    """One job's sandbox: the clone at /repo, /out for the bundle, the toolchain read-only. The agent's deps."""

    sandbox: Sandbox
    repo: Path
    out: Path
    read_only: dict[str, Path]
    env: dict[str, str]
    deadline: float
    paused: Path
    tests: str = ""  # the command that runs the repository's tests, if it has some
    steps: int = 0
    last: tuple[str, int] = ("", 0)  # the previous command and how many times in a row

    def run(self, command: str, limits: Limits = COMMAND_LIMITS) -> tuple[int, str]:
        result = self.sandbox.shell(command, {"/repo": self.repo, "/out": self.out}, self.read_only, self.env,
                                    limits)
        return result.returncode, result.stdout

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            raise Stop("out of time")
        if self.paused.exists():
            raise Stop("paused (/resume, then 💬 change to continue)")


def toolchain(repo: Path | None, settings: Settings) -> tuple[dict[str, Path], dict[str, str], str]:
    """The read-only mounts, environment and test command for a job. A repository with its own `.venv` gets it at its
    own path (its scripts' shebangs name it) plus the interpreter it was made from; scratch gets the sandbox venv
    (pandas, matplotlib, openpyxl, pypdf). No package is ever installed during a job: there is no network."""
    venv = repo / ".venv" if repo else None
    if venv and (venv / "pyvenv.cfg").exists():
        mounts = {str(venv): venv}
        home = next((line.split("=", 1)[1].strip() for line in (venv / "pyvenv.cfg").read_text().splitlines()
                     if line.split("=", 1)[0].strip() == "home"), "")
        if home and not home.startswith("/usr/"):  # e.g. ~/.pyenv/versions/3.12.0/bin: mount the installation
            prefix = Path(home).parent
            mounts[str(prefix)] = prefix
        env = {"PATH": f"{venv}/bin:/usr/bin", "VIRTUAL_ENV": str(venv)}
        has_tests = (repo / "tests").is_dir() or any(repo.glob("test_*.py"))
        tests = "python -m pytest -q -x 2>&1 | tail -n 25" if has_tests and (venv / "bin" / "pytest").exists() else ""
        return mounts, env, tests
    sandbox_venv = settings.sandbox_venv_dir.expanduser().resolve()
    return {"/venv": sandbox_venv}, {"PATH": "/venv/bin:/usr/bin"}, ""


INSTRUCTIONS = """\
You are a careful software engineer. You work alone, in a git repository at /repo, inside an isolated sandbox with
no network: you can't install packages, push, or reach any website. Your only tool is `bash`.

How the tool works:
- Each call runs one command in a fresh `bash -c` starting in /repo; `cd`, variables and background processes don't
  carry over. Chain steps with `&&`. Nothing is interactive: no editors, no pagers, no prompts.
- Look before you change: `rg -n`, `ls`, `sed -n '1,120p' file`, `git log --oneline -5`.
- Edit with `cat > path <<'EOF' … EOF` for a new or short file, or a small `python3 - <<'EOF'` script that reads
  the file, replaces an exact snippet (and checks it was found) and writes it back. Re-read the lines you changed.
- Output over {limit} characters is cut in the middle: ask for less (`| head`, `sed -n`).

How to work:
- Do exactly what was asked, in the style of the code around it; the smallest change that does it. No unrelated
  refactors, no new dependencies.
- Check your work: run the code you wrote, or the tests that cover it.{tests}
- Don't commit, branch or reset; that is done for you when you finish.
- When done, or if it can't be done, stop calling tools and answer with a short report in the language of the
  request: what you changed (files), how you checked it, and what is left or uncertain. Never claim a check you
  didn't run."""


def _short(text: str, n: int = OUTPUT_CHARS) -> str:
    if len(text) <= n:
        return text
    head, tail = n // 3, n - n // 3
    return f"{text[:head]}\n[… {len(text) - n} characters cut …]\n{text[-tail:]}"


def _trim(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Keep the window: older command outputs and file contents written in old calls shrink, old reasoning goes. The
    latest KEEP_OUTPUTS steps stay whole (mini-swe-agent keeps everything; a 32k window can't)."""
    returns = [(i, j) for i, m in enumerate(messages) if isinstance(m, ModelRequest)
               for j, p in enumerate(m.parts) if isinstance(p, ToolReturnPart)]
    old = set(returns[:-KEEP_OUTPUTS])
    cutoff = returns[-KEEP_OUTPUTS][0] if len(returns) >= KEEP_OUTPUTS else 0
    out: list[ModelMessage] = []
    for i, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any((i, j) in old for j in range(len(m.parts))):
            m = dataclasses.replace(m, parts=[
                dataclasses.replace(p, content=_short(str(p.content), OLD_CHARS)) if (i, j) in old else p
                for j, p in enumerate(m.parts)])
        elif isinstance(m, ModelResponse) and i < cutoff:
            parts = []
            for p in m.parts:
                if isinstance(p, ThinkingPart):
                    continue
                if isinstance(p, ToolCallPart):
                    args = p.args_as_dict()
                    if len(str(args.get("command", ""))) > OLD_CHARS:
                        p = dataclasses.replace(p, args={"command": _short(str(args["command"]), OLD_CHARS)})
                parts.append(p)
            m = dataclasses.replace(m, parts=parts or m.parts)
        out.append(m)
    return out


agent = Agent(output_type=str, deps_type=Workspace, name="code", retries=2)


@agent.tool
def bash(ctx: RunContext[Workspace], command: str) -> str:
    """Run one bash command in /repo (no network, nothing interactive). Returns the exit code and the output.

    Args:
        command: The command line, e.g. `rg -n "def route" franziska/ | head -20`.
    """
    ws = ctx.deps
    ws.check()
    ws.last = (command, ws.last[1] + 1) if command == ws.last[0] else (command, 1)
    if ws.last[1] >= REPEATS:
        return (f"You ran this exact command {ws.last[1]} times in a row and got the same answer. Try something "
                "different, or finish with your report.")
    ws.steps += 1
    try:
        code, output = ws.run(command)
    except SandboxError as e:
        return f"Not run: {e}"
    if code and not output.strip():
        output = (f"(no output: killed for going over {COMMAND_LIMITS.memory} of memory or "
                  f"{COMMAND_LIMITS.timeout_s} s?)")
    return f"exit {code}\n{_short(output)}"


def _model(settings: Settings):
    """The local model, with the job's own thinking setting; each request takes the GPU lock (gpu.Local). On Claude
    when FRANZISKA_CLAUDE_ROUTES lists 'code' (the local model answers a failed step)."""
    local = llm.build_model(dataclasses.replace(settings, thinking=settings.code_thinking, timeout_s=300.0))
    return llm.Models(settings, local=local).for_job("code")


def run(settings: Settings, job_id: int, model=None) -> Job:
    """The whole job, in this process. One at a time: the next waits here for runner.lock."""
    jobs = Jobs(settings)
    settings.code_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(settings.code_dir / "runner.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        job = jobs.get(job_id)
        if job is None or job.status != "queued":
            raise CodeError(f"job #{job_id} is not queued")
        jobs._set(job_id, status="running", started=jobs.now().isoformat(timespec="seconds"))
        folder = jobs.folder(job_id)
        try:
            _work(settings, jobs, job, folder, model or _model(settings))
        except Exception as e:
            log.exception("code job #%d failed", job_id)
            jobs._set(job_id, status="failed", error=f"{type(e).__name__}: {e}"[:1000],
                      finished=jobs.now().isoformat(timespec="seconds"))
        finally:
            shutil.rmtree(folder / "repo", ignore_errors=True)
            shutil.rmtree(folder / "out", ignore_errors=True)
    return jobs.get(job_id)


def _prepare(settings: Settings, jobs: Jobs, job: Job, folder: Path, ws_for) -> tuple[Workspace, str, str | None]:
    """The clone (or the scratch repository) and the job's base commit; the checkout happens in the sandbox."""
    repo = folder / "repo"
    shutil.rmtree(folder, ignore_errors=True)
    (folder / "out").mkdir(parents=True, mode=0o700)
    source = settings.code_repos.get(job.repo)
    parent = jobs.get(job.parent) if job.parent else None
    if source is None:  # scratch
        repo.mkdir(mode=0o700)
        ws = ws_for(repo, None)
        if parent:  # continue the earlier scratch job: its bundle, from the host's scratch repository
            bundle = folder / "out" / "parent.bundle"
            _git(jobs.target(parent), "bundle", "create", "-q", str(bundle), parent.branch)
            setup = (f"git clone -q -b {parent.branch} /out/parent.bundle . && git checkout -q -b job "
                     "&& rm /out/parent.bundle")
            base = parent.base
        else:
            setup = "git init -q -b job . && git commit -q --allow-empty -m 'Empty start'"
            base = None
        base_branch = None
    else:
        ref = parent.branch if parent else "HEAD"
        base = parent.base if parent else _git(source, "rev-parse", "HEAD").strip()
        base_branch = (parent.base_branch if parent
                       else _git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).strip() or None)
        start = _git(source, "rev-parse", ref).strip()
        _git(source.parent, "clone", "-q", "--no-hardlinks", "--no-checkout", str(source), str(repo), timeout=600)
        _git(repo, "remote", "remove", "origin")
        ws = ws_for(repo, source)
        setup = f"git checkout -q -b job {start}"
    code, out = ws.run(f"{setup} && git config commit.gpgsign false")
    if code:
        raise CodeError(f"setting up the workspace failed: {out.strip()[-500:]}")
    if base is None:
        code, out = ws.run("git rev-parse HEAD")
        base = out.strip().splitlines()[-1]
    return ws, base, base_branch


def _work(settings: Settings, jobs: Jobs, job: Job, folder: Path, model) -> None:
    sandbox = Sandbox(settings.sandbox_venv_dir, settings.sandbox_jobs_dir)
    deadline = time.monotonic() + settings.code_minutes * 60

    def ws_for(repo: Path, source: Path | None) -> Workspace:
        mounts, env, tests = toolchain(source, settings)
        who = f"Franziska job {job.id}"
        env = {**env, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": who, "GIT_COMMITTER_NAME": who,
               "GIT_AUTHOR_EMAIL": "franziska@localhost", "GIT_COMMITTER_EMAIL": "franziska@localhost"}
        return Workspace(sandbox, repo, folder / "out", mounts, env, deadline, settings.data_dir / PAUSED_FILE,
                         tests)

    ws, base, base_branch = _prepare(settings, jobs, job, folder, ws_for)
    jobs._set(job.id, base=base, base_branch=base_branch)
    parent = jobs.get(job.parent) if job.parent else None
    tests = (f"\n- The repository's tests: `{ws.tests.split(' 2>&1')[0]}` (they run again when you finish)."
             if ws.tests else "")
    instructions = INSTRUCTIONS.format(limit=OUTPUT_CHARS, tests=tests)
    if parent:
        prompt = (f"Earlier in this repository you were asked: {parent.request}\nYour report then: "
                  f"{parent.summary[:1500]}\nThose changes are committed. Now the user asks for this change:\n"
                  f"{job.request}")
    else:
        what = ("an empty repository: put what you make in it (scripts, a README saying how to run them)"
                if job.repo == SCRATCH else f"the repository '{job.repo}'")
        prompt = f"You are in {what}.\n\nThe request:\n{job.request}"
    error = ""
    try:
        result = agent.run_sync(prompt, model=model, deps=ws, instructions=instructions,
                                usage_limits=UsageLimits(request_limit=settings.code_steps + 2),
                                model_settings={"max_tokens": MAX_TOKENS} if MAX_TOKENS else None,
                                capabilities=[ProcessHistory(_trim)])
        summary = result.output.strip()
    except Stop as e:
        summary, error = "", f"Stopped before finishing: {e}. What it did so far is on the branch."
    except UsageLimitExceeded:
        summary, error = "", f"Stopped at the step limit ({settings.code_steps}); what it did so far is on the branch."
    _finish(jobs, job, ws, base, summary, error)


def _finish(jobs: Jobs, job: Job, ws: Workspace, base: str, summary: str, error: str) -> None:
    """Commit what the agent left, run the tests, bundle, fetch into the target and make the review from there."""
    finished = lambda **f: jobs._set(job.id, summary=summary, steps=ws.steps,  # noqa: E731
                                     finished=jobs.now().isoformat(timespec="seconds"), **f)
    (ws.out / "message.txt").write_text(f"{job.title}\n\nFranziska coding job #{job.id}:\n{job.request}\n")
    code, out = ws.run("git add -A && (git diff --cached --quiet || git commit -q --no-verify -F /out/message.txt)")
    if code:
        finished(status="failed", error=(error + " " if error else "") + f"commit failed: {out.strip()[-400:]}")
        return
    tests = ""
    if ws.tests:
        _, tests = ws.run(ws.tests)
        tests = tests.strip()[-3000:]
    full = job.repo == SCRATCH
    code, out = ws.run(f"git bundle create -q /out/result.bundle {'job' if full else base + '..job'} 2>&1")
    target = jobs.target(job)
    if code:
        if "empty bundle" in out.lower() or "refusing to create" in out.lower():
            finished(status="empty", tests=tests, error=error)
            return
        finished(status="failed", tests=tests, error=f"bundle failed: {out.strip()[-400:]}")
        return
    _git(target, "-c", "fetch.fsckObjects=true", "fetch", "-q", "--no-tags", "--no-write-fetch-head",
         str(ws.out / "result.bundle"), f"+job:refs/heads/{job.branch}")
    review = ("-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-textconv", "--no-color")
    if int(_git(target, "rev-list", "--count", f"{base}..{job.branch}").strip() or 0) == 0:
        _git(target, "branch", "-D", job.branch, check=False)
        finished(status="empty", tests=tests, error=error)
        return
    stat = _git(target, *review, "--stat=100", base, job.branch)
    diff = _git(target, *review, base, job.branch)
    folder = jobs.folder(job.id)
    (folder / "diff.patch").write_text(diff[:DIFF_CHARS] + ("\n[diff cut]\n" if len(diff) > DIFF_CHARS else ""))
    if full:
        _scratch_files(target, job, folder)
    if job.parent:
        jobs._set(job.parent, status="replaced", announced=1)
        _git(target, "branch", "-D", BRANCH.format(job.parent), check=False)
    finished(status="ready", tests=tests, stat=stat, error=error)


def _scratch_files(target: Path, job: Job, folder: Path) -> None:
    """A scratch job's files, read from the host's scratch repository: each one when few and small, else a zip."""
    out = folder / "files"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    names = [n for n in _git(target, "ls-tree", "-r", "--name-only", "-z", job.branch).split("\0") if n]
    sizes = {n: int(_git(target, "cat-file", "-s", f"{job.branch}:{n}").strip()) for n in names[:FILES_SENT + 1]}
    if 0 < len(names) <= FILES_SENT and all(s <= FILE_SENT_MAX for s in sizes.values()):
        used: set[str] = set()
        for name in names:
            base = re.sub(r"[^\w.\-]", "_", Path(name).name) or "file"
            while base in used:
                base = "_" + base
            used.add(base)
            data = subprocess.run(["git", "-C", str(target), "cat-file", "blob", f"{job.branch}:{name}"],
                                  capture_output=True, check=True, timeout=60).stdout
            (out / base).write_bytes(data)
    elif names:
        _git(target, "archive", "--format=zip", "-o", str(out / f"job-{job.id}.zip"), job.branch)
        with zipfile.ZipFile(out / f"job-{job.id}.zip") as z:  # a sanity check of what we made, not of the job
            z.testzip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Franziska's coding jobs (run in the sandbox, reviewed by you).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="queue a job and start its runner unit")
    s.add_argument("repo")
    s.add_argument("request")
    s.add_argument("--wait", action="store_true", help="run it in this process instead of a unit, and print it")
    sub.add_parser("list")
    for name in ("show", "merge", "discard", "stop", "run", "diff"):
        sub.add_parser(name).add_argument("id", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    jobs = Jobs(settings)
    if args.cmd == "start":
        job = jobs.start(args.repo, args.request, "cli", launch=not args.wait)
        if args.wait:
            job = run(settings, job.id)
            jobs.announced(job.id)
            print(jobs.report(job))
        else:
            print(f"Job #{job.id} started: journalctl --user -u {UNIT.format(job.id)} -f")
    elif args.cmd == "run":
        job = run(settings, args.id)
        print(job.line())
    elif args.cmd == "list":
        for job in jobs.recent(20):
            print(job.line())
    elif args.cmd == "show":
        job = jobs.get(args.id)
        print(jobs.report(job) if job else f"no job #{args.id}")
    elif args.cmd == "diff":
        print(jobs.diff(args.id))
    else:
        print(getattr(jobs, args.cmd)(args.id))


if __name__ == "__main__":
    main()
