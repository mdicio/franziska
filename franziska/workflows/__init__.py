"""Workflows: fixed multi-step jobs (search → decide → read → extract → report) that a chat turn can start.

A workflow is plain Python over the services (Life, Money, Gmail) plus two kinds of model calls:
- decisions: AnyJev questions on the local model (`ctx.router.decide`): classify, filter, choose. Typed, with a
  probability, no text to parse.
- generation: one Pydantic AI call with a typed output (`ctx.extract`): fields out of a document, never free text
  that the code then trusts.
It returns a Report: the text to show and Proposals, i.e. writes the user confirms with a button. A workflow never
writes on its own.

To add one: a module with a `WORKFLOW = Workflow(...)`, listed in ALL below. The router picks it by its
description, the local model fills its Params from the message, and it is also a /command and a CLI:

    uv run python -m franziska.workflows mail_bills "bollette del condominio dell'ultimo mese"
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput

from franziska.llm import USAGE_LIMITS


@dataclass(frozen=True)
class Proposal:
    label: str  # the button, e.g. '➕ Pay Condominio 864.00 € by Fri 14 Nov'
    apply: Callable[[], str]  # runs on tap; returns the confirmation line


@dataclass
class Report:
    text: str
    proposals: list[Proposal] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)  # sent with the text (a PDF); the text is then its caption
    notes: str = ""  # how it got there (what it searched, what it set aside): not shown, given to whoever follows up


@dataclass
class Ctx:
    """What a workflow may use. Services are None when not configured (no Google sign-in, no ledger)."""

    model: object  # the Pydantic AI model (local Ollama)
    router: object  # franziska.router.Router: AnyJev decisions on the same model
    now: Callable[[], datetime]
    gmail: object = None
    life: object = None
    money: object = None
    # franziska.memory.MemoryStore: read the profile by its well-known names, write the `mail`/`life` cache in code;
    # a profile fact found in the data is a Proposal, like any other write.
    memory: object = None
    market: object = None  # franziska.market.brief.Market: the market report (closes, news, the portfolio overlay)
    library: object = None  # franziska.library.Library: the bills' attachments are kept in it (class 'mail')
    mail: object = None  # franziska.mail.client.MailStore: headers the triage job already classified (None: no triage)
    progress: Callable[[str], None] = lambda text: None  # a status line for the user ('reading 3 of 12…')
    # The bot's event loop, when a turn started the run (Core.workflow_turn). The model's HTTP client keeps pooled
    # connections bound to the loop that used them, so a run_sync in the workflow's thread (a loop of its own) fails
    # after any chat turn ("bound to a different event loop", evals/live.py 2026-10-28). Calls go back to this loop.
    loop: asyncio.AbstractEventLoop | None = None

    def extract[T: BaseModel](self, output: type[T], instructions: str, text: str, native: bool = False,
                              timeout_s: float | None = None, max_tokens: int | None = None, retries: int = 2) -> T:
        """One typed generation on the local model: `text` in, an `output` instance out. `native`: the output's JSON
        schema as the request's response_format, which llama-server turns into a grammar, so a large nested output
        always parses (the market report's: a 26B wrote `{holdings:[{point:'…` as a tool call, 2026-10-28).
        `timeout_s`, `max_tokens`: this request's (the defaults are FRANZISKA_TIMEOUT_S and llm.MAX_TOKENS, which is
        no cap). `retries`: re-asks after an output that doesn't validate;
        each is a whole request, so a caller on a time budget sets fewer."""
        agent = Agent(output_type=NativeOutput(output) if native else output, instructions=instructions, retries=retries,
                      name=f"extract {output.__name__}")
        settings = {k: v for k, v in (("timeout", timeout_s), ("max_tokens", max_tokens)) if v} or None
        run = agent.run(text, model=self.model, usage_limits=USAGE_LIMITS, model_settings=settings)
        if timeout_s:  # also a wall-clock cap on the whole run, retries and fallback included: the request timeout is
            # httpx's read timeout, which a queued OpenRouter request resets with keep-alives (7 min, 2026-10-30)
            run = asyncio.wait_for(run, timeout_s + 5)
        if self.loop:
            return asyncio.run_coroutine_threadsafe(run, self.loop).result().output
        return asyncio.run(run).output


@dataclass(frozen=True)
class Workflow:
    name: str  # the route key and the /command: lowercase letters and underscores
    title: str  # the label on buttons, e.g. '🧾 Mail bills'
    description: str  # what the router reads, phrased as 'name: what it does, when to use it'
    params: type[BaseModel]  # filled from the user's message by the local model; defaults must be sensible
    run: Callable[[Ctx, BaseModel], Report]  # synchronous: the bot runs it in a thread
    needs: tuple[str, ...] = ()  # Ctx fields that must be set, e.g. ('gmail',)
    scope: str | None = None  # the memory scope its data belongs to: that route's past_conversations sees its runs

    def available(self, ctx: Ctx) -> bool:
        return all(getattr(ctx, n) is not None for n in self.needs)

    def parse(self, ctx: Ctx, message: str) -> BaseModel:
        """The params for this run. An empty message (a bare /command) takes the defaults without a model call."""
        if not message.strip():
            return self.params()
        return ctx.extract(self.params, f"Fill the parameters of the job '{self.name}' ({self.description}) from the "
                                        f"user's message. Leave a field at its default unless the message sets it. "
                                        f"Today is {ctx.now():%A %Y-%m-%d}.", message)


def _all() -> list[Workflow]:
    from franziska.workflows import categorise, mail_bills, markets

    return [mail_bills.WORKFLOW, categorise.WORKFLOW, markets.WORKFLOW]


ALL: list[Workflow] = _all()
BY_NAME = {w.name: w for w in ALL}
