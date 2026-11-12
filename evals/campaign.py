"""The 2026-11 model campaign as one resumable command: quick signal first, depth after, best-effort runs last.

    uv run --env-file .env python -m evals.campaign --plan     # what will run, on what, with what settings, how long
    uv run --env-file .env python -m evals.campaign            # run (or resume after a reboot / Ctrl-C)
    uv run --env-file .env python -m evals.campaign --only A   # one phase

Reboot-safe at every level: a step is marked done only when its command finishes; each step's command resumes
itself (the funnel skips finished stages and, inside a stage, finished tasks and scenarios; the sweep skips
finished measurements). So after a restart the same command picks up at the task that was running, losing at
most that one task (a few minutes).

Each step has a wall-clock limit; a step past it is killed with the llama-servers it started, recorded as timed
out, and the campaign goes on. The bot's model server is stopped once at the start and started again at the end
(also on Ctrl-C or an error). Progress: evals/runs/campaign-2026-11/status.json (read by evals/watchdog.sh) and
log.txt; results: the funnel's summary.md and evals/speed/sweep.md.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "evals" / "runs" / "campaign-2026-11"
FUNNEL = "evals/runs/funnel-20261101-1623"  # the first funnel run; every funnel step adds to it
F = [sys.executable, "-m", "evals.funnel", "--resume", FUNNEL]
SWEEP = [sys.executable, "evals/speed/sweep.py", "--only"]
SMALL = "qwen3.5-4b,qwen3.5-2b,gemma4-e4b-qat,nemotron3.5-bolt-e4b,lfm2.5-8b-a1b,lfm2.5-1.2b,ornith1.5-9b"
# qwen3.8-27b-iq2xs left out from C1 on (2026-11-02): the same weights as GSQ IQ3 at a lower quant, 1.6 tok/s faster;
# the GSQ build is the one to decide on. It keeps its speed/router/smoke rows.
MAIN_BEST = "gemma4-26b-a4b,gemma4-26b-qat,qwen3.6-35b-a3b,lfm2.5-24b-a2b,qwen3.8-27b-gsq,ornith1.5-35b-a3b"
# Phase E, wide: every task a model hasn't settled yet (evals.plan), no token cap, one step per model so the
# campaign log shows progress and each has its own limit. Baseline first; GSQ only if its D2 smoke rerun comes within
# 2 checks of the baseline (the funnel's gate; it did: 6 vs 8), and without code tasks (at 21 tok/s they time out at
# 900 s, and they are ~85% of a wide run). Small: quick-chat candidates only, no router gate (the router role is
# gemma's; this asks whether they can take a plain turn). Dropped from E (2026-11-02): qwen3.6-35b-a3b and
# ornith1.5-35b-a3b (12/20 smoke + best effort vs gemma's 16, 2 behind in both runs, slower, router 7-9 s),
# lfm2.5-24b (router 7/54, best-effort mail 1/5), qwen3.5-2b and lfm2.5-1.2b (simple bench 0/3 and 1/3),
# lfm2.5-8b-a1b (router format), qwen3.8-27b-iq2xs (see above).
WIDE_MAIN = [("gemma4-26b-a4b", 90, ""), ("gemma4-26b-qat", 90, ""), ("qwen3.8-27b-gsq", 90, "wide-main-nocode")]
WIDE_SMALL = "qwen3.5-4b,gemma4-e4b-qat,nemotron3.5-bolt-e4b,ornith1.5-9b"

# (id, minutes estimated, limit minutes, what it answers, command)
STEPS: list[tuple[str, int, int, str, list[str]]] = [
    # ---- A: quick signal (~1 h 50)
    ("A1-forget", 0, 2, "drop results measured wrong: gemma baseline speed (downloads were writing), DFlash speed "
     "(OOM, now fixed), Qwen3.5-4B simple (template rejected the requests, now fixed)",
     [*F, "--forget", "speed", "--models", "gemma4-26b-a4b,gemma4-26b-dflash,gemma4-26b-qat-dflash"]),
    ("A1b-forget", 0, 2, "", [*F, "--forget", "simple", "--models", "qwen3.5-4b"]),
    ("A2-speed", 4, 20, "gemma baseline speed on a quiet disk; does DFlash speed gemma up (both builds)?",
     [*F, "--models", "gemma4-26b-a4b,gemma4-26b-dflash,gemma4-26b-qat-dflash"]),
    ("A3-small", 45, 150, "can a small model handle a plain turn alone? (all seven, no router gate)",
     [*F, "--no-gates", "--models", SMALL]),
    ("A4-gemma-settings", 20, 90, "which threads / prompt batch / load mode is fastest for gemma (both builds)?",
     [*SWEEP, "gemma-q4km,gemma-qat"]),
    ("A5-new-models", 60, 240, "main models not tested yet: Qwen3.8 GSQ (medium reasoning), Ornith 35B, Qwen3.8 "
     "IQ2_XS; the Reddit flags and IQ2_XS+MTP as speed-only variants",
     [*F, "--models", "qwen3.8-27b-gsq,qwen3.8-27b-gsq-post,ornith1.5-35b-a3b,qwen3.8-27b-iq2xs,qwen3.8-27b-iq2xs-mtp"]),
    # ---- B: speed in depth (~1 h 30)
    ("B1-speculative", 25, 120, "DFlash draft length 4/8/15 on gemma; MTP draft length 2/3/4 on Qwen3.8 IQ3 and IQ2_XS",
     [*SWEEP, "spec-dflash,spec-mtp"]),
    ("B2-dense-settings", 10, 60, "Qwen3.8 dense: prompt batch 512/1024, KV q4_0 vs q8_0",
     [*SWEEP, "gsq-iq3,gsq-iq2xs"]),
    ("B3-context-ladder", 55, 180, "speed of each model fitted for 32k / 64k / 128k context, empty and with 24k filled",
     [*SWEEP, ",".join(f"ladder-{m}" for m in ("gemma-q4km", "gemma-qat", "qwen3.6", "lfm24b", "ornith35", "gsq-iq3",
                                                "gsq-iq2xs", "qwen3.5-4b"))]),
    # ---- C: best effort (~6 h, overnight)
    ("C1-best-effort", 360, 900, "what each main model can do with room to think (32k tokens/answer, 30 min/task)",
     [*F, "--best", "--models", MAIN_BEST]),
    # ---- D: reruns of what A5 measured wrong (added 2026-11-01 19:25): the GSQ files' template rejected every mail
    # request (HTTP 500, now a lenient copy), and the Reddit variant used --no-mmap, which b11936 doesn't have
    ("D1-forget", 0, 2, "drop Qwen3.8 GSQ smoke (mail never reached the model) and the Reddit variant's speed",
     [*F, "--forget", "smoke", "--models", "qwen3.8-27b-gsq"]),
    ("D1b-forget", 0, 2, "", [*F, "--forget", "speed", "--models", "qwen3.8-27b-gsq-post"]),
    ("D2-rerun", 20, 150, "Qwen3.8 GSQ smoke with the fixed template; the Reddit flags' speed",
     [*F, "--models", "qwen3.8-27b-gsq,qwen3.8-27b-gsq-post"]),
    # ---- E: wide (added 2026-11-02, ~13 h): the 61 bench tasks and 16 mail scenarios outside smoke, never run on
    # most models (the Home-world life/money/notes/mail tasks are the bot's real work); evals.plan skips every pair
    # already finished at the same budget. `python -m evals.plan --models ... --tasks wide-main` shows what will run.
    *[(f"E{i}-wide-{m}", est, est * 3 + 60, f"{m}: {wset or 'wide-main'}, what it hasn't settled yet",
       [*F, "--wide", "--models", m, *(["--wide-set", wset] if wset else [])])
      for i, (m, est, wset) in enumerate(WIDE_MAIN, 1)],
    (f"E{len(WIDE_MAIN) + 1}-wide-small", 50, 180, "quick-chat candidates: the easy tier (no code) and single-step "
     "mail at the chat budget; ornith1.5-9b also gets its router and simple stages",
     [*F, "--wide", "--no-gates", "--models", WIDE_SMALL]),
]

current: subprocess.Popen | None = None


def status(**kw) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / "status.json").write_text(json.dumps({"pid": os.getpid(), "at": datetime.now().isoformat(timespec="seconds"),
                                                 **kw}))


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with (DIR / "log.txt").open("a") as f:
        f.write(line + "\n")


def done() -> set[str]:
    f = DIR / "state.jsonl"
    return {json.loads(line)["step"] for line in f.read_text().splitlines() if line.strip()} if f.exists() else set()


def kill_servers() -> None:
    for port in (8091, 8092):
        subprocess.run(["pkill", "-f", f"llama-server.*--port {port}"], capture_output=True)
    subprocess.run(["pkill", "-f", "llama-bench"], capture_output=True)


def stop(p: subprocess.Popen) -> None:
    """Ctrl-C to the step (the funnel and the sweep clean up on it, the funnel stops its eval), then by force."""
    os.killpg(p.pid, signal.SIGINT)
    try:
        p.wait(60)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
    kill_servers()


def run_step(sid: str, limit_min: int, cmd: list[str]) -> str:
    global current
    with (DIR / f"{sid}.log").open("a") as out:
        out.write(f"\n== {datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(cmd)}\n")
        out.flush()
        current = subprocess.Popen(cmd, cwd=ROOT, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = current.wait(limit_min * 60)
        except subprocess.TimeoutExpired:
            stop(current)
            return f"timeout after {limit_min} min"
        except KeyboardInterrupt:  # stopped here, before `current` is cleared (the handler in main never saw it)
            stop(current)
            raise
        finally:
            current = None
    return "ok" if rc == 0 else f"exit {rc}"


def plan() -> str:
    out, phase = [], ""
    for sid, est, limit, what, cmd in STEPS:
        if sid[0] != phase:
            phase = sid[0]
            total = sum(e for s, e, *_ in STEPS if s[0] == phase)
            out.append(f"\n## Phase {phase} (~{total // 60} h {total % 60:02d} min)")
        mark = "done" if sid in done() else ""
        out.append(f"- {sid} {f'~{est} min' if est else ''} (limit {limit} min) {mark}\n    {what}\n    $ "
                   + " ".join(c.replace(sys.executable, "python") for c in cmd))
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--plan", action="store_true", help="print the steps and what's done, then exit")
    p.add_argument("--only", default="", help="phases or step ids, comma-separated (e.g. A or A3-small,B1-speculative)")
    args = p.parse_args()
    if args.plan:
        print(plan())
        return
    DIR.mkdir(parents=True, exist_ok=True)
    only = [o for o in args.only.split(",") if o]
    steps = [s for s in STEPS if not only or any(s[0] == o or s[0][0] == o for o in only)]
    subprocess.run(["systemctl", "--user", "stop", "franziska-llm"])  # once; the steps then leave it alone
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        for sid, est, limit, what, cmd in steps:
            if sid in done():
                continue
            log(f"start {sid} (~{est} min, limit {limit} min)")
            status(step=sid, state="running", started=datetime.now().isoformat(timespec="seconds"))
            t0 = time.time()
            res = run_step(sid, limit, cmd)
            log(f"{sid}: {res} in {(time.time() - t0) / 60:.0f} min")
            if res == "ok":
                with (DIR / "state.jsonl").open("a") as f:
                    f.write(json.dumps({"step": sid, "at": datetime.now().isoformat(timespec="seconds")}) + "\n")
            # a failed or timed-out step stays not-done: the next run retries it (and it resumes where it was)
        left = [s[0] for s in steps if s[0] not in done()]
        status(state="all done" if not left else "finished with steps left", left=left)
        log("all done" if not left else f"finished; not done: {', '.join(left)}")
    except KeyboardInterrupt:  # run_step has stopped the step already
        kill_servers()
        status(state="interrupted")
        log("interrupted; rerun the same command to resume")
    finally:  # the bot needs its server whatever state the campaign ends in
        subprocess.run(["systemctl", "--user", "start", "franziska-llm"])


if __name__ == "__main__":
    main()
