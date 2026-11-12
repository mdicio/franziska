"""Collect a model sweep into one markdown file: speed (evals/speed/*.jsonl) and mail-eval runs (evals/runs/*/).

    uv run python -m evals.model_report evals/runs/20261028-0040 evals/runs/20261028-0110 ... > docs/MODEL_FINDINGS.md
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

from evals.mail_eval import HARD

ROOT = Path(__file__).parent
HARD_NAMES = {s.name for s in HARD}


def speed_section() -> list[str]:
    rows = []
    for f in sorted((ROOT / "speed").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    out = ["## Speed (RTX 3060 12 GB, ~2.7k-token mail prompt, thinking on)", "",
           "| config | prefill tok/s | generate tok/s | MTP accepted | VRAM MiB | note |", "|---|---|---|---|---|---|"]
    for r in rows:
        if "runs" not in r:
            out.append(f"| {r['cfg']} | | | | | {r.get('error', '')} |")
            continue
        run = r["runs"][0]
        note = "Ollama loaded during run" if r.get("ollama_interfered") else ""
        out.append(f"| {r['cfg']} | {run['pp']} | {run['tg']} | {run['draft_acc']} | {r['vram_mib']} | {note} |")
    return out


def eval_section(dirs: list[Path]) -> list[str]:
    rows = []
    for d in dirs:
        f = d / "results.jsonl"
        if f.exists():
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    if not rows:
        return ["## Mail eval", "", "no results yet"]
    servers = list(dict.fromkeys(r["server"] for r in rows))

    def k_n(rs):
        return f"{sum(r['ok'] for r in rs)}/{len(rs)}" if rs else "–"

    out = ["## Mail eval: summary", "",
           "| model | hard passed | base passed | median s | p90 s | median out tok | timeouts | Ollama loaded mid-run |",
           "|---|---|---|---|---|---|---|---|"]
    for s in servers:
        rs = [r for r in rows if r["server"] == s]
        secs = sorted(r["seconds"] for r in rs)
        p90 = secs[min(len(secs) - 1, int(0.9 * len(secs)))]
        out.append(f"| {s} | {k_n([r for r in rs if r['scenario'] in HARD_NAMES])} | "
                   f"{k_n([r for r in rs if r['scenario'] not in HARD_NAMES])} | "
                   f"{statistics.median(secs):.0f} | {p90:.0f} | "
                   f"{statistics.median(r['out_tokens'] for r in rs):.0f} | "
                   f"{sum('timeout' in r['note'] for r in rs)} | {sum(bool(r.get('ollama_during')) for r in rs)} |")
    names = list(dict.fromkeys(r["scenario"] for r in rows))
    out += ["", "## Per scenario (passed/runs, median seconds)", "",
            "| scenario | " + " | ".join(servers) + " |", "|---|" + "---|" * len(servers)]
    for n in names:
        cells = []
        for s in servers:
            rs = [r for r in rows if r["server"] == s and r["scenario"] == n]
            cells.append(f"{k_n(rs)} · {statistics.median(r['seconds'] for r in rs):.0f}s" if rs else "")
        out.append(f"| {n}{' (hard)' if n in HARD_NAMES else ''} | " + " | ".join(cells) + " |")
    out += ["", "## Failures", ""]
    for r in rows:
        if not r["ok"]:
            out.append(f"- **{r['server']}** {r['scenario']} #{r['repeat']} ({r['seconds']}s): {r['note'][:300]}")
    out += ["", "Runs: " + ", ".join(str(d.relative_to(ROOT.parent)) for d in dirs)]
    return out


if __name__ == "__main__":
    dirs = [Path(a).resolve() for a in sys.argv[1:]]
    print("\n".join(["# Local models for the mail agent", "", *speed_section(), "", *eval_section(dirs)]))
