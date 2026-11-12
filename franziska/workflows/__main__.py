"""Run a workflow from the terminal, on the bot's own services and local model (core.Core).

    uv run --env-file .env python -m franziska.workflows                       # list them
    uv run --env-file .env python -m franziska.workflows mail_bills "condominio, ultimi 60 giorni"

Proposals are listed, not applied.
"""

import dataclasses
import sys

import pydantic_ai

from franziska import obs
from franziska.config import Settings
from franziska.core import Core
from franziska.workflows import BY_NAME


def main() -> None:
    pydantic_ai.BANNER_ENABLED = False
    if len(sys.argv) < 2 or sys.argv[1] not in BY_NAME:
        sys.exit("\n".join(f"{w.name}: {w.description.split(': ', 1)[1]}" for w in BY_NAME.values()))
    settings = Settings.from_env()
    obs.setup("cli", settings.traces_db, settings.otlp)
    with obs.root(f"cli {sys.argv[1]}", sys.argv[1]):
        run(Core(settings))


def run(core: Core) -> None:
    wf = core.workflows.get(sys.argv[1])
    if wf is None:
        wf = BY_NAME[sys.argv[1]]
        sys.exit(f"{wf.name} needs the router (FRANZISKA_ROUTER=1)"
                 + (f" and {', '.join(wf.needs)} (Google: FRANZISKA_GOOGLE=1 and a sign-in)." if wf.needs else "."))
    ctx = dataclasses.replace(core.ctx, model=core.models.for_route(wf.name), progress=print)
    params = wf.parse(ctx, " ".join(sys.argv[2:]))
    print(f"{wf.name} {params.model_dump()}")
    report = wf.run(ctx, params)
    print(report.text)
    for p in report.proposals:
        print(f"[proposal] {p.label}")


if __name__ == "__main__":
    main()
