"""The no-network execution boundary for code written during a Franziska turn.

This is deliberately *not* a general host command runner.  A caller gives it a
Python program and copied inputs; the program sees only a read-only ``/in`` and
a writable ``/work``.  Bubblewrap supplies the filesystem and network
namespace, while a transient systemd service supplies resource limits.  If either
layer cannot be started, execution fails closed.

The sandbox interpreter is a small, dedicated virtual environment.  It must
not be Franziska's normal environment: mounting that would make it too easy to
accidentally grow the sandbox's view of the host.  ``python -m
franziska.sandbox bootstrap`` creates it with the analysis packages (PACKAGES)
in an administrator step, the only time the network is used; ``--bare`` leaves
it standard-library only.  Model-written code never installs anything.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import tempfile
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Callable, Mapping

from pydantic_ai.toolsets import FunctionToolset

from franziska.config import Settings

BWRAP = "/usr/bin/bwrap"
SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEM_PYTHON = "/usr/bin/python3"
MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_FILES = 32
CAPTURE_BYTES = 48 * 1024
MAX_SCRIPT_CHARS = 16_000
MAX_TEXT_INPUTS = 4
MAX_TEXT_INPUT_CHARS = 120_000
MAX_DOCUMENTS = 4
# What `bootstrap` installs: tables, charts, spreadsheets, PDFs (PROPOSALS §3.3 Level 1). Import name → package.
PACKAGES = {"pandas": "pandas", "matplotlib": "matplotlib", "openpyxl": "openpyxl", "pypdf": "pypdf"}


class SandboxError(RuntimeError):
    """The program was not run, or its isolated run did not complete cleanly."""


@dataclass(frozen=True)
class Limits:
    """Hard per-run resource limits, enforced by systemd rather than the model."""

    memory: str = "2G"
    cpu_quota: str = "400%"
    tasks: int = 64
    timeout_s: int = 120
    file_size: str = "1M"  # stdout/stderr: the agent never needs an unbounded transcript


@dataclass(frozen=True)
class Result:
    """The bounded transcript and copied-out artifacts of one completed run."""

    stdout: str
    outputs: tuple[Path, ...]
    returncode: int


def _safe_name(name: str) -> PurePath:
    path = PurePath(name)
    if not name or path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise SandboxError(f"unsafe sandbox input name: {name!r}")
    return path


def _copy_input(value: bytes | Path, destination: Path) -> None:
    if isinstance(value, bytes):
        if len(value) > MAX_INPUT_BYTES:
            raise SandboxError(f"input {destination.name!r} is larger than {MAX_INPUT_BYTES // 1024 // 1024} MiB")
        destination.write_bytes(value)
        return
    source = value.expanduser()
    try:
        mode = source.lstat().st_mode
    except FileNotFoundError as e:
        raise SandboxError(f"sandbox input does not exist: {source}") from e
    if not stat.S_ISREG(mode):
        raise SandboxError(f"sandbox input is not a regular file: {source}")
    if source.stat().st_size > MAX_INPUT_BYTES:
        raise SandboxError(f"input {source.name!r} is larger than {MAX_INPUT_BYTES // 1024 // 1024} MiB")
    shutil.copyfile(source, destination, follow_symlinks=False)


class Sandbox:
    """Run Python programs in the private-data-free Level 1 sandbox."""

    def __init__(self, venv: Path, jobs_dir: Path, limits: Limits | None = None):
        self.venv = venv.expanduser().resolve()
        self.jobs_dir = jobs_dir.expanduser()
        self.limits = limits or Limits()

    def problem(self) -> str | None:
        """Why code can't run here (no bwrap, no interpreter, …), or None. Core offers run_python only without one,
        so the model is never handed a tool that can only fail."""
        try:
            self._check_runtime()
        except SandboxError as e:
            return str(e)
        return None

    def packages(self) -> list[str]:
        """The analysis packages installed in the sandbox interpreter (PACKAGES), for the tool's description."""
        lib = self.venv / "lib"
        return [name for name in PACKAGES if any(lib.glob(f"python3*/site-packages/{name}/__init__.py"))]

    @classmethod
    def from_settings(cls, settings: Settings) -> "Sandbox":
        return cls(settings.sandbox_venv_dir, settings.sandbox_jobs_dir,
                   Limits(timeout_s=settings.sandbox_timeout_s))

    @staticmethod
    def _check_runtime_tools() -> None:
        if not Path(BWRAP).is_file() or not os.access(BWRAP, os.X_OK):
            raise SandboxError("bubblewrap is unavailable; refusing to run code without filesystem isolation")
        if not Path(SYSTEMD_RUN).is_file() or not os.access(SYSTEMD_RUN, os.X_OK):
            raise SandboxError("systemd-run is unavailable; refusing to run code without resource limits")

    def _check_runtime(self) -> None:
        self._check_runtime_tools()
        python = self.venv / "bin" / "python"
        if not python.exists() or not os.access(python, os.X_OK):
            raise SandboxError(f"sandbox interpreter is missing: {python}; run `python -m franziska.sandbox bootstrap`")
        # A venv made from pyenv often points at ~/.pyenv.  /home is intentionally absent in the child, so report a
        # useful setup error instead of running a sandbox whose interpreter cannot start.
        target = python.resolve()
        if self.venv not in target.parents and not str(target).startswith(("/usr/", "/lib/", "/lib64/")):
            raise SandboxError(f"sandbox interpreter resolves outside its isolated runtime: {target}")

    def _isolated(self, mounts: list[str], env: dict[str, str], chdir: str, argv: list[str],
                  limits: Limits | None = None) -> list[str]:
        """systemd-run (limits) around bwrap (an empty read-only root, no network): the system's /usr and libraries,
        then exactly `mounts` (bwrap's own --bind/--ro-bind triples), a fresh /tmp, and only `env`."""
        limits = limits or self.limits
        bind_system = [("/usr", "/usr"), ("/lib", "/lib"), ("/lib64", "/lib64")]
        # A transient service (rather than --scope) is what permits --wait and --pipe: the parent can return the
        # bounded transcript and know the cgroup has finished before it handles outputs.
        cmd = [SYSTEMD_RUN, "--user", "--quiet", "--wait", "--pipe", "--collect",
               # No swap: past MemoryMax the job is killed at once, not left swapping the host for its whole run.
               "-p", f"MemoryMax={limits.memory}", "-p", "MemorySwapMax=0",
               "-p", f"CPUQuota={limits.cpu_quota}",
               "-p", f"TasksMax={limits.tasks}", "-p", f"RuntimeMaxSec={limits.timeout_s}s",
               "-p", f"LimitFSIZE={limits.file_size}",
               # bwrap otherwise starts from the host root filesystem; namespaces alone do not hide it.  Build a
               # fresh empty root, then add back precisely the runtime paths below.
               BWRAP, "--unshare-all", "--die-with-parent", "--new-session", "--as-pid-1", "--tmpfs", "/"]
        for source, destination in bind_system:
            if Path(source).exists():
                cmd += ["--ro-bind", source, destination]
        # Python installations commonly resolve /usr/bin/python via this directory.  It contains symlinks, not
        # credentials, and mounting it avoids opening all of /etc.
        if Path("/etc/alternatives").is_dir():
            cmd += ["--ro-bind", "/etc/alternatives", "/etc/alternatives"]
        if Path("/etc/fonts").is_dir():  # fontconfig's configuration (no data): chart text without warnings
            cmd += ["--ro-bind", "/etc/fonts", "/etc/fonts"]
        cmd += mounts
        cmd += ["--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                # The empty root is a tmpfs while we assemble it.  Make that mount read-only afterwards; the writable
                # binds and /tmp are child mounts and remain the only writable locations.
                "--remount-ro", "/", "--clearenv"]
        for key, value in env.items():
            cmd += ["--setenv", key, value]
        return cmd + ["--chdir", chdir, *argv]

    def _command(self, job: Path) -> list[str]:
        work, inputs = job / "work", job / "in"
        mounts = ["--dir", "/venv", "--ro-bind", str(self.venv), "/venv",
                  "--dir", "/in", "--ro-bind", str(inputs), "/in",
                  "--dir", "/work", "--bind", str(work), "/work"]
        env = {"HOME": "/work", "PATH": "/venv/bin:/usr/bin", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "LANG": "C.UTF-8",
               # No display: matplotlib must not look for one, and its font cache goes in the throwaway /tmp.
               "MPLBACKEND": "Agg", "MPLCONFIGDIR": "/tmp/matplotlib"}
        return self._isolated(mounts, env, "/work", ["/venv/bin/python", "script.py"])

    def shell(self, command: str, binds: Mapping[str, Path], read_only: Mapping[str, Path], env: Mapping[str, str],
              limits: Limits, chdir: str = "/repo") -> Result:
        """One `bash -c command` for a coding job (code.py): `binds` are the writable mounts (the job's clone at /repo,
        its /out), `read_only` the toolchain (a project venv and its interpreter, at their own paths). No network,
        no host home; HOME is a throwaway directory under /tmp. The transcript keeps its head and its tail."""
        self._check_runtime_tools()
        mounts: list[str] = []
        for destination, source in binds.items():
            mounts += ["--bind", str(source), destination]
        for destination, source in read_only.items():
            mounts += ["--ro-bind", str(source), destination]
        base = {"HOME": "/tmp/home", "PATH": "/usr/bin", "LANG": "C.UTF-8", "TERM": "dumb",
                "PYTHONDONTWRITEBYTECODE": "1", "GIT_PAGER": "cat", "PAGER": "cat"}
        argv = ["/usr/bin/bash", "-c", "mkdir -p \"$HOME\" && " + command]
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(prefix="shell-", dir=self.jobs_dir) as log:
            try:
                completed = subprocess.run(self._isolated(mounts, {**base, **env}, chdir, argv, limits),
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           timeout=limits.timeout_s + 20, check=False)
            except subprocess.TimeoutExpired as e:
                raise SandboxError("sandbox supervisor timed out; its systemd unit will be collected") from e
            log.flush()
            size = os.fstat(log.fileno()).st_size
            with open(log.name, "rb") as f:
                head = f.read(CAPTURE_BYTES // 4)
                if size > CAPTURE_BYTES:
                    f.seek(size - CAPTURE_BYTES * 3 // 4)
                    text = (head.decode("utf-8", "replace") + f"\n[… {size - CAPTURE_BYTES} bytes cut …]\n"
                            + f.read().decode("utf-8", "replace"))
                else:
                    text = (head + f.read()).decode("utf-8", "replace")
        return Result(text, (), completed.returncode)

    @staticmethod
    def _outputs(work: Path, destination: Path) -> tuple[Path, ...]:
        produced = work / "out"
        if not produced.exists():
            return ()
        if not produced.is_dir() or produced.is_symlink():
            raise SandboxError("sandbox output /work/out is not a directory")
        files: list[Path] = []
        total = 0
        for item in sorted(produced.rglob("*")):
            if item.is_dir():
                continue
            mode = item.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise SandboxError(f"sandbox output contains a non-regular file: {item.relative_to(produced)}")
            total += item.stat().st_size
            if len(files) >= MAX_OUTPUT_FILES or total > MAX_OUTPUT_BYTES:
                raise SandboxError("sandbox output exceeds the file-count or size limit")
            relative = item.relative_to(produced)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target, follow_symlinks=False)
            files.append(target)
        return tuple(files)

    def run(self, script: str, inputs: Mapping[str, bytes | Path], output_dir: Path) -> Result:
        """Run ``script`` and copy only regular files from ``/work/out`` to ``output_dir``.

        Non-zero exits are returned (their bounded output explains ordinary script errors); failures to construct the
        isolation boundary raise ``SandboxError``.  The temporary job directory is always removed afterwards.
        """
        self._check_runtime()
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="job-", dir=self.jobs_dir) as raw_job:
            job = Path(raw_job)
            job.chmod(0o700)
            work, in_dir = job / "work", job / "in"
            work.mkdir(mode=0o700)
            (work / "out").mkdir(mode=0o700)  # the one place outputs are copied from; it exists before the program
            in_dir.mkdir(mode=0o700)
            (work / "script.py").write_text(script, encoding="utf-8")
            for name, value in inputs.items():
                _copy_input(value, in_dir / _safe_name(name))
            log = job / "transcript.txt"
            try:
                with log.open("wb") as stream:
                    completed = subprocess.run(self._command(job), stdin=subprocess.DEVNULL, stdout=stream,
                                                 stderr=subprocess.STDOUT, timeout=self.limits.timeout_s + 20,
                                                 check=False)
            except subprocess.TimeoutExpired as e:
                raise SandboxError("sandbox supervisor timed out; its systemd scope will be collected") from e
            transcript = log.read_bytes()[:CAPTURE_BYTES].decode("utf-8", "replace")
            outputs = self._outputs(work, output_dir) if completed.returncode == 0 else ()
            return Result(transcript, outputs, completed.returncode)


def _doc_name(doc_id: int, title: str, has_original: bool) -> str:
    """`doc<id><ext>`: a name the model can write before the run, the original's extension or .txt for its text."""
    ext = PurePath(title).suffix.lower() if has_original else ".txt"
    if not ext or len(ext) > 6 or not ext[1:].isalnum():
        ext = ".bin" if has_original else ".txt"
    return f"doc{doc_id}{ext}"


DESCRIPTION = """Run a short Python program for data analysis in an isolated sandbox: charts, pivots, totals,
conversions (CSV ↔ xlsx), merging or splitting PDFs.

The program has no network, shell, home directory, Franziska databases or credentials. It sees only:
- `/in/<name>`: the text of each entry of `inputs` (rows you got from a tool, or text the user pasted);
{documents}Write result files (png, csv, xlsx, pdf) directly in `/work/out` (it exists): they are sent to the user with your reply.
print() what you need to read back; stdout is returned to you (cut at 48 KB). Installed: {packages}.
Charts: matplotlib, save with plt.savefig('/work/out/<name>.png', dpi=150, bbox_inches='tight'); label the axes in
the user's language. A program that fails returns its traceback: fix it and run it once more."""

DOCUMENTS = """- `/in/doc<N>.<ext>`: the original file of each library document number in `documents` (e.g. document #5, a CSV,
  is `/in/doc5.csv`; one without an original file is `/in/doc5.txt`, its text). Use `glob('/in/doc5.*')` if unsure.
"""


def toolset(sandbox: Sandbox, results_dir: Path, artifacts: list[Path],
            open_document: Callable[[int], tuple[str, bytes | None, str] | None] | None = None) -> FunctionToolset:
    """The one Level-1 agent tool: isolated Python over explicit, copied inputs.

    The caller owns ``artifacts`` for one reply. There is intentionally no path argument: a model cannot name a host
    file, and it cannot choose where an output will be published. ``open_document(id)`` is the input bridge from the
    library, already filtered by the route's privacy classes: (title, original bytes or None, text) or None.
    """
    tools = FunctionToolset()
    installed = ", ".join(sandbox.packages()) or "the standard library only"

    documents_line = DOCUMENTS if open_document else ""

    @tools.tool_plain(description=DESCRIPTION.format(packages=installed, documents=documents_line))
    def run_python(script: str, inputs: dict[str, str] | None = None, documents: list[int] | None = None) -> str:
        """
        Args:
            script: Complete Python source. Read inputs from `/in`; write result files to `/work/out/`.
            inputs: At most four small text inputs, e.g. `{"spending.json": "[{...}]"}`. Names have no paths.
            documents: Library document numbers (#N) whose files to copy to `/in`; at most four.
        """
        if len(script) > MAX_SCRIPT_CHARS:
            return f"Not run: the script is over {MAX_SCRIPT_CHARS} characters; simplify it."
        inputs, documents = inputs or {}, documents or []
        if len(inputs) > MAX_TEXT_INPUTS:
            return f"Not run: use at most {MAX_TEXT_INPUTS} input files."
        if len(documents) > MAX_DOCUMENTS:
            return f"Not run: use at most {MAX_DOCUMENTS} documents."
        if documents and open_document is None:
            return "Not run: there are no documents here; pass the data in `inputs`."
        try:
            encoded: dict[str, bytes] = {name: value.encode("utf-8") for name, value in inputs.items()}
        except (AttributeError, UnicodeError):
            return "Not run: every input must be UTF-8 text."
        if sum(len(value) for value in encoded.values()) > MAX_TEXT_INPUT_CHARS:
            return f"Not run: copied text inputs are limited to {MAX_TEXT_INPUT_CHARS} characters in total."
        copied = []
        for doc_id in dict.fromkeys(documents):
            got = open_document(doc_id)
            if got is None:
                return f"Not run: there is no document #{doc_id} you can read. list_documents shows them."
            title, original, text = got
            name = _doc_name(doc_id, title, original is not None)
            if name in encoded:
                return f"Not run: the input {name!r} has a document's name; rename the input."
            encoded[name] = original if original is not None else text.encode("utf-8")
            copied.append(f"/in/{name} ({title})")
        output = results_dir / secrets.token_urlsafe(10)
        try:
            result = sandbox.run(script, encoded, output)
        except SandboxError as e:
            shutil.rmtree(output, ignore_errors=True)
            return f"Not run: {e}"
        if result.returncode:
            shutil.rmtree(output, ignore_errors=True)
            why = result.stdout[-6000:].strip() or (
                f"(no output: it was killed for going over {sandbox.limits.memory} of memory or "
                f"{sandbox.limits.timeout_s} s; work on less data or aggregate it first)")
            return f"The program failed (exit {result.returncode}); nothing was sent. Fix it and run it once more.\n{why}"
        artifacts.extend(result.outputs)
        summary = result.stdout.strip() or "(no stdout)"
        names = ", ".join(p.name for p in result.outputs)
        sent = (f"Files that go to the user with your reply: {names}. Don't paste links or paths to them."
                if names else "No files were written to /work/out.")
        return (f"Done{' (inputs: ' + ', '.join(copied) + ')' if copied else ''}. stdout:\n{summary}\n{sent}")
    return tools


def install(venv: Path, packages: list[str] | None = None) -> None:
    """Install the analysis packages into the sandbox interpreter: an administrator step on the host, with network,
    that never runs model-written code. Wheels only, so no package's build script runs either."""
    uv = shutil.which("uv")
    if not uv:
        raise SandboxError("uv is unavailable; cannot install into the sandbox runtime")
    python = venv.expanduser() / "bin" / "python"
    subprocess.run([uv, "pip", "install", "--python", str(python), "--only-binary", ":all:",
                    *(packages or PACKAGES.values())], check=True)


def bootstrap(venv: Path, packages: bool = True) -> None:
    """Create the dedicated interpreter with the distro Python, not the caller's project venv, and (unless
    `packages` is False) install PACKAGES into it. Never runs model-provided code.
    """
    venv = venv.expanduser()
    if venv.exists() and any(venv.iterdir()):
        raise SandboxError(f"sandbox venv already exists: {venv}")
    if not Path(SYSTEM_PYTHON).is_file():
        raise SandboxError(f"system Python is unavailable: {SYSTEM_PYTHON}")
    uv = shutil.which("uv")
    if not uv:
        raise SandboxError("uv is unavailable; cannot create the dedicated sandbox runtime")
    venv.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The box's distro Python deliberately has no ensurepip/venv package installed.  uv can still make a clean venv
    # around that already-present interpreter; no Python download or package install is requested here.
    subprocess.run([uv, "venv", "--python", SYSTEM_PYTHON, "--no-project", str(venv)], check=True)
    if packages:
        install(venv)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Franziska's isolated Python runtime (no code is run).")
    parser.add_argument("command", choices=("bootstrap", "install", "status"),
                        help="bootstrap: create it with PACKAGES; install: add or upgrade PACKAGES; status: check it")
    parser.add_argument("--venv", type=Path, help="override FRANZISKA_SANDBOX_VENV")
    parser.add_argument("--bare", action="store_true", help="bootstrap with the standard library only")
    args = parser.parse_args()
    settings = Settings.from_env()
    venv = args.venv or settings.sandbox_venv_dir
    if args.command == "bootstrap":
        bootstrap(venv, packages=not args.bare)
        print(f"Created sandbox runtime at {venv}")
    elif args.command == "install":
        install(venv)
    sandbox = Sandbox(venv, settings.sandbox_jobs_dir)
    problem = sandbox.problem()
    print(f"sandbox at {venv}: " + (f"NOT READY: {problem}" if problem else
                                    f"ready; packages: {', '.join(sandbox.packages()) or 'none'}"))


if __name__ == "__main__":
    main()
