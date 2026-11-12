# Roadmap

Rewritten 2026-11-01. Decisions taken: `DESIGN.md` (58). Investments: `INVEST.md`.

**Rule for the order: trust before coverage.** An assistant you can't rely on (it misses a reminder, invents a date,
loses data, forgets what you just said) gets replaced by a second system. So dependability comes before new areas.

## Where it stands (2026-11-01)

Routes: 📅 life, 💶 money (ledger, bills, DEGIRO portfolio), ✉️ mail and Google, 🌐 web, 🖥 PC, chat, plus workflows
(`mail_bills`, `categorise`, `markets`) and a planner for multi-part messages. Memory (profile, per-domain notes,
past conversations), a document library with word + vector search, a sandbox for `run_python` and coding jobs, a web UI,
an MCP server, voice notes, the market brief. Guards: loop, claim checks, taint, tiers, `/pause`.
Local model: gemma-4-26B on llama-server, 32k context.

What real use shows is failing: references across turns and routes (an earlier chat, an attachment, "this document").

## Next, in order

1. **Cross-turn context**: past conversations across routes with better matching, attachments that
   outlive their turn, referents passed to planned steps. Done 2026-11-01 and 2026-11-12: search across routes by
   words and meaning, documents kept in the library, the chat's thread in every run and planner step. A follow-up to
   a turn whose data the route's model may not see (life on Claude after money read a bill) runs on the local model,
   is logged as seen as that route (`turns.seen_as`) and stays out of the route's history; so does a plan step or an
   `ask` that would carry such data to Claude. A figure from the route's own earlier turns counts as backed. A
   merged plan step that needs earlier results runs after them. Accepted: on Claude, a reference to money or mail
   data past the 15-minute follow-up window finds nothing (the privacy table decides; relax it in `FRANZISKA_SEES`
   or keep that route local). Measured by `evals/xturn.py`.
2. **Measure with a simulated user**: a stronger model plays you; judge on state. Add every real miss. The first
   cases are `evals/xturn.py`.
3. **Typed money answers and delegation**: `check_payment`, then life ↔ money asks, the full planner.
4. **Real-data checks of what exists**: the library on real documents, `run_python` and coding jobs for a week, a week
   of market reports.
5. **Always on** (`deploy/BOX.md`): PC off = no reminders or brief. Parked until you decide.

## Open source

Prepared for a public release in 2026-11: any bank's export imports through a saved column mapping, and
`python -m franziska init`, `doctor` and `demo` set it up, check it and show it on invented data.

Done 2026-11-12:
- **Modules, first cut**: every assistant is one `Module` in `franziska/modules.py` (router description, label, privacy
  class, documents, sandbox, cloud/Claude, asks, the setting that switches it on); the tables the rest reads are built
  from it. An assistant from outside the repo loads with `FRANZISKA_MODULES` (CONTRIBUTING.md). A module holding money
  or mail is refused a cloud model or Claude at start.
- **Bank-neutral routing**: money's description names no broker. Router eval on gemma-4-26B: 57/58 before and after,
  four new investment cases right at p=1.00, the one miss unchanged.
- A pre-push hook refuses the pre-public history anywhere but the private archive.

- **Guided setup in the web app** (phase 1, 2026-11-12): a first run opens on Setup (`web/onboarding.py`): the model
  and a time zone are needed, every other piece is an optional card that says what it adds, whether it works and how
  to turn it on. Telegram is checked and your id found from the page; the code sandbox and the services (for this
  folder, not `~/franziska`) install from it. Secrets are write-only. Tested as a stranger: a copy of the repo under
  an empty home, no .env.

Next, in order (what stops a stranger from using it, biggest first):
1. **One Google sign-in, from the Setup page.** Google stays optional: without it Franziska keeps its own calendar and
   the mail assistant is off; there is no replacement for Gmail. Gmail can't be reached without a Google Cloud app of
   the user's own (gog too: it has no app of its own, and a shared app would need Google's verification and, for
   Gmail, a yearly security audit). So the page makes that one-time part short: upload the downloaded JSON, one
   consent in the browser, and the token serves both Franziska and gog (`gog auth credentials set`, `gog auth import`).
   Today it is two terminal sign-ins.
2. **Reminders without Telegram**: the scheduler lives in the bot, which needs a token; a web-only install has no
   reminders. Run the jobs without Telegram and push to the installed web app (or ntfy).
3. **Modules, second cut**: an outside module's planner steps and background jobs; `/<module>` to pin a route.
4. **Hardware**: say plainly what works without a 12 GB GPU (Ollama and a small model, the cloud model per route) and
   measure the router there.
5. A short screen recording of the Telegram chat in the README.

## Later

**Dependable:** backups off this disk; fresh bank data (monthly export, later PSD2); calibrated routing (p is
saturated); context that degrades instead of failing (token-budgeted history, trimmed tool results).

**Channels:** the web UI exists; next in value are email-in (a `+assistant` alias) and phone notifications (ntfy).
Matrix only if Telegram's cloud becomes a problem. WhatsApp stays out (DESIGN §2).

**Life areas not covered**, each starting with a store and code reminders, the agent last:
1. Admin deadlines and documents (ID, licence, bollo, revisione, insurance, IMU/TARI/730), a `documents` store with
   expiries, reminders N weeks ahead.
2. Projects (a first cut exists: `project`, `minutes`, the Sunday review); an outcome and a date per project if used.
3. People (birthdays, last heard from, gift ideas). 4. Health (medication, refills; private like money).
5. Travel (bookings from mail → calendar). 6. Home (utilities, maintenance).

**Guardrails still open:** an audit row for taps and jobs (agent writes are in the turn log); budgets per run
(wall time, writes per run); privacy classes enforced in the cloud wrapper, not by convention; secrets never in a
store an agent can read. Done: taint, permission tiers, loop guard, claim checks, `/pause`, `/undo` for tasks.

**Bigger designs, not started**: entities (one layer linking the stores), one model server for
every model, Docling for tables. Each starts with an eval that decides whether it pays.

## Decided against, or superseded

- **A shell or a command allowlist for agents:** replaced by the PC route (structured tools, buttons for changes) and
  the sandbox (DESIGN decisions 44–47).
- **A ReAct orchestrator:** the planner fixes the plan from your message before anything untrusted is read (DESIGN 53).
- **A separate `invest` agent and `/perf`:** investments are answered by money.
- **A2A and MCP between agents:** events (`events.py`) for facts, typed `Core.delegate` calls for questions.
- **Vision on the main model:** off; costs ~11% prefill on every turn, tesseract reads the same photos.
- **A second runtime or Ollama as main:** one llama-server for everything (decision 19); Ollama stays the fallback.
