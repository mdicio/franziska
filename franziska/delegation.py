"""Assistants that pass work to each other (docs/PROPOSALS.md part 1, DESIGN decisions 36–38).

Two tools, built per run by `Core` and never shown to the assistant they would call:
- `hand_off(assistant, why)`: the message is for another assistant. The run stops (HandOff, raised like the guard's
  Stopped), and the core answers the same message on that route instead, once: the second run has no hand_off, so two
  assistants can't pass a message back and forth. Not in a pinned conversation (the user chose the assistant), and a
  message that carries a document goes only to the routes that may read one (core.DOCUMENT_ROUTES).
- `ask(assistant, request)`: a piece of the work needs another assistant. `Core.delegate` runs it on the request alone
  (no conversation history, its own usage limit) and its answer comes back to the caller, which carries on. Depth 1
  (the callee has neither tool), at most MAX_ASKS per run. The callee runs read-only: its writes come back as buttons
  on the caller's reply, except a write the user's own message asked for (Core._authorizer: "ricordami domani alle
  10 di…" asked of life by money runs). An answer that read mail, a page or a document taints the caller, so the
  caller's own writes after it are buttons too, with the same exception (guards.py).

Who may ask whom is ASK, here in code rather than in a prompt.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic_ai.toolsets import FunctionToolset

from franziska import modules, workflows

# Caller → the assistants it may ask. Not life → money yet: the secretary never reads the ledger (DESIGN decision 6)
# until money has typed answers to give it (check_payment, FRANZISKA.md F1). Nobody asks web: a request an agent
# writes can carry personal data into a search query. Chat and web hand off instead.
ASK: dict[str, tuple[str, ...]] = {m.name: m.asks for m in modules.BUILTIN if m.asks}  # Module.asks
MAX_ASKS = 2  # per run
ANSWER_CHARS = 4000  # of a callee's answer given back to the caller


class HandOff(Exception):
    """An assistant passed the message on: `route` answers it instead."""

    def __init__(self, route: str, why: str):
        super().__init__(f"hand off to {route}: {why}")
        self.route, self.why = route, why


@dataclass
class StepResult:
    """What one assistant did for another (Core.delegate)."""

    route: str
    request: str
    text: str  # its answer
    sources: list[str] = field(default_factory=list)  # its tool results: evidence for the caller's reply checks
    proposals: list[workflows.Proposal] = field(default_factory=list)  # its writes, waiting for the user's tap
    wrote: list[str] = field(default_factory=list)  # the writes it did (the user asked for them): the tools' lines
    tainted: bool = False  # it read mail, a web page or a document
    stopped: str | None = None  # a guard's reason, 'busy' or 'error: <type>'
    tools: list[dict] = field(default_factory=list)  # its calls, for the turn log
    ms: int = 0
    tokens: int | None = None

    def for_caller(self) -> str:
        text = self.text if len(self.text) <= ANSWER_CHARS else self.text[:ANSWER_CHARS] + "…"
        if self.stopped:
            text = f"(it couldn't finish: {self.stopped})\n{text}"
        if self.wrote:
            text += "\n(Done: " + "; ".join(self.wrote) + ")"
        if self.proposals:
            text += ("\n(Waiting for the user's tap, not done: " + "; ".join(p.label for p in self.proposals)
                     + ". They get the buttons with your reply.)")
        return text


def _target(routes: dict[str, str]):
    return Literal[tuple(routes)]  # one of the keys, so the model can't name an assistant that isn't there


def _listed(routes: dict[str, str]) -> str:
    return "\n".join(f"- {description}" for description in routes.values())


def handoff_toolset(routes: dict[str, str], refuse: Callable[[], str | None] = lambda: None) -> FunctionToolset:
    """`hand_off` to one of `routes` (name → the router's description of it). `refuse()`: why the run may no longer
    hand off (it already changed something), or None."""

    async def hand_off(assistant: _target(routes), why: str) -> str:
        if reason := refuse():
            return reason
        raise HandOff(assistant, why)

    hand_off.__doc__ = f"""The user's message is for another assistant: call this instead of answering, and that
    assistant gets the message as it is. Only when the request clearly belongs to one of them, never for something
    your own tools can do, and never after you changed anything this turn. The other assistants:
{_listed(routes)}

    Args:
        assistant: who should answer.
        why: a few words, for the log.
    """
    toolset = FunctionToolset()
    toolset.add_function(hand_off)
    return toolset


def ask_toolset(routes: dict[str, str], ask: Callable[[str, str], Awaitable[str]]) -> FunctionToolset:
    """`ask` one of `routes`; `ask(route, request)` runs it (Core.delegate) and returns its answer."""

    async def ask_assistant(assistant: _target(routes), request: str) -> str:
        return await ask(assistant, request)

    ask_assistant.__doc__ = f"""Ask another assistant for a piece of the work your tools can't do (an amount or date
    that is in an email, what is planned on a day), then carry on with its answer. It doesn't see this conversation:
    write a complete request (who, what, which period). It saves only what the user asked for in their message; any
    other change comes back as a button for the user. Its answer says which. The assistants you can ask:
{_listed(routes)}

    Args:
        assistant: who to ask.
        request: the complete request, in the user's language.
    """
    toolset = FunctionToolset()
    toolset.add_function(ask_assistant, name="ask")
    return toolset
