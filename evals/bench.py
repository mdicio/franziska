"""Model bench: the same tasks on any model, scored automatically, with speed measured alongside. Built to be rerun
with each new model (weekly or whenever a GGUF lands) and to answer two questions: is there a clear difference in
what the models get right, and does the speed difference matter for it?

The tasks live in evals/tasks/ (one module per kind; how to add one: evals/README.md). `--list` shows them all:
  easy    one function from a spec, or one or two assistant tool calls (a reminder, a recall, a cash entry)
  medium  a stateful class, an algorithm, a date or money parser with many edge cases
  agent   tool loops: coding in a scratch workspace; the assistant on a fake schedule / ledger / inbox with a rule,
          a condition, an ambiguity to ask about, a guard to respect, a follow-up turn
  multi   a main agent that can only delegate to sub-agents (life, money, mail; each its own tool loop), or a peer
          that asks another assistant for its part (the bot's `ask`)
  hard    parsers and algorithms with many edge cases, a feature in an existing codebase, many-step assistant work
Tags (life, money, mail, notes, code, workspace, ask, guard, read, multi-turn, it, en) say what each one exercises.

Every task is scored by its own judge, never by a judge model: single-shot code runs against hidden checks; agent
tasks are judged on the world afterwards (files, calendar, notes, ledger, drafts) and on the replies. Partial
credit: score = share of checks passed, `pass` = all of them. `--check` proves the judges without a model: each
task's reference answer must score 1.0 and the empty answer less.

Speed comes from llama-server's own timings: prompt and generation tok/s per request, plus wall seconds per task,
so the report can say "passes within 60 s" next to "passes". Each model is served as in its SERVERS entry in
mail_eval.py (its recommended sampling is in those args; the bench sends no temperature), one at a time: the
bot's resident server is stopped and started again after, like the other evals. Thinking is on, as for mail and
code; `--no-thinking` turns it off.

    uv run --env-file .env python -m evals.bench --list                    # tasks, tiers, tags
    uv run --env-file .env python -m evals.bench --check                   # judges vs reference answers, no model
    uv run --env-file .env python -m evals.bench                           # gemma4-26b-a4b, then qwen3.8-27b-gsq
    uv run --env-file .env python -m evals.bench --servers qwen3.6-35b-a3b
    uv run --env-file .env python -m evals.bench --servers mymodel=/path/to/model.gguf   # ad hoc, "--fit on" only
    uv run --env-file .env python -m evals.bench --only easy,agent --repeats 2
    uv run --env-file .env python -m evals.bench --only money,mail          # by tag
    uv run --env-file .env python -m evals.bench --report evals/runs/bench-A evals/runs/bench-B   # compare runs
    uv run --env-file .env python -m evals.bench --only medium,agent,multi,hard --task-timeout 900 --max-tokens 16000 \\
        --budget-min 90 --skip-done evals/runs/bench-A    # only what an earlier run did not finish, plus new tasks

A new model: add a SERVERS entry in mail_eval.py (gguf + args), then pass its name. Budget: each model stops
starting tasks after --budget-min (default 20) of task time, and no task runs past --task-timeout (default 240 s,
the point where a phone chat gives up anyway); skipped and timed-out tasks count as failed in the table.

Results: evals/runs/bench-<time>/ (results.jsonl, summary.md, transcripts/ with every request and tool call).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import json
import re
import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from evals import harness
from evals.mail_eval import PORT, SERVERS, LlamaServer
from evals.tasks import TASKS, TIERS, Task, check_all, make_world, tags_of
from evals.tasks.base import SYS_CODE, extract_code, run_tests
from evals.tasks.worlds import AGENTS, DELEGATE, SUB, Workspace
from franziska import gpu
from franziska.config import ROOT, Settings

DEFAULT_SERVERS = "gemma4-26b-a4b,qwen3.8-27b-gsq"
MAX_STEPS = 14
SUB_STEPS = 8  # a sub-agent's tool loop
FAST_S = 60  # "fast enough": a pass inside this many seconds
OLLAMA_V1 = "http://localhost:11434/v1"


# ---- model client -----------------------------------------------------------------------------------------------------
class Timeout(Exception):
    pass


@dataclasses.dataclass
class Meter:
    requests: int = 0
    prompt_n: int = 0
    prompt_ms: float = 0.0
    gen_n: int = 0
    gen_ms: float = 0.0
    truncated: bool = False


class Chat:
    def __init__(self, base: str, model: str, thinking: bool, ollama: bool, reasoning: str = "", key: str = ""):
        self.base, self.model, self.thinking, self.ollama = base, model, thinking, ollama
        self.cloud = bool(key)  # an OpenAI-compatible cloud endpoint: no chat-template kwargs, its own reasoning
        self.headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.reasoning = reasoning  # the template's reasoning_effort (SERVERS "reasoning"); '' = template default
        self.log: list[dict] = []
        self.shot_tokens: int | None = None  # --max-tokens: one answer, thinking included; None: the context window

    def ask(self, messages: list[dict], meter: Meter, deadline: float, tools: list[dict] | None = None,
            max_tokens: int | None = None) -> dict:
        left = deadline - time.time()
        if left < 5:
            raise Timeout
        body = {"model": self.model, "messages": messages, "chat_template_kwargs": {"enable_thinking": self.thinking}}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if self.cloud:
            del body["chat_template_kwargs"]
            if self.reasoning:
                body["reasoning_effort"] = self.reasoning
        elif self.ollama and not self.thinking:
            body["reasoning_effort"] = "none"
        elif self.reasoning and self.thinking:
            body["chat_template_kwargs"]["reasoning_effort"] = self.reasoning
        if tools:
            body["tools"] = tools
        try:
            r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=left, headers=self.headers).json()
        except httpx.TimeoutException as e:
            raise Timeout from e
        if "choices" not in r:
            raise RuntimeError(str(r)[:200])
        t, u = r.get("timings") or {}, r.get("usage") or {}
        meter.requests += 1
        meter.prompt_n += t.get("prompt_n", u.get("prompt_tokens", 0))
        meter.prompt_ms += t.get("prompt_ms", 0)
        meter.gen_n += t.get("predicted_n", u.get("completion_tokens", 0))
        meter.gen_ms += t.get("predicted_ms", 0)
        choice = r["choices"][0]
        meter.truncated |= choice.get("finish_reason") == "length"
        msg = choice["message"]
        self.log.append({"request": messages[-1], "reply": msg, "finish": choice.get("finish_reason")})
        return msg


def run_code_task(chat: Chat, task: Task, meter: Meter, deadline: float) -> tuple[float, str]:
    msg = chat.ask([{"role": "system", "content": SYS_CODE}, {"role": "user", "content": task.prompt}], meter, deadline,
                   max_tokens=chat.shot_tokens)
    text = re.sub(r"<think>.*?</think>", "", msg.get("content") or "", flags=re.S)
    if not text.strip():
        return 0.0, "empty answer" + (" (ran out of tokens while thinking)" if meter.truncated else "")
    score, why = run_tests(extract_code(text), task.tests, task.forbidden)
    if score < 1 and meter.truncated:
        why += " (a reply was cut at the token limit)"
    return score, why


def tool_loop(chat: Chat, system: str, user: str, tools: list[dict], call: Callable[[str, dict], str], meter: Meter,
              deadline: float, steps: int, tag: str = "", followups: tuple[str, ...] = (),
              on_turn: Callable[[int], None] | None = None) -> tuple[str, int, str, list[str]]:
    """One agent: ask, run the tool calls, repeat; then the next user message, if any (`steps` per user message).
    -> (last final reply, tool calls made, why it stopped early or '', the final reply of each user message)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    final, calls, why, replies = "", 0, "", []
    pending = list(followups)
    try:
        while True:
            for _ in range(steps):
                msg = chat.ask(messages, meter, deadline, tools, max_tokens=chat.shot_tokens and max(4096, chat.shot_tokens // 2))
                messages.append({"role": "assistant", "content": msg.get("content") or "", **(
                    {"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})})
                if not msg.get("tool_calls"):
                    final = msg.get("content") or ""
                    break
                for c in msg["tool_calls"]:
                    fn = c["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args, out = None, "error: arguments are not valid JSON"
                    if args is not None:
                        out = call(fn["name"], args if isinstance(args, dict) else {})
                        calls += 1
                    chat.log.append({"agent": tag, "tool": fn["name"], "args": fn.get("arguments"), "result": out[:500]})
                    messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out[:3000]})
            else:
                why = f"no final answer in {steps} steps; "
                break
            replies.append(final)
            if not pending:
                break
            messages.append({"role": "user", "content": pending.pop(0)})
            chat.log.append({"agent": tag, "user": messages[-1]["content"]})
            if on_turn:
                on_turn(len(replies))
    except Timeout:
        why = "timeout; "
    return final, calls, why, replies


def run_agent_task(chat: Chat, task: Task, meter: Meter, deadline: float) -> tuple[float, str]:
    ws = Workspace(task.files)
    world = make_world(task)
    agents = task.agents if task.agents is not None else (AGENTS if task.multi else {})
    if world is None:
        own, target = Workspace.TOOLS, ws.call
    else:
        names = task.tools if task.tools is not None else (() if agents else None)
        own, target = [t for t in world.TOOLS if names is None or t["function"]["name"] in names], world.call
    delegations = [0]
    # Worlds shaped like the bot (Home) say how it frames a message: the time it was sent before each user message
    # and each request to another assistant, and the assistant's own prompt for a sub-agent. Other worlds: as before.
    stamp = getattr(world, "STAMP", "")
    sub_system = getattr(world, "sub_system", None)

    def only(tools: list[dict], fn: Callable[[str, dict], str]) -> Callable[[str, dict], str]:
        """A tool the agent wasn't offered is an error, not a way around its own tool list."""
        offered = {t["function"]["name"] for t in tools}
        return lambda name, a: fn(name, a) if name in offered else f"error: unknown tool {name}"
    call = only(own, target)
    if agents:
        reach = DELEGATE if task.agents is None else world.agent_tool(task.call_tool, tuple(agents))
        key = "agent" if task.call_tool == "delegate" else "assistant"
        own_names = {t["function"]["name"] for t in own}

        def route(name: str, a: dict) -> str:
            if name in own_names:
                return target(name, a)
            agent = str(a.get(key, ""))
            if name != task.call_tool or agent not in agents:
                return f"error: {task.call_tool}({key}, request) with {key} one of {', '.join(agents)}"
            delegations[0] += 1
            tools = [t for t in world.TOOLS if t["function"]["name"] in agents[agent]]
            system = sub_system(agent, task.call_tool) if sub_system else SUB.format(name=agent)
            reply, _, stopped, _ = tool_loop(chat, system, stamp + str(a.get("request", "")), tools,
                                             only(tools, world.call), meter, deadline, SUB_STEPS, tag=agent)
            return (reply or "(no answer)") + (f" [{stopped.strip('; ')}]" if stopped else "")
        own, call = own + [reach], route
    main_tag = "orchestrator" if agents else ""

    def on_turn(i: int) -> None:
        if world is not None and hasattr(world, "turn"):
            world.turn = i
    final, calls, why, replies = tool_loop(chat, task.system, stamp + task.prompt, own, call, meter, deadline, MAX_STEPS,
                                           main_tag, [stamp + t for t in task.turns], on_turn)
    if world is not None:
        world.replies = replies + [""] * (1 + len(task.turns) - len(replies))
    if task.turns and len(replies) < 1 + len(task.turns):
        why += f"stopped before turn {len(replies) + 1} of {1 + len(task.turns)}; "
    if agents:
        sub_calls = sum(1 for e in chat.log if "tool" in e and e.get("agent") != main_tag)
        used = f"{delegations[0]} {'delegations' if task.call_tool == 'delegate' else 'asks'}, {sub_calls} sub-agent tool calls"
    else:
        used = f"{calls} tool calls"
    score, detail = task.judge(ws, final, world)  # the state at the point it stopped is still scored
    ws.cleanup()
    return score, f"{why}{detail} [{used}]".strip()


# ---- run --------------------------------------------------------------------------------------------------------------
def pick(only: str) -> list[Task]:
    want = {w for w in only.split(",") if w}
    known = {t.name for t in TASKS} | set(TIERS) | {g for t in TASKS for g in tags_of(t)}
    if unknown := want - known:
        raise SystemExit(f"--only: unknown {', '.join(sorted(unknown))} (see --list)")
    tasks = [t for t in TASKS if not want or t.name in want or t.tier in want or want & set(tags_of(t))]
    return sorted(tasks, key=lambda t: TIERS.index(t.tier))


def serve_spec(name: str) -> tuple[str, dict]:
    if "=" in name:
        label, path = name.split("=", 1)
        return label, {"gguf": str(Path(path).expanduser()), "args": ["--fit", "on"]}
    if name not in SERVERS:
        raise SystemExit(f"unknown server {name}; known: {', '.join(SERVERS)}")
    return name, SERVERS[name]


def finished(r: dict) -> bool:
    """A result that says something about the model: not skipped, not cut short by a limit or a server error."""
    return not r["skipped"] and not any(k in r["why"] for k in ("timeout", "ran out of tokens", "cut at the token limit", "error:"))


def merge(rows: list[dict]) -> list[dict]:
    """Later rows replace earlier ones for the same (model, task, rep): a rerun supersedes the cut-short result."""
    return list({(r["model"], r["task"], r["rep"]): r for r in rows}.values())


def run_model(label: str, spec: dict, args, tasks: list[Task], run_dir: Path) -> list[dict]:
    rows: list[dict] = []
    config = harness.config_id(spec, thinking=not args.no_thinking)
    todo = [t for t in tasks if (label, t.name) not in args.done]
    if len(todo) < len(tasks):
        print(f"  not rerun (finished in {', '.join(args.skip_done)}): {', '.join(t.name for t in tasks if t not in todo)}", flush=True)
    tasks = todo

    def go(chat: Chat) -> None:
        chat.shot_tokens = args.max_tokens or None
        t0 = time.time()
        for task in tasks:
            for rep in range(args.repeats):
                row = {"model": label, "task": task.name, "tier": task.tier, "rep": rep, "thinking": not args.no_thinking,
                       "budget": [args.max_tokens or None, args.task_timeout],  # evals.plan compares by inclusion
                       "harness": args.harness, "task_v": harness.task_version(task), "config": config}
                spent = time.time() - t0
                if spent > args.budget_min * 60:
                    row |= {"score": 0.0, "passed": False, "skipped": True, "why": "budget used up", "seconds": 0}
                else:
                    cap = min(task.timeout_s or args.task_timeout, args.budget_min * 60 - spent + 30)
                    chat.log, meter, start = [], Meter(), time.time()
                    try:
                        score, why = (run_agent_task if task.judge else run_code_task)(chat, task, meter, start + cap)
                    except Timeout:
                        score, why = 0.0, f"timeout after {cap:.0f} s"
                    except Exception as e:  # a broken response must not end the whole run
                        score, why = 0.0, f"error: {type(e).__name__}: {str(e)[:150]}"
                    row |= {"score": round(score, 2), "passed": score >= 0.999, "skipped": False, "why": why,
                            "seconds": round(time.time() - start, 1), "requests": meter.requests,
                            "gen_tokens": meter.gen_n, "prompt_tokens": meter.prompt_n,
                            "gen_tps": round(meter.gen_n / (meter.gen_ms / 1000), 1) if meter.gen_ms else None,
                            "prompt_tps": round(meter.prompt_n / (meter.prompt_ms / 1000)) if meter.prompt_ms else None,
                            "truncated": meter.truncated}
                    if price := spec.get("price"):
                        row["cost_usd"] = round((meter.prompt_n * price[0] + meter.gen_n * price[1]) / 1e6, 5)
                    (run_dir / "transcripts").mkdir(exist_ok=True)
                    (run_dir / "transcripts" / f"{label}-{task.name}-{rep}.json").write_text(json.dumps(chat.log, indent=1, default=str))
                rows.append(row)
                with (run_dir / "results.jsonl").open("a") as f:
                    f.write(json.dumps(row) + "\n")
                mark = "SKIP" if row["skipped"] else ("pass" if row["passed"] else f"{row['score']:.2f}")
                print(f"  {task.name:18} {mark:5} {row['seconds']:6.1f}s  {row['why'][:90]}", flush=True)

    if spec.get("live"):
        go(Chat(Settings.from_env().base_url or OLLAMA_V1, "live", not args.no_thinking, False))
    elif "cloud" in spec:  # 'deepinfra:<id>' (llm.build_cloud_model's form), no GPU
        provider, _, name = spec["cloud"].partition(":")
        if provider != "deepinfra" or not os.environ.get("DEEPINFRA_API_KEY"):
            raise RuntimeError(f"{spec['cloud']}: only deepinfra, with DEEPINFRA_API_KEY set")
        from franziska.llm import DEEPINFRA_URL

        go(Chat(DEEPINFRA_URL, name, not args.no_thinking, False, spec.get("reasoning", ""),
                key=os.environ["DEEPINFRA_API_KEY"]))
    elif "claude" in spec:  # the subscription, through Claude Code (evals/claude_chat.py), no GPU
        from evals.claude_chat import ClaudeChat

        go(ClaudeChat(spec["claude"], spec.get("effort", "")))
    elif "ollama" in spec:
        go(Chat(OLLAMA_V1, spec["ollama"], not args.no_thinking, True))
    else:
        with LlamaServer(label, spec, run_dir):
            go(Chat(f"http://localhost:{PORT}/v1", label, not args.no_thinking, False, spec.get("reasoning", "")))
    return rows


# ---- report -----------------------------------------------------------------------------------------------------------
def report(rows: list[dict]) -> str:
    models = list(dict.fromkeys(r["model"] for r in rows))
    tasks = list(dict.fromkeys(r["task"] for r in sorted(rows, key=lambda r: TIERS.index(r["tier"]))))

    def of(m, **kw):
        return [r for r in rows if r["model"] == m and all(r[k] == v for k, v in kw.items())]

    head = ["model"] + TIERS + ["score", f"pass ≤{FAST_S}s", "task time", "gen tok/s", "gen tokens"]
    lines = ["## Summary", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for m in models:
        cells = [f"{sum(r['passed'] for r in of(m, tier=t))}/{len(of(m, tier=t))}" for t in TIERS]
        done = [r for r in of(m) if not r["skipped"]]
        tps = [r["gen_tps"] for r in done if r.get("gen_tps")]
        lines.append("| " + " | ".join([
            m, *cells, f"{sum(r['score'] for r in of(m)):.1f}/{len(of(m))}",
            f"{sum(r['passed'] and r['seconds'] <= FAST_S for r in of(m))}/{len(of(m))}",
            f"{sum(r['seconds'] for r in done) / 60:.1f} min", f"{statistics.median(tps):.1f}" if tps else "-",
            str(sum(r.get("gen_tokens", 0) for r in done))]) + " |")
    by_name = {t.name: t for t in TASKS}
    areas = sorted({g for r in rows if r["task"] in by_name for g in tags_of(by_name[r["task"]])})
    if areas:
        lines += ["", "## By area (passed / run; a task counts in each of its tags)", "",
                  "| area | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
        for g in areas:
            cells = []
            for m in models:
                rs = [r for r in of(m) if r["task"] in by_name and g in tags_of(by_name[r["task"]])]
                cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "")
            lines.append(f"| {g} | " + " | ".join(cells) + " |")
    lines += ["", "## Per task (score, seconds; ✓ = all checks)", "", "| task | tier | " + " | ".join(models) + " |", "|---|---|" + "---|" * len(models)]
    for t in tasks:
        tier = next(r["tier"] for r in rows if r["task"] == t)
        cells = []
        for m in models:
            rs = of(m, task=t)
            cells.append(" ".join("–" if r["skipped"] else f"{'✓' if r['passed'] else r['score']} {r['seconds']:.0f}s" for r in rs) or "")
        lines.append(f"| {t} | {tier} | " + " | ".join(cells) + " |")
    lines += ["", "## What failed", ""]
    for r in rows:
        if not r["passed"]:
            lines.append(f"- **{r['model']} / {r['task']}** ({r['score']}): {r['why']}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--servers", default=DEFAULT_SERVERS, help="SERVERS names (mail_eval.py) or name=path.gguf, comma-separated")
    p.add_argument("--only", default="", help="task names, tiers (easy, medium, agent, multi, hard) or tags (see --list)")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--budget-min", type=float, default=20, help="task time per model before the rest is skipped")
    p.add_argument("--task-timeout", type=int, default=240)
    p.add_argument("--max-tokens", type=int, default=0,
                   help="cap one single-shot answer, thinking included (agent steps get half); 0: no cap, the server's "
                        "context window is the limit")
    p.add_argument("--run-dir", type=Path, help="write here; if it has results, resume: skip what's recorded")
    p.add_argument("--skip-done", nargs="+", default=[], metavar="RUN_DIR",
                   help="earlier runs: tasks a model finished there are not rerun (cut-short ones are), and the summary merges both")
    p.add_argument("--no-thinking", action="store_true")
    p.add_argument("--report", nargs="+", metavar="RUN_DIR", help="merge these runs into one comparison table and exit")
    p.add_argument("--dry-run", action="store_true", help="print which tasks each model would run, then exit")
    p.add_argument("--list", action="store_true", help="the tasks (of --only, default all) with tier and tags")
    p.add_argument("--check", action="store_true",
                   help="no model: each task's reference answer must pass its judge and the empty answer must not")
    args = p.parse_args()
    if args.list:
        tasks = pick(args.only)
        for t in tasks:
            print(f"{t.tier:7}{t.name:22}{t.what}  [{', '.join(tags_of(t))}]")
        counts = {tier: sum(t.tier == tier for t in tasks) for tier in TIERS}
        print(f"\n{len(tasks)} tasks: " + ", ".join(f"{n} {tier}" for tier, n in counts.items() if n))
        print("servers:", ", ".join(SERVERS))
        return
    if args.check:
        res = check_all(pick(args.only))
        bad = [(n, p) for n, p in res if p and p != ["no reference"]]
        for n, probs in res:
            print(f"{'ok ' if not probs else '-- ' if probs == ['no reference'] else 'BAD'} {n:22} {'; '.join(probs)}")
        print(f"\n{len(res) - len(bad)}/{len(res)} fine ({sum(p == ['no reference'] for _, p in res)} without a reference)")
        sys.exit(1 if bad else 0)
    if args.report:
        rows = [json.loads(l) for d in args.report for l in (Path(d) / "results.jsonl").read_text().splitlines()]
        print(report(merge(rows)))
        return
    tasks = pick(args.only)
    args.harness = harness.current("bench")
    prev = [json.loads(l) for d in args.skip_done for l in (Path(d) / "results.jsonl").read_text().splitlines()]
    args.done = {(r["model"], r["task"]) for r in merge(prev) if finished(r)}
    if args.run_dir and (args.run_dir / "results.jsonl").exists():
        # resuming an interrupted run (reboot, kill): everything already recorded there at this budget stays as it is,
        # including timeouts, so a resumed run gives the same table as an uninterrupted one. Rows of another budget
        # (the run dir reused after the limits changed) don't count: those tasks run again.
        budget = [args.max_tokens or None, args.task_timeout]
        args.done |= {(r["model"], r["task"]) for r in
                      (json.loads(line) for line in (args.run_dir / "results.jsonl").read_text().splitlines() if line.strip())
                      if r.get("budget") == budget and r.get("harness", args.harness) == args.harness}
    if args.dry_run:
        for name in args.servers.split(","):
            label = serve_spec(name)[0]
            print(f"{label}: " + ", ".join(t.name for t in tasks if (label, t.name) not in args.done))
        return
    run_dir = args.run_dir or ROOT / "evals" / "runs" / f"bench-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    local = any("cloud" not in SERVERS.get(n, {}) and "claude" not in SERVERS.get(n, {}) for n in args.servers.split(","))
    # the bot's local requests fail fast instead of loading next to the server; a cloud-only run needs no GPU
    with gpu.hold("model bench") if local else contextlib.nullcontext():
        for name in args.servers.split(","):
            label, spec = serve_spec(name)
            print(f"== {label}", flush=True)
            try:
                rows += run_model(label, spec, args, tasks, run_dir)
            except RuntimeError as e:
                print(f"  skipped: {e}", flush=True)
    table = report(merge(prev + rows)) if rows else "no results"
    (run_dir / "summary.md").write_text(table + "\n")
    print("\n" + table + f"\n\n{run_dir}")


if __name__ == "__main__":
    main()
