"""Red-team check for ``franziska.sandbox`` (PROPOSALS §3.4), run on demand.

It deliberately executes hostile-looking code and passes only when the sandbox
cannot see a home directory, Docker, host environment, network or a writable
system tree.  It also proves the normal artifact route still works.

    uv run python -m evals.sandbox
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from franziska import code
from franziska.config import ROOT, Settings
from franziska.sandbox import Limits, Sandbox, bootstrap, toolset

PROBE = r'''
import json
import os
import socket
from pathlib import Path

def denied(fn):
    try:
        fn()
    except Exception:
        return True
    return False

checks = {
    "home_absent": not Path("/home").exists(),
    "docker_absent": not Path("/var/run/docker.sock").exists(),
    "host_env_denied": denied(lambda: Path.home() / ".env".read_bytes()),
    "system_write_denied": denied(lambda: Path("/usr/sandbox-probe").write_text("no")),
    "outside_write_denied": denied(lambda: Path("/outside-sandbox").write_text("no")),
    "network_denied": denied(lambda: socket.create_connection(("1.1.1.1", 53), timeout=1)),
    "dns_denied": denied(lambda: socket.getaddrinfo("example.com", 443)),
    # A fresh PID namespace should show only this process, so there is no other process environment to read.
    "other_processes_absent": not [p for p in Path("/proc").iterdir() if p.name.isdigit() and int(p.name) != os.getpid()],
    "clean_env": set(os.environ) <= {"HOME", "PATH", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE", "LANG", "PWD",
                                   "MPLBACKEND", "MPLCONFIGDIR"},
    "environment": sorted(os.environ),
}
Path("out").mkdir(exist_ok=True)
Path("out/probe.json").write_text(json.dumps(checks, sort_keys=True))
'''


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="franziska-sandbox-") as raw:
        base = Path(raw)
        venv, jobs, output = base / "venv", base / "jobs", base / "output"
        bootstrap(venv, packages=False)  # the boundary, not the packages: no network needed
        result = Sandbox(venv, jobs, Limits(timeout_s=10)).run(PROBE, {}, output)
        if result.returncode:
            raise SystemExit(f"sandbox probe did not run (exit {result.returncode}):\n{result.stdout}")
        if result.outputs != (output / "probe.json",):
            raise SystemExit(f"unexpected sandbox artifacts: {result.outputs}")
        checks = json.loads((output / "probe.json").read_text())
        failed = [name for name, passed in checks.items() if name != "environment" and not passed]
        if failed:
            raise SystemExit("SANDBOX ESCAPE: " + ", ".join(failed) + "\n" + json.dumps(checks, indent=2))
        # systemd must actually enforce the wall-clock limit, not merely receive its option.
        timed = Sandbox(venv, jobs, Limits(timeout_s=2)).run("import time; time.sleep(10)", {}, base / "timeout-output")
        if timed.returncode == 0:
            raise SystemExit("SANDBOX ESCAPE: a job exceeded its wall-clock limit")
        # Memory: a program that fills more than MemoryMax is killed, not the host's memory.
        hog = Sandbox(venv, jobs, Limits(memory="256M", timeout_s=20)).run(
            "blocks = []\nwhile True: blocks.append(bytearray(64 * 1024 * 1024))", {}, base / "memory-output")
        if hog.returncode == 0:
            raise SystemExit("SANDBOX ESCAPE: a job filled memory past its limit")
        # Processes: past TasksMax, fork fails inside the sandbox; the children are killed with the job.
        bomb = Sandbox(venv, jobs, Limits(tasks=16, timeout_s=20)).run(
            "import os, time\nn = 0\ntry:\n    for _ in range(200):\n        if os.fork() == 0:\n            time.sleep(30)\n"
            "            os._exit(0)\n        n += 1\nexcept OSError:\n    pass\nprint(n)", {}, base / "fork-output")
        forked = int(bomb.stdout.strip().splitlines()[-1]) if bomb.stdout.strip() else -1
        if not 0 <= forked < 16:
            raise SystemExit(f"SANDBOX ESCAPE: {forked} processes forked under TasksMax=16:\n{bomb.stdout}")
        # C2: exercise the agent-facing wrapper too. It must copy explicit input text in, expose only its artifact
        # path to the adapter, and preserve that artifact after the private job directory is gone.
        artifacts: list[Path] = []
        tools = toolset(Sandbox(venv, jobs, Limits(timeout_s=10)), base / "results", artifacts)
        run_python = tools.tools["run_python"].function
        reply = run_python(
            "from pathlib import Path\nvalues = [int(x) for x in Path('/in/values.txt').read_text().split()]\n"
            "Path('out/total.txt').write_text(str(sum(values)))\nprint(sum(values))\n",
            {"values.txt": "2 3 5"},
        )
        if not reply.startswith("Done") or len(artifacts) != 1 or artifacts[0].read_text() != "10":
            raise SystemExit(f"C2 artifact handoff failed: {reply!r}, {artifacts!r}")
        jobs_check(base, jobs)
    print("sandbox red-team check passed: host files, network, system writes, memory, forks and the clock were "
          "contained; artifact copied out; a coding job's commands saw only their clone and toolchain")


# C3: a coding job's command (code.py) runs with the job's clone writable at /repo, /out, and this repository's venv
# plus its interpreter read-only at their own paths. Everything else of the home folder must stay invisible.
JOB_PROBE = r"""
probe() { if eval "$2" >/dev/null 2>&1; then echo "$1=0"; else echo "$1=1"; fi; }
probe secrets_absent "cat {root}/.env || ls {home}/.ssh || ls {home}/.config || ls {home}/.local/share/franziska"
probe repo_rest_absent "ls {root}/franziska"
probe venv_read_only "touch {root}/.venv/probe"
probe system_write_denied "touch /usr/probe"
probe outside_write_denied "touch /probe"
probe network_denied "python3 -c 'import socket; socket.create_connection((\"1.1.1.1\", 53), timeout=1)'"
probe docker_absent "test -e /var/run/docker.sock"
probe repo_writable "! touch /repo/probe"
probe out_writable "! touch /out/probe"
probe venv_runs "! {root}/.venv/bin/python -c 'import pydantic_ai'"
"""


def jobs_check(base: Path, jobs: Path) -> None:
    settings = Settings.from_env()
    repo, out = base / "job-repo", base / "job-out"
    repo.mkdir()
    out.mkdir()
    mounts, env, _ = code.toolchain(ROOT, settings)
    result = Sandbox(settings.sandbox_venv_dir, jobs).shell(
        JOB_PROBE.replace("{root}", str(ROOT)).replace("{home}", str(Path.home())),
        {"/repo": repo, "/out": out}, mounts, env, Limits(timeout_s=60, tasks=64))
    checks = dict(line.split("=") for line in result.stdout.split() if "=" in line)
    failed = [name for name, ok in checks.items() if ok != "1"]
    if failed or len(checks) < 10:
        raise SystemExit("CODING JOB SANDBOX: " + (", ".join(failed) or "probe incomplete") + "\n" + result.stdout)


if __name__ == "__main__":
    main()
