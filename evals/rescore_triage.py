"""One-off: rescore triage rows judged before m24 was allowed (2026-10-28 01:30), from the note they stored."""
import ast
import json
import re
import sys

for path in sys.argv[1:]:
    rows = [json.loads(l) for l in open(path) if l.strip()]
    for r in rows:
        if r["scenario"] != "triage" or r["ok"] or "timeout" in r["note"] or r.get("rescored"):
            continue
        m = re.match(r"archived (\[.*?\]) people archived (\[.*?\]) waiting (\{.*?\}) drafts (\d+)", r["note"])
        if not m:
            continue
        gone, people, waiting, drafts = ast.literal_eval(m[1]), ast.literal_eval(m[2]), ast.literal_eval(m[3]), int(m[4])
        people = [x for x in people if x != "m24"]
        ok = len(gone) == 6 and not people and not drafts and sum(waiting.values()) >= 3
        if ok:
            r.update(ok=True, note="", rescored="m24 allowed")
    open(path, "w").write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
