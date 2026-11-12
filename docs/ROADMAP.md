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
   outlive their turn, referents passed to planned steps.
2. **Measure with a simulated user**: a stronger model plays you; judge on state. Add every real miss.
3. **Typed money answers and delegation**: `check_payment`, then life ↔ money asks, the full planner.
4. **Real-data checks of what exists**: the library on real documents, `run_python` and coding jobs for a week, a week
   of market reports.
5. **Always on** (`deploy/BOX.md`): PC off = no reminders or brief. Parked until you decide.

## Open source

Prepared for a public release in 2026-11: any bank's export imports through a saved column mapping, and
`python -m franziska init`, `doctor` and `demo` set it up, check it and show it on invented data. Next:
1. **Modules**: a module declares its privacy class, routes and jobs, and Core builds the routes from the installed
   modules instead of fixed wiring, so a new area plugs in without touching Core.
2. **Bank-neutral routing**: the money route's description still names one broker; make it generic and re-run the
   routing eval.
3. A short screen recording of the Telegram chat in the README.

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
