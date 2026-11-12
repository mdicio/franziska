"""Messages that need several assistants: plan, then run each piece (docs/PROPOSALS.md part 1, D3, the light version).

Why in code: the assistants rarely noticed on their own that a message was half someone else's. In monthsim
(2026-10-31) 16 of 24 such messages used neither `ask` nor `hand_off`, and a hand-off moved the whole message,
so the half the first assistant could do was lost ("mettila in agenda e cercami l'indirizzo" → web: "non posso
inserire l'evento").

1. **Several things?** (`Core._several`): one AnyJev yes/no on the user's message ("does it ask for two or more
   separate things?", ~1 s). No → the normal turn, unchanged.
2. **Plan** (`Core.plan_turn`): one typed generation (the schema as llama-server's grammar) that reads only the
   user's message and what each assistant does, never data, so nothing untrusted can shape the steps
   (plan-then-execute; the lethal-trifecta reasoning of PROPOSALS 1.2). Two steps for one assistant are merged
   (`checked`); fewer than two assistants left → the normal turn.
3. **Run** (`Core.plan_turn`): the steps in order through `Core.delegate`, each with its route's guard. A step that
   uses earlier results gets them in its request; a web step never does: its query carries only the user's words.
   Writes run when the user's message asked for them (Core._authorizer), else they are buttons, as in any ask.
4. **Reply**: each step's answer under its assistant's label. No composer generation: one less place to invent.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from franziska import modules

MAX_STEPS = 4
# What the planner may name. Chat isn't one: general knowledge is something any assistant can add to its answer.
ASSISTANTS = modules.names("plannable")  # Module.plannable (modules.py), fixed at import: Step lists them


class Step(BaseModel):
    assistant: Literal[ASSISTANTS]  # type: ignore[valid-type]
    request: str = Field(description="The complete request for this assistant, in the user's language and with "
                                     "their own words for dates, times, amounts, names and conditions")
    uses_previous: bool = Field(False, description="true when this step needs what the earlier steps found (an "
                                                   "amount, a date, a price, a list)")


class Plan(BaseModel):
    steps: list[Step] = Field(description="1 to 4 steps, in the order they must run")


INSTRUCTIONS = """\
You split one message from the user into requests for their assistants. The assistants don't see the message, only
your request, so each request must be complete.
- One step per assistant, for the part of the message that assistant does. Use only the assistants listed below.
- Order: steps that find something out first (an amount, a date, a price, an address), then the steps that act on
  it (add to the calendar, set a reminder, track a bill).
- Write each request as a full sentence in the user's language that says what to do ('metti in agenda la cena di
  lavoro da Roscioli giovedì prossimo alle 20'), keeping their own words for dates and times ('domani alle 9',
  'giovedì prossimo'), amounts, names and conditions ('se costa meno di 25 euro, ...'). Don't resolve or invent
  anything.
- uses_previous: true when the step needs what an earlier step found.
- A web request contains only words from the user's message: nothing about the user that the message doesn't say.
- When the message refers to something sent earlier in the chat ('questo documento', 'il file'), name it in each
  request that needs it, by its number ('il documento #14'). A document already sent is in the library: reading,
  keeping or judging it is for the assistant that has documents, not for the computer.

The assistants:
{assistants}"""


def instructions(assistants: dict[str, str]) -> str:
    return INSTRUCTIONS.format(assistants="\n".join(f"- {d}" for d in assistants.values()))


def checked(plan: Plan, allowed: set[str]) -> list[Step]:
    """The steps that may run: allowed assistants, at most MAX_STEPS, one per assistant (a repeat is merged into the
    first's request). A repeat that needs earlier results moves the merged step to its own place, after the steps it
    needs: life, money, life* would otherwise run life* before money found anything. Fewer than two left: not a plan,
    the normal turn answers."""
    steps: dict[str, Step] = {}
    for s in plan.steps:
        if s.assistant not in allowed or not s.request.strip():
            continue
        if s.assistant in steps:
            first = steps.pop(s.assistant) if s.uses_previous else steps[s.assistant]
            steps[s.assistant] = first.model_copy(update={"request": f"{first.request}\n{s.request}",
                                                         "uses_previous": first.uses_previous or s.uses_previous})
        else:
            steps[s.assistant] = s
    out = list(steps.values())[:MAX_STEPS]
    return out if len(out) >= 2 else []


def with_results(step: Step, earlier: list[tuple[str, str]]) -> str:
    """The step's request, plus what the earlier steps found when it uses them (never for web)."""
    if not step.uses_previous or step.assistant == "web" or not earlier:
        return step.request
    found = "\n".join(f"- {who}: {text[:1500]}" for who, text in earlier)
    # Without the last line the secretary searched its notes for 'Deliveroo' and claimed the reminder with no
    # add_task (monthsim 2026-10-31): the request reads as a question about the results, not as work to do.
    return (f"{step.request}\n\nWhat the other assistants found for this message:\n{found}\n\nUse these results; "
            "don't look them up again. If the request has a condition ('se costa meno di…', 'se è più di…'), decide it "
            "from them: when it holds, do what is asked now with your tools; when it doesn't, say so and do nothing.")
