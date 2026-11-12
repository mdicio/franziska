"""markets: the market report as a PDF (franziska/market). A route, `/markets [weekly]` and a CLI; the bot also sends
it on weekdays at FRANZISKA_MARKET_AT (the weekly edition on Mondays).

    uv run --env-file .env python -m franziska.workflows markets "weekly"
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field

from franziska.market import pdf
from franziska.workflows import Ctx, Report, Workflow

log = logging.getLogger(__name__)


class Params(BaseModel):
    edition: Literal["daily", "weekly"] = Field(
        "daily", description="'weekly' for the weekly edition (the past week and the week ahead), else 'daily'")


def run(ctx: Ctx, params: Params) -> Report:
    market = ctx.market
    brief = market.build(ctx, params.edition)
    ctx.progress("making the PDF…")
    path = pdf.write_pdf(brief, market.reports_dir, market.lang, market.written_by(ctx.model), market.timezone)
    caption = pdf.caption(brief)
    if market.mail and ctx.gmail is not None:
        try:  # the PDF is on disk and in the chat either way; a mail that fails is said, not fatal
            ctx.gmail.send_to_self(f"📈 {brief.edition.capitalize()} market report, {brief.day:%a %d %b %Y}",
                                   caption, [path])
        except Exception as e:
            log.warning("markets: mailing the report failed: %s", e)
            caption += f"\n(not mailed: {type(e).__name__})"
    return Report(caption, files=[path])


WORKFLOW = Workflow(
    name="markets",
    title="📈 Markets",
    description=("markets: the market report, a PDF: how stock markets, rates, currencies and commodities moved, "
                 "market news and the week ahead; daily or the weekly edition. Only when they ask for the market "
                 "report or for market news"),
    params=Params,
    run=run,
    needs=("market",),
    scope="money",
)
