"""llama.cpp settings sweep on this box (RTX 3060 12 GB, Ryzen 5 3600, DDR4): which flags change speed, and by how much.
docs/LLAMA_SETTINGS.md explains each flag; this measures them. Two parts:

  bench   llama-bench grids (no server): threads, -ub, load mode, KV type, at an empty context and at 8k filled.
          One llama-bench process per model-loading setting (load mode, placement); flags that don't reload the
          model (threads, -ub, depth) are lists inside that process, so the grid is cheap.
  spec    speculative decoding needs the server: speed_bench.py configs (MTP draft length, DFlash block length).

Peak VRAM per process is sampled with nvidia-smi. The bot's model server is stopped for the sweep and started
again after (also on Ctrl-C).

    uv run python evals/speed/sweep.py                 # everything (~1.5 h)
    uv run python evals/speed/sweep.py --only gemma-q4km,gsq-iq3
    uv run python evals/speed/sweep.py --list

Results: evals/speed/sweep-<time>.jsonl (one row per llama-bench test) and sweep-<time>.md (sorted tables).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

L = Path.home() / ".local/opt/llama.cpp"
BIN, CUDART = L / "llama-b11936", L / "cudart-llama-b11936-bin-ubuntu-cuda-12.8-x64"
G = Path.home() / "models/gguf"
HERE = Path(__file__).parent
GSQ = G / "Qwen3.8-27B-GSQ-RCO-IQ3_XXS.gguf"

# The same measurement for every row: a 2048-token prompt and 128 generated tokens, at depth 0 and 8192 (an agent
# turn already carries ~5.5k tokens of tools and instructions), 2 repetitions each.
COMMON = ["-p", "2048", "-n", "128", "-d", "0,8192", "-r", "2", "-fa", "on", "-o", "jsonl"]
# Quick-signal grids (2026-11-01): the values that decide, not every value. Gemma's experts run on the CPU, so
# threads (6 real cores vs 12 with SMT) and the prompt batch matter; dense models are all on the GPU, so only the
# batch and the KV type (matched pairs) are left.
MOE = ["-ctk", "q8_0", "-ctv", "q8_0", "-fitt", "1024", "-fitc", "32768", "-t", "6,12", "-ub", "512,2048"]
DENSE = ["-ngl", "99", "-ub", "512,1024"]

# name → (gguf, [one arg list per llama-bench process])
BENCH: dict[str, tuple[str, list[list[str]]]] = {
    "gemma-q4km": ("gemma-4-26B-A4B-it-UD-Q4_K_M.gguf", [[*MOE, "-lm", "mmap"], [*MOE, "-lm", "none"]]),
    "gemma-qat": ("gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", [[*MOE, "-lm", "mmap"], [*MOE, "-lm", "none"]]),
    "gsq-iq3": (str(GSQ), [[*DENSE, "-ctk", "q4_0", "-ctv", "q4_0"], [*DENSE, "-ctk", "q8_0", "-ctv", "q8_0"]]),
    "gsq-iq2xs": ("Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf",
                  [[*DENSE, "-ctk", "q4_0", "-ctv", "q4_0"], [*DENSE, "-ctk", "q8_0", "-ctv", "q8_0"]]),
}
# Context ladder: the same model with placement fitted for a 32k / 64k / 128k context (--fit moves more experts or
# layers to RAM to make room for the cache), measured empty and with 24k already in the context. The question is
# "how fast is this model at the context the bot needs", not "at the context that flatters it": production went
# past 24k 64 times and hit the 32k ceiling twice (journal, 2026-10-27..11-01).
LADDER_COMMON = ["-p", "512", "-n", "128", "-d", "0,24576", "-r", "2", "-fa", "on", "-o", "jsonl", "-t", "6"]
LADDER_CTX = (32768, 65536, 131072)


def ladder(gguf: str, kv: str = "q8_0", margin: int = 1024) -> tuple[str, list[list[str]], list[str]]:
    return (gguf, [["-ctk", kv, "-ctv", kv, "-fitt", str(margin), "-fitc", str(c)] for c in LADDER_CTX], LADDER_COMMON)


LADDER = {
    "ladder-gemma-q4km": ladder("gemma-4-26B-A4B-it-UD-Q4_K_M.gguf"),
    "ladder-gemma-qat": ladder("gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf"),
    "ladder-qwen3.6": ladder("Qwen3.6-35B-A3B-UD-IQ4_XS.gguf"),
    "ladder-lfm24b": ladder("LFM2.5-24B-A2B-UD-IQ3_XXS.gguf"),
    "ladder-ornith35": ladder("Ornith-1.5-35B-Q4_K_M.gguf"),
    "ladder-gsq-iq3": ladder(str(GSQ), "q4_0", 256),
    "ladder-gsq-iq2xs": ladder("Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf", "q4_0", 256),
    "ladder-qwen3.5-4b": ladder("Qwen3.5-4B-UD-Q6_K_XL.gguf"),
}
BENCH.update(LADDER)

# speed_bench.py configs for the speculative part (defined there, run there)
SPEC_GROUPS = {
    "spec-dflash": ["g26-fit", "g26-fit-dflash4", "g26-fit-dflash8", "g26-fit-dflash15", "g26qat-fit", "g26qat-fit-dflash8"],
    "spec-mtp": ["gsq-16kq4", "gsq-16kq4-mtp2", "gsq-16kq4-mtp3", "gsq-16kq4-mtp4",
                 "iq2xs-32kq8", "iq2xs-32kq8-mtp2", "iq2xs-32kq8-mtp3", "iq2xs-32kq8-mtp4"],
}


BENCH_TIMEOUT_S = 40 * 60  # per llama-bench process
SPEC_TIMEOUT_S = 15 * 60  # per speed_bench config (load + one 400-token request)


def wait_free_vram(limit_s: int = 300) -> None:
    """Nothing else on the GPU, or a placement lands in RAM. Raises after limit_s instead of waiting forever."""
    t0 = time.time()
    while vram() > 1500:
        if time.time() - t0 > limit_s:
            raise RuntimeError(f"GPU still holds {vram()} MiB after {limit_s} s: something else is running")
        time.sleep(2)


def vram() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return int(out.strip() or 0)


def bench(name: str, out: Path, have: list[dict]) -> list[dict]:
    gguf, procs, *common = BENCH[name]
    common = common[0] if common else COMMON
    rows = []
    env = {**os.environ, "LD_LIBRARY_PATH": f"{BIN}:{CUDART}"}
    for args in procs:
        if any(r["name"] == name and r["args"] == " ".join(args) and "error" not in r for r in have):
            print(f"  {name} {' '.join(args)}: already measured", flush=True)
            continue
        peak, stop = [0], threading.Event()

        def sample():
            while not stop.is_set():
                peak[0] = max(peak[0], vram())
                time.sleep(1)

        th = threading.Thread(target=sample, daemon=True)
        th.start()
        t0 = time.time()
        try:  # one process is ~12 min; a hung one is killed, recorded, and the sweep goes on
            p = subprocess.run([str(BIN / "llama-bench"), "-m", str(G / gguf), *common, *args], capture_output=True,
                               text=True, env=env, timeout=BENCH_TIMEOUT_S)
        except subprocess.TimeoutExpired as e:
            p = subprocess.CompletedProcess(e.cmd, -9, e.stdout or "", f"timeout after {BENCH_TIMEOUT_S} s")
            p.stdout = p.stdout.decode() if isinstance(p.stdout, bytes) else p.stdout
        stop.set()
        th.join()
        got = [json.loads(line) for line in p.stdout.splitlines() if line.startswith("{")]
        if not got:
            err = (p.stderr.strip().splitlines() or ["no output"])[-1][:200]
            rows.append({"name": name, "args": " ".join(args), "error": err})
            print(f"  {name} {' '.join(args)}: {err}", flush=True)
        for g in got:
            rows.append({"name": name, "args": " ".join(args), "peak_vram_mib": peak[0],
                         "test": "pp" if g["n_gen"] == 0 else "tg", "depth": g.get("n_depth", 0),
                         "threads": g["n_threads"], "ub": g["n_ubatch"], "ctk": g["type_k"], "ctv": g["type_v"],
                         "load_mode": next((a for a in ("mmap", "none") if f"-lm {a}" in " ".join(args)), ""),
                         "n_cpu_moe": g.get("n_cpu_moe"), "tps": round(g["avg_ts"], 1), "sd": round(g["stddev_ts"], 1),
                         "fit_ctx": int(args[args.index("-fitc") + 1]) if "-fitc" in args else None})
        print(f"  {name} {' '.join(args)}: {len(got)} tests in {time.time() - t0:.0f}s, peak VRAM {peak[0]} MiB",
              flush=True)
        with out.open("a") as f:
            for r in rows[-max(len(got), 1):]:
                f.write(json.dumps(r) + "\n")
    return rows


def table(rows: list[dict]) -> str:
    """Per model: generation at depth 8192 and prompt speed, best first (what an agent turn feels)."""
    out = []
    for name in dict.fromkeys(r["name"] for r in rows):
        rs = [r for r in rows if r["name"] == name and "error" not in r]
        errs = [r for r in rows if r["name"] == name and "error" in r]
        deep = max((r["depth"] for r in rs), default=8192)
        d = f"{deep // 1024}k"
        out += [f"## {name}", "", f"| fit ctx | threads | ub | KV | load | tg@0 | tg@{d} | pp@0 | pp@{d} | peak VRAM |",
                "|---|---|---|---|---|---|---|---|---|---|"]

        def key(r):
            return (r.get("fit_ctx") or "", r["threads"], r["ub"], f"{r['ctk']}/{r['ctv']}", r["load_mode"])

        keys = dict.fromkeys(key(r) for r in rs)

        def get(k, test, depth):
            return next((r for r in rs if key(r) == k and r["test"] == test and r["depth"] == depth), {})

        lines = []
        for k in keys:
            tgd = get(k, "tg", deep).get("tps", 0)
            lines.append((tgd, f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | {k[4]} | {get(k, 'tg', 0).get('tps', '')} | "
                               f"{tgd} | {get(k, 'pp', 0).get('tps', '')} | {get(k, 'pp', deep).get('tps', '')} | "
                               f"{get(k, 'tg', 0).get('peak_vram_mib', '')} |"))
        out += [line for _, line in sorted(lines, reverse=True)]
        out += [f"- error: `{e['args']}`: {e['error']}" for e in errs] + [""]
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--only", default="", help=f"comma-separated, from {list(BENCH)} and {list(SPEC_GROUPS)}")
    p.add_argument("--out", type=Path, default=HERE / "sweep.jsonl", help="results; a rerun skips what is in it")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()
    if args.list:
        for n, (g, procs, *_) in BENCH.items():
            print(n, g, *(" ".join(a) for a in procs), sep="\n  ")
        for g, cfgs in SPEC_GROUPS.items():
            print(g, *cfgs, sep="\n  ")
        return
    only = [o for o in args.only.split(",") if o] or [*BENCH, *SPEC_GROUPS]
    out = args.out  # one file for the whole campaign: a rerun skips what it already has (reboot-safe)
    spec_out = out.with_name(out.stem + "-spec.jsonl")
    have = [json.loads(line) for line in out.read_text().splitlines() if line.strip()] if out.exists() else []
    have_spec = [json.loads(line) for line in spec_out.read_text().splitlines() if line.strip()] if spec_out.exists() else []
    resident = subprocess.run(["systemctl", "--user", "is-active", "franziska-llm"], capture_output=True).returncode == 0
    subprocess.run(["systemctl", "--user", "stop", "franziska-llm"])
    rows = []
    try:
        for n in only:
            wait_free_vram()  # a server still exiting would push the bench's layers to RAM
            if n.startswith("spec"):
                for cfg in SPEC_GROUPS[n]:  # one process per config, so a hung server costs one config, not the rest
                    if any(r.get("cfg") == cfg and "error" not in r for r in have_spec):
                        print(f"  spec {cfg}: already measured", flush=True)
                        continue
                    try:
                        subprocess.run([sys.executable, str(HERE / "speed_bench.py"), cfg], timeout=SPEC_TIMEOUT_S,
                                       env={**os.environ, "SPEED_OUT": str(spec_out)})
                    except subprocess.TimeoutExpired:
                        print(f"  spec {cfg}: timeout after {SPEC_TIMEOUT_S} s", flush=True)
                        subprocess.run(["pkill", "-f", "llama-server.*--port 8092"])
                        with spec_out.open("a") as f:
                            f.write(json.dumps({"cfg": cfg, "error": f"timeout after {SPEC_TIMEOUT_S} s"}) + "\n")
                    wait_free_vram()
            else:
                rows += bench(n, out, have)
                all_rows = have + rows
                out.with_suffix(".md").write_text(table(all_rows) + "\n")
    finally:
        if resident:
            subprocess.run(["systemctl", "--user", "start", "franziska-llm"])
    print(table(have + rows))


if __name__ == "__main__":
    main()
