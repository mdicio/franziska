# Contributing

Thanks for looking. Franziska is a personal assistant that people trust with their own data, so the bar is
dependability over features: a reminder that fires, a figure that matches the bank, a write that never happens
behind your back.

## Setup

```bash
uv sync
git config core.hooksPath .githooks   # refuses secrets, data files and figures from your own ledger
uv run pytest                         # offline: scripted models, no GPU
```

Your data and secrets live outside the repo (`~/.local/share/franziska`, `~/.config/franziska`). Never commit an
export, a database or a real figure; the hook catches most of it, but read your diff.

## How changes are checked

- **Run the real thing.** Most changes are verified on the bot or the CLI with the local model, not with unit tests.
- **Tests are few and broad**: a whole flow end to end (a Telegram message → router → agent → store → reply), with
  scripted models (`tests/`). Please don't add small unit tests for a function.
- **Evals measure the bot on the real model**: `evals/week.py` (a simulated week), `evals/router_eval.py`,
  `evals/bench.py` and others (`evals/README.md`). A change to routing, prompts or a guard should come with a run.

## Adding things

**Your bank.** First try sending its export to the bot: an unknown statement is mapped once (`finance/mapped.py`)
and saved as a profile in `<data dir>/banks/`. If that works, you're done; you can share the profile JSON. Write a
parser in `finance/importer.py` only when the export needs more than a column mapping (stated balances to check,
several sections, pending rows), and give it the same checks: a file that doesn't add up is refused, never imported
in part.

**A workflow** (a fixed multi-step job: search → decide → read → extract → report): a module in
`franziska/workflows/` with a `WORKFLOW = Workflow(...)`, listed in `ALL`. It becomes a route, a `/command` and a CLI.
Decisions are AnyJev questions, generation is a typed extraction, and writes are proposals the user taps
(`workflows/__init__.py`).

**A new assistant (route)** is one `Module` in `franziska/modules.py`: its description for the router, its label,
the class of data it holds and the classes it may see, whether it gets documents, the sandbox, a cloud model or
Claude, whom it may ask, and the setting that switches it on. Decide the privacy class before anything else: what it
may read, and what may read it. A route that touches the internet sees public data only, and a module that holds
money or mail is refused a cloud model or Claude at start. Then build its agent in `Core._agent`.

**An assistant of your own, outside this repo** (a private one, or one you publish separately): a Python module with
a `MODULE = Module(..., build=build)`, where `build(core, text, said)` returns its pydantic-ai agent and that agent's
deps for one message. Put its folder on `PYTHONPATH` and name it in `FRANZISKA_MODULES=mypkg.garden`; Core loads it
at start, and the router, the privacy table, hand-offs and the labels pick it up. Not yet: planner steps and
background jobs for an outside module.

```python
from pydantic_ai import Agent
from franziska.modules import Module

agent = Agent(name="garden", output_type=str, instructions="You look after the user's plants.")
MODULE = Module("garden", "🌱 Garden", "garden: the user's plants: watering, repotting, what to plant when",
                holds="personal", sees=("public", "personal"), build=lambda core, text, said: (agent, None))
```

## Style

- Python 3.12, line length 120 (`ruff`).
- Deterministic first: if code can compute it, the model doesn't.
- Comments say why, with the measurement that decided it when there is one.
- Decisions that shape the design go in `docs/DESIGN.md` as a numbered decision.
