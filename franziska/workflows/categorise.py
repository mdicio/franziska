"""Spending groups for the payees that have none: 'categorizza le spese', /categorise.

1. pending   payees of spending with no category and no earlier decision, the most money first (categories.py).
             Known payees never get here: their new payments took the group on import
2. decide    one AnyJev choice per payee over the spending groups, in batches of 20 on the local model; each batch
             is saved as it comes, so a run stopped halfway resumes where it was
3. report    how many payees and how much money got a group, and the biggest guesses to look at. One button
             confirms every guess (they become the user's answers); a correction is a chat message
             ('Q8 is transport') or /uncat

The guesses are written as `proposed_category`, never `category`: that column stays the bank's facts and the
user's word. Nothing leaves the machine.
"""

from __future__ import annotations

from pydantic import BaseModel

from franziska.finance import categories
from franziska.workflows import Ctx, Proposal, Report, Workflow


class Params(BaseModel):
    pass


def run(ctx: Ctx, params: Params) -> Report:
    text = ctx.money.categorise(categories.decider(ctx.router), ctx.router.model, progress=ctx.progress)
    guesses = len(ctx.money.review(10_000))
    proposals = [Proposal(f"✅ Confirm all {guesses} guesses", ctx.money.confirm_guesses)] if guesses else []
    return Report(text, proposals)


WORKFLOW = Workflow(
    name="categorise",
    title="🏷️ Categorise",
    description="categorise: in bulk, give a spending group to all the bank payments that have none yet "
                "(categorise my expenses, categorizza le spese); not a single payee the user names",
    params=Params,
    run=run,
    needs=("money",),
    scope="money",
)
