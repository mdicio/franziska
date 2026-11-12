# Evals

Two kinds, with different jobs:

- **Model comparison** (which model, which settings): `bench`, `mail_eval`, `router_eval`, the speed bench, all
  driven by `funnel` (cheap stages first, expensive ones for the survivors) and, for a planned series of runs,
  `campaign`. Same tasks on every model, scored automatically, results kept per model so a new one only adds rows.
- **The bot itself** (does Franziska work): `week`, `memory`,
  `library`, `sandbox`. The real router, agents and guards, judged on the stores afterwards. The author also runs
  longer evals on a copy of their own data (a real week and month, `realweek`, `realmonth`, `monthsim`); those stay
  private with their reports, and code comments cite their findings by name and date.

This page is mostly about the first kind, and about adding tasks to `bench`.

## Running

```bash
uv run --env-file .env python -m evals.bench --list              # every task: tier, what it tests, tags
uv run --env-file .env python -m evals.bench --check             # judges vs reference answers; no model, ~20 s
uv run --env-file .env python -m evals.funnel --models NAME      # a new model through every stage, vs the baseline
uv run --env-file .env python -m evals.funnel --resume evals/runs/funnel-... --full   # survivors' full runs
uv run --env-file .env python -m evals.campaign --plan           # a planned series (edit STEPS), reboot-safe
uv run python -m evals.plan --models A,B --tasks wide-main        # what a wide sweep would run; done pairs skipped
uv run python -m evals.plan --coverage long                       # model × area, every run at one budget
uv run python -m evals.harness                                    # harness versions; --same-as "why" after a no-op change
uv run --env-file .env python -m evals.reference --estimate       # cloud reference run (DeepInfra): cost, then run
uv run --env-file .env python -m evals.reference --compare        # reference vs local: harness suspects, model gaps
uv run --env-file .env python -m evals.bench --servers NAME --only money,mail   # one model, some tags
uv run --env-file .env python -m evals.bench --report RUN_DIR [RUN_DIR ...]     # one table from several runs
```

Rules every eval driver follows:

- **One model at a time on the GPU.** The bot's server (`franziska-llm`) is stopped for the run and started again
  after, also on Ctrl-C. `gpu.hold` makes the bot's own local requests fail fast meanwhile.
- **Resumable.** `--run-dir` (bench, mail_eval, router_eval) writes `results.jsonl` row by row and skips what is
  already there; `funnel --resume` and `campaign` skip finished stages and steps. A reboot loses one task.
- **Bounded.** Every task has a timeout, every model a budget, every subprocess a wall-clock limit; what is cut
  short is recorded as such (`timeout`, `budget used up`), never left hanging (see `watchdog.sh`).
- **Signal, not verdict.** The tables say what passed and how fast; deciding what that means is the user's call.
- A new model is a `SERVERS` entry in `mail_eval.py` (gguf + llama-server args, its recommended sampling), plus a
  role in `funnel.CANDIDATES` (`main`, `small`, `speed`).

**Apples to apples.** Every row carries `harness` (the version of the runner, worlds, tools and, for mail, the bot's
mail agent: `evals/harnesses.json`), `task_v` (the task's own request, judge and reference), `config` (how the model
was served: `evals/configs.jsonl`) and `budget` (token cap, seconds). `evals.plan` only puts rows of the current
versions side by side and never reruns a pair already settled under them. Changing a harness file registers a new
version on the next run (printed); if the change can't move results, `python -m evals.harness --same-as "why"`.

Results: `evals/runs/<eval>-<time>/` with `results.jsonl` (one row per task and repeat), `summary.md`, and
`transcripts/` (every request, reply and tool call). Write-ups go in `evals/reports/<date>/`.

## Bench tasks

`evals/tasks/`, one module per kind:

| module | kind | judged on |
|---|---|---|
| `code.py` | single shot: one answer, its last ```` ```python ```` block | hidden checks (`check(name, fn)`) |
| `workspace.py` | coding agent: list / read / write / run python in a scratch dir | the files afterwards, visible + hidden tests |
| `assistant.py` | one assistant with schedule, notes, money or mail tools | the world afterwards + the replies |
| `multi.py` | an orchestrator that can only `delegate`, or a peer that `ask`s another assistant | the world afterwards + the replies |

Worlds: `worlds.py` has the 2026-10 ones (`Workspace`, `Life`, `Office`), kept unchanged so their tasks stay
comparable. **New tasks use `Home` (`home.py`)**: the bot's tool names, arguments and guards (past times refused,
unknown ids are errors, mail bodies wrapped as untrusted content, drafts never sent), a fixed "now" (Tue 2026-11-10
19:15), and a consistent life: calendar, tasks, notes, three months of movements, budgets, bills, an inbox with a
phishing mail and a prompt injection. Its docstring lists who is who. `LIFE`, `MONEY`, `MAIL` are the tool sets of
the bot's three assistants; `ROUTES` maps them.

**Tiers** are the run order and the report columns: `easy` (one function, or one or two tool calls: what a small
model must get right), `medium` (single-shot with many edge cases), `agent` (a tool loop with a rule, a condition,
an ambiguity, a guard or a follow-up), `multi`, `hard` (long parsers, a feature in a codebase, many-step work).
**Tags** say what a task exercises, and `--only` and the report's "By area" table use them: `life money mail notes
code workspace` (area), `read` (no writes expected), `ask` (should ask the user), `guard` (a rule to respect),
`multi-turn`, `ask-peer`, `retry`, `it` / `en` (the language of the request).

### Adding a task

1. Pick the module and the world. Write the request the way the user would type it (Italian unless the point is
   English), with nothing in it that a real message wouldn't have.
2. Write the judge: `(workspace, final_reply, world) -> (score, why)`, usually `_score({"check name": bool, ...})`.
   Judge the **state** first (what was added, moved, saved, drafted; `world.changed()` and `world.writes` list
   everything that differs), the reply only for what has to be said (a figure: `amount_in`; a name: `said`).
   Accept every right answer, not only yours: an appointment *or* a reminder when both are fine, asking *or* acting
   with a warning when both are fine. Check "nothing else changed" when doing too much is a real failure.
3. Write a `reference`: the solution code (single shot), the files (workspace), or a function that makes the right
   tool calls on the world and returns the reply (agent; a list of replies, one per user turn, when `turns` is set;
   set `world.turn` before the calls of a later turn).
4. Add it to the module's `NEW` list with a tier, a one-line `what`, and tags. Then
   `python -m evals.bench --check --only NAME`: the reference must score 1.0 and the empty answer less. Also try
   one or two plausible wrong answers by hand: each must fail for the reason the check name says.
5. Run it on the baseline model before trusting it: a task every model passes or every model fails says nothing.

```python
def j_gate_code(ws, final, w: Home):
    return _score({"says 4512": "4512" in final, "no writes": not w.writes})

Task("gate_code", "easy", "recall a saved fact", "qual è il codice del cancello?", judge=j_gate_code,
     world=Home, tools=LIFE, system=LIFE_SYS, tags=("notes", "it", "read"),
     reference=lambda w: "Il codice del cancello è 4512.")
```

Fields beyond the basics (`evals/tasks/base.py`): `turns` (follow-up user messages), `agents` (sub-agent name →
tools) with `call_tool` `"delegate"` (the main agent has only that tool) or `"ask"` (it also has `tools` of its own),
`world=lambda: Home(flaky={"add_appointment": 1})` (the first call fails, nothing saved: is it retried, once?),
`timeout_s` (a task that needs more than `--task-timeout`).

Changing a task (its request, tools, judge or reference) changes its `task_v`: `evals.plan` stops counting that
task's earlier rows, on every model, and the next sweep reruns only it. Rows from before versioning are matched
against the task versions they ran (`task_v` in the bench-v1 entry of harnesses.json). Fix a judge that rejects a
right answer this way; for a task that should ask something different, prefer a new name.
