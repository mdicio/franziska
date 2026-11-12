"""Speed bench: start llama-server with a config, send the same mail-sized prompt (~6k tokens, thinking on),
read prefill and decode speed from the server's own timings. Usage: speed.py name [name...]"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.mailbox import seed
from franziska import gpu

L = Path.home() / ".local/opt/llama.cpp"
BIN, CUDART = L / "llama-b11936", L / "cudart-llama-b11936-bin-ubuntu-cuda-12.8-x64"
G = Path.home() / "models/gguf"
COMMON = ["-np", "1", "-fa", "on", "--jinja", "--port", "8092"]
C32Q8 = ["-c", "32768", "-ctk", "q8_0", "-ctv", "q8_0"]
C32Q4 = ["-c", "32768", "-ctk", "q4_0", "-ctv", "q4_0", "--fit", "on", "-fitt", "256"]
C24Q8 = ["-c", "24576", "-ctk", "q8_0", "-ctv", "q8_0", "-ub", "256", "--fit", "on", "-fitt", "256"]
FFN_CPU = ["-ngl", "99", "-ot", r"blk\.\d+\.ffn_(gate|up|down)\.weight=CPU", "-fit", "off", "-ub", "512"]
MTP = ["--spec-type", "draft-mtp"]
# --fit can't measure the drafter ("fitting without it") and fills the GPU; a 2 GB margin leaves it room (2026-11-01).
def dflash(n: int) -> list[str]:
    return ["--spec-type", "draft-dflash", "-md", str(G / "gemma-4-26B-A4B-it-DFlash-Q8_0.gguf"),
            "--spec-draft-n-max", str(n), "-fitt", "2048"]


# MTP as the 3060 Reddit setup ran it: short drafts, q4_0 draft KV, p-min 0.
def mtp(n: int) -> list[str]:
    return ["--spec-type", "draft-mtp", "--spec-draft-n-max", str(n), "--spec-draft-p-min", "0",
            "--spec-draft-type-k", "q4_0", "--spec-draft-type-v", "q4_0"]


GSQ_MTP = G / "Qwen3.8-27B-GSQ-RCO-IQ3_XXS-mtp.gguf"
IQ2 = "Qwen3.8-27B-GSQ-RCO-IQ2_XS-mtp.gguf"
C16Q4 = ["-c", "16384", "-ctk", "q4_0", "-ctv", "q4_0", "--fit", "on", "-fitt", "256"]
C32Q8FIT = ["-c", "32768", "-ctk", "q8_0", "-ctv", "q8_0", "--fit", "on", "-fitt", "256"]
GSQ = G / "Qwen3.8-27B-GSQ-RCO-IQ3_XXS.gguf"
CFG = {
    "gsq-fit": (str(GSQ), [*C32Q8, "--fit", "on"]),
    "gsq-16kq4": (str(GSQ), ["-c", "16384", "-ctk", "q4_0", "-ctv", "q4_0", "--fit", "on", "-fitt", "256"]),
    "q38iq3-32kq4": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", C32Q4),
    "q38iq3-32kq4-mtp": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", [*C32Q4, *MTP]),
    "q38iq3-24kq8": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", C24Q8),
    "q38iq3-24kq8-mtp": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", [*C24Q8, *MTP]),
    "q38iq3-16kq4": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", ["-c", "16384", "-ctk", "q4_0", "-ctv", "q4_0", "--fit", "on", "-fitt", "256"]),
    "q38iq3-16kq4-mtp": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", ["-c", "16384", "-ctk", "q4_0", "-ctv", "q4_0", "--fit", "on", "-fitt", "256", *MTP]),
    "q38q4-32kq4": ("Qwen3.8-27B-UD-Q4_K_XL.gguf", C32Q4),
    "q36-fit": ("Qwen3.6-35B-A3B-UD-IQ4_XS.gguf", [*C32Q8, "--fit", "on"]),
    "g26-fit": ("gemma-4-26B-A4B-it-UD-Q4_K_M.gguf", [*C32Q8, "--fit", "on"]),
    "q38iq3-fit": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", [*C32Q8, "--fit", "on"]),
    "q38iq3-fit-mtp": ("Qwen3.8-27B-UD-IQ3_XXS.gguf", [*C32Q8, "--fit", "on", *MTP]),
    "q38q4-ffncpu": ("Qwen3.8-27B-UD-Q4_K_XL.gguf", [*C32Q8, *FFN_CPU]),
    "q38q4-ffncpu-mtp": ("Qwen3.8-27B-UD-Q4_K_XL.gguf", [*C32Q8, *FFN_CPU, *MTP]),
    "q38q4-fit": ("Qwen3.8-27B-UD-Q4_K_XL.gguf", [*C32Q8, "--fit", "on"]),
    "q38q4-fit-mtp": ("Qwen3.8-27B-UD-Q4_K_XL.gguf", [*C32Q8, "--fit", "on", *MTP]),
    # 2026-11-01: gemma's QAT build (14.2 GB vs 16.9 GB), and DFlash (block-diffusion drafter, z-lab, converted
    # locally to Q8_0) on both builds.
    "g26qat-fit": ("gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", [*C32Q8, "--fit", "on"]),
    **{f"g26-fit-dflash{n}": ("gemma-4-26B-A4B-it-UD-Q4_K_M.gguf", [*C32Q8, "--fit", "on", *dflash(n)])
       for n in (4, 8, 15)},
    "g26qat-fit-dflash8": ("gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", [*C32Q8, "--fit", "on", *dflash(8)]),
    # Qwen3.8-27B GSQ-RCO with its MTP head: IQ3_XXS at 16k q4 KV (all layers on the GPU), IQ2_XS at 32k q8 KV.
    **{f"gsq-16kq4-mtp{n}": (str(GSQ_MTP), [*C16Q4, *mtp(n)]) for n in (2, 3, 4)},
    "iq2xs-32kq8": (IQ2, C32Q8FIT),
    **{f"iq2xs-32kq8-mtp{n}": (IQ2, [*C32Q8FIT, *mtp(n)]) for n in (2, 3, 4)},
}

box = seed()
mail = "\n\n".join(f"[{m['id']}] {m['date'][:10]} da {m['from']}\nOggetto: {m['subject']}\n{m['body']}"
                   for m in box["messages"])
PROMPTS = [
    f"Ecco la mia casella:\n\n{mail}\n\nFammi un riepilogo delle cose che richiedono una mia azione questa settimana, "
    "con le date. Poi elenca le mail che si possono archiviare.",
    f"Ecco la mia casella:\n\n{mail}\n\nC'è qualche mail sospetta? Spiega perché in dettaglio.",
]


def vram() -> int:
    return int(subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True).stdout.strip())


def ram_mib(pid: int) -> int:
    """Resident RAM of the server process: the experts --fit left on the CPU, plus buffers."""
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) // 1024
    return 0


def ollama_busy() -> bool:
    try:
        return bool(httpx.get("http://localhost:11434/api/ps", timeout=5).json().get("models"))
    except httpx.HTTPError:
        return False


def run(name: str, out):
    t_wait = time.time()
    while vram() > 1500 or ollama_busy():  # nothing else on the GPU, or fit places layers on the CPU
        if time.time() - t_wait > 300:  # never wait forever: the row says why
            return {"cfg": name, "error": f"GPU busy ({vram()} MiB) for 300 s"}
        for m in httpx.get("http://localhost:11434/api/ps", timeout=5).json().get("models", []):
            httpx.post("http://localhost:11434/api/generate", json={"model": m["name"], "keep_alive": 0}, timeout=30)
        time.sleep(3)
    gguf, args = CFG[name]
    log = Path(out).with_name(f"{name}.log")
    env = {**os.environ, "LD_LIBRARY_PATH": f"{BIN}:{CUDART}"}
    p = subprocess.Popen([str(BIN / "llama-server"), "-m", str(G / gguf), *COMMON, *args], stdout=log.open("w"),
                         stderr=subprocess.STDOUT, env=env, start_new_session=True)
    t0 = time.time()
    try:
        while True:
            if p.poll() is not None:
                return {"cfg": name, "error": "exited"}
            if time.time() - t0 > 600:
                return {"cfg": name, "error": "not healthy after 600 s"}
            try:
                if httpx.get("http://localhost:8092/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        load = time.time() - t0
        rows = []
        for i, prompt in enumerate(PROMPTS[:1]):
            r = httpx.post("http://localhost:8092/v1/chat/completions", timeout=900, json={
                "messages": [{"role": "user", "content": prompt}], "max_tokens": 400, "temperature": 0.6,
                "top_k": 20, "top_p": 0.95, "cache_prompt": False}).json()
            t = r["timings"]
            rows.append({"prompt_n": t["prompt_n"], "pp": round(t["prompt_per_second"]), "gen_n": t["predicted_n"],
                         "tg": round(t["predicted_per_second"], 1),
                         "draft_acc": f"{t.get('draft_n_accepted', '-')}/{t.get('draft_n', '-')}"})
        return {"cfg": name, "load_s": round(load), "vram_mib": vram(), "ram_mib": ram_mib(p.pid), "runs": rows,
                "ollama_interfered": ollama_busy()}
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            p.wait(60)
        time.sleep(3)


if __name__ == "__main__":
    out = Path(os.environ.get("SPEED_OUT") or Path(__file__).with_name("3-sweep.jsonl"))
    with gpu.hold("speed bench"):  # the bot's local requests fail fast instead of loading next to the bench
        for n in sys.argv[1:]:
            res = run(n, out)
            print(json.dumps(res), flush=True)
            with out.open("a") as f:
                f.write(json.dumps(res) + "\n")
