"""Retrieval eval for the document library (PROPOSALS §2.4, step L1): which search finds the page that answers.

Each configuration runs the real `Library.search` on a scratch library.sqlite:
- `words`: FTS5 BM25 alone (what the library does without an embedding server);
- `meaning:<model>`: vectors alone;
- `hybrid:<model>`: both, fused by RRF (what the bot runs with FRANZISKA_EMBED_URL);
- `hybrid+rerank:<model>`: the fused top 20 reordered by the cross-encoder (`--rerank`).
Every embedding model in EMBEDDERS that has a GGUF is served here on the CPU (llama-server -ngl 0, as
deploy/franziska-embed.service runs it), one at a time, so the GPU and the bot's model are left alone.

Measured: recall@1 and @5 by page (the passage's document and page are the labelled ones), the right document in
the top 5, MRR, ms per query, and the seconds to embed the corpus. Per question kind (exact / paraphrase / cross).

    uv run python -m evals.library                                   # the synthetic corpus (evals/library_corpus.py)
    uv run python -m evals.library --rerank                          # also the reranker on top
    uv run python -m evals.library --docs ~/docs --questions q.jsonl # your own files; they and the run stay local

`--questions` is JSONL, one {"q": "...", "doc": "<file name in --docs>", "page": 3, "kind": "paraphrase"} per
line. Results: evals/runs/library-<time>/ (results.jsonl, summary.md).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import httpx

from evals.library_corpus import DOCS, QUESTIONS
from evals.mail_eval import CUDART, GGUF, LLAMA
from franziska import documents
from franziska.config import ROOT
from franziska.library import Embedder, Library, Reranker

EMBEDDERS = {"qwen3-embedding-0.6b": "Qwen3-Embedding-0.6B-Q8_0.gguf",
             "embeddinggemma-300m": "embeddinggemma-300M-Q8_0.gguf"}
RERANKER = "bge-reranker-v2-m3-Q8_0.gguf"
EMBED_PORT, RERANK_PORT = 8191, 8192
THREADS = 6  # of the 12 cores; the unit uses 4 so a turn keeps the rest
K = 5


@contextmanager
def cpu_server(gguf: str, port: int, kind: str, log: Path):
    """llama-server on the CPU only (no CUDA device visible), --embedding or --reranking, for the block."""
    cmd = [str(LLAMA / "llama-server"), "-m", str(GGUF / gguf), "--host", "127.0.0.1", "--port", str(port),
           "-ngl", "0", f"--{kind}", "-c", "8192", "-b", "2048", "-ub", "2048", "-t", str(THREADS)]
    env = {**os.environ, "LD_LIBRARY_PATH": f"{LLAMA}:{CUDART}", "CUDA_VISIBLE_DEVICES": ""}
    proc = subprocess.Popen(cmd, stdout=log.open("w"), stderr=subprocess.STDOUT, env=env)
    try:
        for _ in range(120):
            if proc.poll() is not None:
                raise RuntimeError(f"llama-server exited, see {log}")
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise RuntimeError(f"llama-server not healthy after 120 s, see {log}")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=30)


def corpus(args) -> tuple[dict[str, str], list[dict]]:
    """{title: text with form feeds between pages}, and the questions."""
    if not args.docs:
        return ({t: "\f".join(pages) for t, pages in DOCS.items()},
                [{"q": q, "doc": d, "page": p, "kind": k} for q, d, p, k in QUESTIONS])
    docs = {}
    for f in sorted(Path(args.docs).expanduser().iterdir()):
        if f.is_file() and (text := documents.text_of(f.name, "", f.read_bytes()).strip()):
            docs[f.name] = text
    questions = [json.loads(line) for line in Path(args.questions).read_text().splitlines() if line.strip()]
    return docs, questions


def build(docs: dict[str, str], where: Path, embedder: Embedder | None) -> tuple[Library, float]:
    lib = Library(where / "library.sqlite", where / "files", datetime.now, embedder)
    for title, text in docs.items():
        lib.add(title, text, "eval", "personal")
    started = time.monotonic()
    if embedder:
        lib.fill(batch=16, max_s=1800)
        if left := lib.pending():
            raise RuntimeError(f"{left} chunks not embedded: is the server up?")
    return lib, time.monotonic() - started


def score(lib: Library, questions: list[dict], mode: str) -> list[dict]:
    out = []
    for q in questions:
        started = time.monotonic()
        hits = lib.search(q["q"], ("personal",), mode=mode, limit=K)
        ms = (time.monotonic() - started) * 1000
        rank = next((i + 1 for i, h in enumerate(hits) if h.doc.title == q["doc"] and h.page == q["page"]), None)
        out.append({**q, "rank": rank, "doc_hit": any(h.doc.title == q["doc"] for h in hits), "ms": round(ms, 1),
                    "got": [f"{h.doc.title} p.{h.page} ({h.via})" for h in hits]})
    return out


def summarise(name: str, rows: list[dict], ingest_s: float | None) -> dict:
    def stats(rs: list[dict]) -> dict:
        n = len(rs) or 1
        return {"n": len(rs), "r1": sum(r["rank"] == 1 for r in rs) / n,
                "r5": sum(r["rank"] is not None for r in rs) / n,
                "doc5": sum(r["doc_hit"] for r in rs) / n,
                "mrr": sum(1 / r["rank"] for r in rs if r["rank"]) / n}
    kinds = sorted({r["kind"] for r in rows})
    return {"config": name, **stats(rows), "ms": statistics.median(r["ms"] for r in rows),
            "ingest_s": ingest_s, "kinds": {k: stats([r for r in rows if r["kind"] == k]) for k in kinds}}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--docs", help="a folder of your own documents (PDF, .docx, .xlsx, .txt, photos)")
    p.add_argument("--questions", help="JSONL of questions about --docs")
    p.add_argument("--embedders", default=",".join(EMBEDDERS), help="keys of EMBEDDERS to compare")
    p.add_argument("--rerank", action="store_true", help=f"also rerank with {RERANKER}")
    args = p.parse_args()
    if bool(args.docs) != bool(args.questions):
        p.error("--docs and --questions go together")
    docs, questions = corpus(args)
    run = ROOT / "evals" / "runs" / f"library-{datetime.now():%Y%m%d-%H%M}"
    run.mkdir(parents=True, exist_ok=True)
    print(f"{len(docs)} documents, {len(questions)} questions → {run}")
    results, summaries = [], []

    def record(name: str, rows: list[dict], ingest_s: float | None = None) -> None:
        results.extend({"config": name, **r} for r in rows)
        s = summarise(name, rows, ingest_s)
        summaries.append(s)
        print(f"  {name:36} r@1 {s['r1']:.2f}  r@5 {s['r5']:.2f}  doc@5 {s['doc5']:.2f}  mrr {s['mrr']:.2f}  "
              f"{s['ms']:.0f} ms" + (f"  ingest {ingest_s:.1f} s" if ingest_s else ""))

    with tempfile.TemporaryDirectory() as tmp:
        lib, _ = build(docs, Path(tmp), None)
        record("words", score(lib, questions, "words"))
        for key in args.embedders.split(","):
            if not (GGUF / EMBEDDERS[key]).exists():
                print(f"  {key}: no {EMBEDDERS[key]} in {GGUF}, skipped")
                continue
            with cpu_server(EMBEDDERS[key], EMBED_PORT, "embedding", run / f"{key}.log") as url, \
                    tempfile.TemporaryDirectory() as where:
                lib, ingest_s = build(docs, Path(where), Embedder(url))
                record(f"meaning:{key}", score(lib, questions, "meaning"), ingest_s)
                record(f"hybrid:{key}", score(lib, questions, "hybrid"))
                if args.rerank:
                    with cpu_server(RERANKER, RERANK_PORT, "reranking", run / "rerank.log") as rurl:
                        lib.reranker = Reranker(rurl)
                        record(f"hybrid+rerank:{key}", score(lib, questions, "hybrid"))

    (run / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in results))
    kinds = sorted({q["kind"] for q in questions})
    lines = [f"# Library retrieval, {datetime.now():%Y-%m-%d %H:%M}", "",
             f"{len(docs)} documents, {len(questions)} questions ({', '.join(kinds)}); top {K}; "
             f"{'your documents' if args.docs else 'the synthetic corpus'}.", "",
             "| config | r@1 | r@5 | doc@5 | MRR | ms/query | embed corpus s | "
             + " | ".join(f"{k} r@5" for k in kinds) + " |",
             "|---" * (7 + len(kinds)) + "|"]
    for s in summaries:
        lines.append(f"| {s['config']} | {s['r1']:.2f} | {s['r5']:.2f} | {s['doc5']:.2f} | {s['mrr']:.2f} | "
                     f"{s['ms']:.0f} | {s['ingest_s']:.1f} | " if s["ingest_s"] else
                     f"| {s['config']} | {s['r1']:.2f} | {s['r5']:.2f} | {s['doc5']:.2f} | {s['mrr']:.2f} | "
                     f"{s['ms']:.0f} | | ")
        lines[-1] += " | ".join(f"{s['kinds'][k]['r5']:.2f}" for k in kinds) + " |"
    misses = [r for r in results if r["rank"] is None]
    if misses:
        lines += ["", "## Misses (not in the top 5)", ""]
        lines += [f"- `{r['config']}` [{r['kind']}] {r['q']} → want {r['doc']} p.{r['page']}; got "
                  f"{'; '.join(r['got'][:3]) or 'nothing'}" for r in misses]
    (run / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"\n{run / 'summary.md'}")


if __name__ == "__main__":
    main()
