# Personal assistants: findings and design plan

> References to `PROPOSALS.md`, `CHANGELOG.md`, `TODO.md` and `FRANZISKA.md` (and to the `realweek`, `realmonth`,
> `monthsim`, `handoff` and `live` evals) point to the private development notebook and the evals run on the
> author's own data, which are not in this repository; they are kept as a record of where a decision came from.

Status as of 2026-10-25 (second pass: secretary, MCP, AnyJev router, pi; third pass: finance to the same level, and the
link between the two; fourth pass: always-on box + desktop GPU on demand, Google Calendar and Gmail groundwork). Covers both projects: **finance** (expenses, income, savings, investments) and
**secretary** (life tasks and reminders), both reached through a chat app.
Code lives in this repository.

## 1. What exists and was verified

Project (uv, Python 3.12, Pydantic AI 2.61, Ollama `gemma4:12b`, python-telegram-bot 22.8,
mcp 2.3, AnyJev 0.3.0 @ 35311c5, pi 1.4.0 on Node 24.22.1 in `~/.local/opt`). Architecture and diagrams:
`ARCHITECTURE.md` (this file keeps the decisions and the reasons behind them).

**Finance** (first pass): `.xlsx` importer, SQLite ledger in cents, read-only SQL tool behind an
authorizer, "no numbers without a query" validator. 808 movements imported; figures matched SQL to the cent.

**Finance** (third pass, mirrors the secretary):
- `finance/merchants.py`: the counterparty parsed out of the bank's six text shapes (POS old/new, Bancomat Pay, direct
  debit, transfer in/out, ATM), normalised (no legal suffix, no per-payment codes). 808/808 rows get one. Stored in a
  new `merchant` column (old ledgers migrate on open) and exposed in the `tx` view.
- `finance/categories.py` (v3, 2026-10-28; replaced ~50 substring rules that left 63% of spending uncategorised):
  11 broad spending groups decided per payee, never per payment. `category` is authoritative (bank facts: salary,
  interest, ATM; cash entries; the user's word), `proposed_category` is the local model's guess (one AnyJev choice
  per payee, in batches: `Money.categorise`, the `categorise` workflow). Reports count the guess; a correction
  ('Q8 is transport', `/uncat`) applies to the payee's past and future payments. Measured: 49/50 against the old
  rules' labels, ~2 s per payee; the probability saturates near 1, so review is ordered by money, not by p.
- `finance/service.py` (`Money`): month summary (in/out/net/savings rate, vs 3-month average, top categories),
  budgets (`budgets` table, 🟢🟠🔴, share of month gone), recurring charges (gaps of ~1-3 months with at least two
  monthly ones; a mixed payee like Apple is split by exact amount), price-change alert on import (> 15%), data
  freshness, bills (below), brief lines. All SQL + arithmetic, no model.
- `finance/agent.py`: 8 tools as thin wrappers; the grounding validator now accepts any tool result (it used to
  reject "Recorded 3.50 €" because only `query_ledger` counted). The categories in use are in the instructions.
- `bot.py`: `/month [YYYY-MM]` `/budget` `/subs` `/bills` `/categorise` `/uncat` (the model's guess for the biggest
  unconfirmed payee, first of the group buttons), money lines in the brief, the monthly summary job on the 1st, imports report bills settled.

**Integration** (finance → secretary, never the other way):
- A bill (`add_bill`) is a row in `ledger.bills` (payee, match text, amount) plus a task "Pay <payee>" in
  `life.sqlite` via `Life.new_task` (due 18:00 on the date, reminder N days before at 09:00, `recur`). The amount is
  not in the task, so the `life` MCP server and `/think` never see it.
- `Money.reconcile()` (after every import and on `add_bill`): a debit containing the match text, within 20% of the
  amount, from 25 days before to 15 after the due date, not before the bill existed (−7 days), and not already used
  (`bill_payments.tx_id` is the key) completes the task; a recurring one moves on.
- The brief: `Life.brief()` + `Money.brief_lines()` (expected charges in the next 7 days, budget warnings, stale
  data). `FRANZISKA_BRIEF_MONEY=0` disables it and the monthly summary.

**Secretary** (second pass):
- `secretary/tasks.py`: tasks and appointments in one table (`kind`), one reminder each, `recur`
  daily/weekly/monthly/yearly. A recurring task moves on when completed; a recurring event moves once started; a
  reminder left unanswered skips to the latest occurrence instead of nagging; a snooze moves only that occurrence
  (`snoozed_from`). WAL + busy timeout (bot and MCP server share the file).
- Nagging (`first_reminder`, `mark_sent`): an undated task by priority (high every 2 h in 08:30–21:00 on the first
  day, then daily; normal daily; low every 2 days) for 3 days, then the same "still needed?" (`nagging_since`; a
  snooze or edit starts a new run). A deadline is reminded 15 min before its time (08:30 on a bare day) and not nagged before that;
  once it has passed, the bot asks once the next morning, "still needed?" (Done / Keep / Tomorrow / Drop), and then
  only the brief and the weekly review list it. More than 3 reminders at once go out as one list. The priority guess
  (`Router.priority`) picks normal or low: high is the user's word.
- `secretary/memory.py`: notes with SQLite FTS5 (accent-folded, prefix match, crude Italian singular/plural folding).
- `secretary/service.py` (`Life`): the 9 tools once, plus `brief()` and `snooze()`. The Pydantic AI agent
  (`secretary/agent.py`) and the MCP server (`mcp_server.py`) are thin wrappers.
- `bot.py`: AnyJev routing with button fallback, `/today` `/week` `/notes` `/think` `/auto` `/life` `/money`,
  reminders with Done/Snooze buttons, daily brief. No LLM in reminders, brief or listings.
- `router.py` + `jev_ollama.py`: one AnyJev L0 decision per message over Ollama (see README for numbers).
- `pi/think.ts`: one pi SDK session per `/think`, tools restricted to `mcp__life__*`, credentials from pi's store.

Verified on 2026-10-25:
- 40 offline tests pass (store/recurrence, notes, service, router over AnyJev's FakeBackend, MCP in-process
  round-trip incl. error messages, agents with scripted models).
- Live gemma4 secretary, 12 Italian messages in sequence (reminder, appointment, weekly chore, 2 notes, week agenda,
  recall, move, complete, dinner with 2 h reminder, recall, cancel): every message got the right tool and the DB
  matched; ~2 s per turn (first 6 s, cold). The weekly chore was stored as a recurring appointment rather than a
  task; it still reminds every Monday.
- Router: 15/16 + 6/6 follow-ups on gemma4, median 0.66 s (README).
- pi → MCP → SQLite with `ollama/gemma4:12b` standing in for ChatGPT: correct tools (after sharpening the MCP tool
  descriptions: first try stored "remind me" as an appointment), 48 s, clean exit.
- `pi mcp list`: life connected, 10 tools. Bot handlers driven with mocked Telegram objects: routing, chat, agenda.
- Finance third pass (2026-10-25): 61 offline tests (merchant shapes, migration of a first-pass ledger, reseeding,
  summary, recurring with skipped months and mixed payees, budgets, bill settled by a real-format import and not
  twice, last month's payment not settling a new monthly bill, price change, brief). On a copy of a real ledger:
  recurring detection found the phone plan, the streaming and cloud subscriptions, and marked the cancelled ones as
  stopped. Live gemma4, 10 Italian finance messages: the right tool every time, 2-6 s per turn; a per-merchant
  yearly total equal to SQL to the cent. Router 8/8 on finance-vs-life messages
  (README). `/uncat` suggestions: ~4 s each, sensible for businesses (airlines → travel; a beach bar →
  restaurants) but gemma4 is saturated (p≈1.0) and puts people under 'shopping', hence the fixed `transfers`
  option and buttons instead of auto-applying. Bot handlers driven with mocked Telegram objects.

**Box, desktop, Google** (fourth pass, groundwork; runbook in `deploy/BOX.md`):
- `compute.py` (`Desktop`): is Ollama on the desktop up (2 s check, failures cached 20 s), Wake-on-LAN magic packet,
  wake-and-wait shared by concurrent callers. `bot.py` checks it before every model turn; while the desktop is off,
  `FRANZISKA_WHEN_OFF` = `ask` (buttons ☁️ ChatGPT now / 🖥️ Wake desktop), `cloud`, or `wake`. A chat pinned to
  money always waits for the desktop. `/desktop`, `/desktop wake`.
- `cloud.py`: the pi bridge as a function, with the last 4 cloud exchanges as context (for desktop-off chats).
- `export_vocab.py`: the router's tokenizer as a tensor-free GGUF, 15.8 MB instead of 8 GB, so the box can route
  with the desktop's logprobs. Verified identical tokens and chat template to the full gemma4 blob.
- `google/auth.py`: the user's own OAuth client; one-time sign-in through `google-auth-oauthlib` via `uv run --with`,
  then refresh with plain httpx (no Google SDK in the bot). Scopes: `calendar.events`, `gmail.readonly`.
- `google/calendar.py`: appointments live in Google Calendar when `FRANZISKA_GOOGLE=1` (write: add, move, cancel;
  Google notifies the phone). A local mirror (`calendar_events` in life.sqlite, window yesterday → +120 days,
  recurring events expanded) gives short stable ids `#c5`, offline reads, and a "last synced" warning when stale.
  Task ids stay integers (finance's `bills.task_id` untouched). Moving/cancelling a recurring event acts on one
  occurrence.
- `google/gmail.py` + `mail/client.py`: new Gmail headers (sender, subject, ~200-char snippet; promotions/social/forums
  skipped by query) every 10 min; two AnyJev decisions per message on the local model (kind of 6, and "asks you to
  act within days"); untriaged mail waits for the desktop and is never sent to the cloud. What needs you is pushed,
  listed in the brief (last 24 h) and in `/mail` (3 days).
- MCP: times are plain strings parsed by the server (see the pi finding below); ids accept `12`, `#12`, `c5`.

Verified (fourth pass, 2026-10-25):
- 84 offline tests (box/desktop wake logic, OAuth refresh and 401 retry, calendar add/move/cancel/refresh against a
  fake Google with time zones, all-day and recurring instances, Life with Google ids and outages, mail fetch/triage/
  digest, bot desktop-off policies with mocked Telegram, lenient MCP times).
- Mail triage live on gemma4, 10 realistic Italian emails: kind 9/10 (a dentist confirmation called `action`, not
  `appointment`; both count as needing you), "act within days" 6/6 on the kinds that need you (2 misses on a
  shipping notice and a LinkedIn digest, which the digest filters out anyway); ~4.5 s per email.
- Bot live on this desktop: local route (appointment saved, 6.9 s), then desktop "off" with `cloud` policy through
  the real pi bridge (gemma4 standing in for ChatGPT): reminder saved, follow-up "anzi alle 10" moved it.
- **Found and fixed**: pi validates tool arguments against the JSON schema; a Python `datetime` parameter becomes
  `format: date-time` (RFC 3339, offset required), so local `2026-10-26T10:00` was rejected twice, after which the
  model told the user "moved to 10:00" with nothing changed. Times are now strings parsed server-side, and the cloud
  prompt forbids claiming unconfirmed changes. After the fix: 3/3 follow-ups moved the reminder, 12-15 s instead of
  43-65 s. Also: a turn that ends with reasoning only is asked once for a reply.
- **Not yet run**: the bot against real Telegram (needs a BotFather token), `/think` on ChatGPT (needs `pi` →
  `/login openai`, interactive), Google (needs the user's OAuth client), Wake-on-LAN (needs BIOS + netplan on the
  desktop, and the box).

## 2. Findings

### Chat channel
- **Telegram** chosen: official, free bot API, quick setup.
- **WhatsApp**: bots only via Meta's business Cloud API, or unofficial libraries (Baileys, whatsmeow) that risk
  a ban of the number.
- Any cloud messenger sees message text, including figures the bot replies with. Full locality would need a
  self-hosted messenger (e.g. Matrix).

### "Claw" agents (OpenClaw, nanobot, PicoClaw, ZeroClaw, NanoClaw)
- All support Ollama/OpenAI-compatible local models and Telegram. Footprints: PicoClaw/ZeroClaw tiny (Go/Rust),
  nanobot ~100–200 MB (Python), OpenClaw >1 GB (Node). NanoClaw isolates each agent in a container.
  PicoClaw launched Feb 2026; maturity claims vary by source.
- Not used as the core because they are general agents with shell/file access (large blast radius next to bank
  data), and a 12B local model does much better with a few narrow tools than a big general toolset.
- Still usable later as a front end, pointing at our tools exposed as MCP servers.

### pi (pi.dev)
- Moved from `@mariozechner/pi-*` (deprecated May 2026, last 0.73.1) to **`@earendil-works/pi-coding-agent`**,
  v1.4.0 (2026-10-21), MIT, repo github.com/earendil-works/pi, **Node ≥ 22.19** (not installed here yet).
- Integration modes: TypeScript SDK (`createAgentSession`, custom tools via extensions `pi.registerTool()`,
  `SessionManager.inMemory()`); CLI print / JSON / **RPC** (`pi --mode rpc`, long-lived JSONL, drivable from
  Python); built-in **MCP** client (SDK sessions must add `createMcpExtension()`); Ollama via `models.json`.
- Built-in coding tools can be restricted (`tools: [...]`, `noTools`), which we would do.
- Credentials live in `~/.pi/agent/auth.json` (keep private).

### Using subscriptions instead of API keys
- **ChatGPT: officially allowed.** OpenAI launched "Sign in with ChatGPT" on 2026-09-29; pi was a launch partner
  (`/login openai`; the older reverse-engineered "OpenAI Codex" provider is now "legacy"). Usage counts against
  the Plus/Pro plan. Preview limits: `store: false` and `stream: true` required, no `temperature`/`top_p`,
  no hosted tools (file search, code interpreter, hosted MCP), no server-side conversation state.
- **Claude: not allowed through third-party apps.** Anthropic's legal page: OAuth is for "ordinary use of Claude
  Code and other native Anthropic applications"; "Anthropic does not permit third-party developers to offer
  Claude.ai login into their own applications, or to route requests through Free, Pro, or Max plan credentials"
  and may enforce "without prior notice". Third-party tokens blocked since 2026-04-04 (OpenClaw hit first);
  bans reported. Pi still offers an Anthropic login: **do not use it.**
- Compliant Claude options: the unmodified `claude -p` under your own login, for occasional personal "ask Claude"
  turns (limits assume "ordinary, individual usage"), not an always-on backend; or a small Claude API key budget.

### Hardware
- RTX 3060 12 GB desktop: dense models up to ~14B at 4-bit, MoE models up to ~35B-A3B with experts partly in
  RAM (gemma-4-26B-A4B at 38 tok/s, decision 19).
- Raspberry Pi 5: great host for bot + SQLite + pi + deterministic redaction; too slow for a useful LLM (1–3B).
- Options: Pi always on + wake-on-LAN the desktop for local inference (reminders and `/tasks` need no LLM), or a
  ≥32 GB mini PC. With a cloud tier, a Pi 5 8 GB is enough. Deployment: `uv sync` + a systemd service.

## 3. Design: decisions taken and what's next

### Decisions (2026-10-25)
1. **Harness: both, split by job.** Pydantic AI stays the local agent loop (tested, small tool sets, works with
   gemma4). pi is the cloud path: it has Sign in with ChatGPT, which Pydantic AI would need a custom flow for.
   Claude subscription: not used through third-party tools (see §2).
2. **Tools as MCP, finance excluded.** `franziska.mcp_server` exposes schedule + notes to pi, Claude Code and Codex.
   The ledger is not exposed to any MCP client; the cloud only ever sees what `/think` reads through `life`.
   Unchanged by the finance pass: bills cross into the schedule as dated tasks without amounts. If a cloud tool ever
   needs money context, the option is an aggregates-only server (category totals per month), never rows or SQL.
3. **Cloud is explicit.** `/think`, or a desktop-off message under `FRANZISKA_WHEN_OFF` (default `ask`: the user
   presses "ChatGPT now"). The router never escalates on its own. The cloud sees schedule, calendar and notes through
   MCP; never the ledger, never mail.
4. **AnyJev where a decision is a classification.** Routing is one typed choice with a probability, no parsing,
   ~0.7 s, on the model already loaded. Not used for the agents' tool calls (that is generation).
5. **Deterministic where possible.** Reminders, snooze, brief and agenda listings never call a model. Same for
   finance: summaries, budgets, recurring charges, bill matching, merchant parsing.
6. **One-way link.** Finance may create and complete secretary tasks (bills); the secretary never reads the ledger.
   One brief, two sources.
7. **Box always on, GPU on demand.** Everything without a model (reminders, brief, listings, calendar sync, mail
   fetch, pi) runs on a ~10 W box; Ollama runs on the desktop, reached over Tailscale and woken over the LAN. The box
   routes with a 16 MB vocabulary file and the desktop's logprobs. Mail triage and finance wait for the desktop.
8. **Google is the home of appointments, the box keeps a mirror.** Writes go to Google first (the phone gets the
   notifications and the calendar works without us); reads come from the mirror (fast, short ids, offline). Tasks,
   notes and money stay local. Gmail is read-only; nothing is ever sent from the user's account.

### Next
The old 2026-10-25 'next' table is gone: most of it is built (CHANGELOG.md), the rest is in `../TODO.md` and
`ROADMAP.md`.

### Not done (deliberately)
- No embedding model for notes and memory: FTS5 is enough for them at this size. Documents have one (§11).
- No automatic cloud escalation; no mail content to the cloud; no sending mail.
- `data/` and `.env` are git-ignored.

## 5. Architecture: how the pieces fit (2026-10-27)

Fully local for now, to find where a 12B model's limits are. The rule that decides where a feature goes:
**use the lowest rung that does the job**, because every rung up is slower and fails in more ways.

| Rung | What it is | Where | Examples |
|---|---|---|---|
| 1. Code | deterministic Python over the stores and APIs | `*/service.py`, `google/`, `documents.py` | reminders, brief, bill matching, Gmail search, PDF text |
| 2. Decision | one AnyJev question on the local model: a choice or yes/no with a probability, no text generated | `router.py` (`route`, `choose`, `decide`) | routing, priority, mail triage, "is this a bill / about the condominium?" |
| 3. Extraction | one Pydantic AI call with a typed output; code checks the fields against the source | `workflows.Ctx.extract` | issuer, amount and due date out of an email; a workflow's params out of a message |
| 4. Agent | a Pydantic AI loop with a small toolset over one service | `secretary/agent.py`, `finance/agent.py` | "sposta il dentista a venerdì", "quanto ho speso da Deliveroo?" |
| 5. Open-ended | an agent that combines tools, CLIs and skills it wasn't scripted for | not built (below) | "sort out my condominium paperwork" |

**Building blocks**

- **Services** (rung 1) own the data and every rule. Everything else is a thin wrapper over them, so a feature lives
  in one place: the agent tool, the MCP tool, the /command and the CLI all call the same method.
- **Workflows** (`franziska/workflows/`, rungs 1–3): a fixed job, written as plain sequential Python: search →
  decide → read → extract → report. One file each with `WORKFLOW = Workflow(name, title, description, params, run)`
  and one line in `ALL`. That alone makes it a router route (the description is the option the router reads), a
  `/name` command, and `python -m franziska.workflows name "…"` (on the bot's own `Core`). Its `scope` (mail for
  `mail_bills`) is the memory scope its data belongs to: that route's `past_conversations` sees its runs. Params come
  from the message through one typed
  call (rung 3); a bare command uses the defaults. **A workflow never writes**: it returns a Report whose Proposals
  become buttons, and the write runs on tap. Delete the file and the line, and the feature is gone everywhere.
- **Agents** (rung 4): one per domain with that domain's tools only (9–14 each). The router keeps the toolset a
  12B model sees small; we never give one agent every tool.
- **Decisions** (rung 2) are where Jev is central: anywhere the code has to choose, it asks a typed question
  instead of parsing text. Inside workflows (filter, classify), in front of agents (route), and later inside loops
  (below). They cost ~1 s, can't produce malformed output, and give a probability to threshold on; the known
  weakness is that gemma4's probabilities are saturated (p≈1.0 even when wrong), so thresholds are weak until a
  better-calibrated model is measured (CHANGELOG §3).

**First workflow: `mail_bills`** ("trova le bollette e gli avvisi del condominio dell'ultimo mese"). Gmail keyword
search (recall) → per message 1–2 AnyJev questions on sender + subject + snippet (bill / notice / other; about the
topic?) → full text + PDF attachments of the kept ones (`pdftotext`) → one typed extraction each (issuer, summary,
amount, due date; an amount not literally in the text is dropped) → merge duplicates (reminders of the same bill),
sort by due date → a ➕ button per future bill that calls `Money.add_bill`. Live on gemma4 against a 7-email fake
mailbox (rata in a PDF, a sollecito of the same rata, an assemblea, Enel, TARI, a payment receipt, Amazon):
all 7 classified right, the receipt and the order dropped, the duplicate merged, amounts and dates right including
the PDF-only one; 16–19 s; router 9/10 on bill-, money- and life-shaped messages (the miss is the known ETF case).
Then the full Telegram path in-process: message → route → workflow → buttons → tap → bill + "Pay" task saved.

Real mailbox, 2026-10-27 ("condominio, ultimi 30 giorni"). First version: 100 emails judged one by one (cap hit,
job alerts matched 'avviso'/'notice'), 13 entries for ~4 real items (the same notice 5×, residents' replies each an
entry, a stale assemblea), ~55 Gmail requests, 81 s. Reworked along three rules that hold for any workflow:
- **Cheapest check first.** A topic named by the user is the Gmail query itself, and a subject containing it is on
  topic in code; only the rest get a Jev yes/no, on sender + subject only. The bill/notice choice (sender +
  subject + snippet) runs only on what is left. Here: 0 topic questions to the model.
- **Work per conversation.** Threads, merged by subject without Re:/Fwd:/PEC prefixes; one request for every
  subject in a thread, one for every body; replies without their quoted text, summarised in one 💬 line.
- **Read lazily.** Attachments are downloaded one at a time, likely-relevant names first, only until the ~2k-token
  budget is full; images, signatures and PEC envelopes never (the convocation thread had 11, one was read).
Result: 4 conversations, 4 entries (a past rata with the residents' dispute, the next rata with its due date and a
➕, a maintenance notice, the last assemblea under "Earlier"), 16 Gmail requests, 45 s.

**What comes next, only when a real request needs it**

- **Skills** (rung 4 made bigger without more tools): `skills/<name>/SKILL.md` in the open Agent Skills format
  (frontmatter `name` + `description`, a markdown body, optional scripts). For a local model, selection is a Jev
  decision over the descriptions (plus "none"), not the model reading a catalog; the chosen body is added to the
  agent's instructions for that turn. A skill can say "run workflow X" or "use CLI Y". The same files work in
  Claude Code or pi later. First candidate: weekly review / planning (CHANGELOG §4).
- **CLI as the universal interface**: each service already has, or gets, a `python -m franziska.<x>` command
  (workflows do). That is what an open-ended agent uses (one `run` tool with an allowlist of our CLIs, in a
  sandbox, read-only credentials), and what MCP clients and you use from a terminal. gogcli fits here if Drive /
  Contacts / Sheets are ever needed: one more CLI on the allowlist, with `--readonly`.
- **Open-ended loop** (rung 5), only after the eval harness (CHANGELOG §1) shows where workflows stop being enough.
  The shape to copy is mini-swe-agent: a linear message history, one shell tool, no framework. Jev's role inside
  it: which skill, is the goal reached (stop / continue / ask the user), is this step a write that needs a button.
  If gemma4 can't drive it, the measured result says so and the cloud tier comes back as an explicit choice.

**Decisions (2026-10-27)**
9. Fully local for mail content: workflows that read mail run only on the desktop's model.
10. New capability = a service method first, then the thinnest wrapper that exposes it (workflow, tool, command).
    No second implementation of the same behaviour for another harness: MCP or the CLI is the shared surface.
11. Writes from anything automated are proposals the user taps; only direct chat requests write straight away.
12. pi / `/think` stays parked (login failing). If it comes back, the cloud agent should be the same Pydantic AI
    agents with a cloud model instead of a second tool layer in `pi/think.ts` (13 MCP tools vs ~29 local ones today).
13. Open-ended mail is a local agent (`mail/agent.py`) over `gog mcp` + the `life` MCP server, not new tools of our
    own: gog already has typed, allowlisted Gmail tools (no send, no permanent delete). Its model is a separate
    setting (Ollama or llama-server) and is picked by `evals/mail_eval.py`, which runs the real agent against a fake
    Gmail with gog's schemas, judged on mailbox state (2026-10-27).

**Decisions (2026-10-28)**
17. Local model work runs one request at a time; cloud calls run in parallel. `franziska/gpu.py` holds a
    cross-process file lock around every local request (agents through `gpu.Local`, AnyJev decisions through the
    router's backend); long jobs (evals, sweeps) `hold` it, and the bot then answers "busy" at once. The lock
    serialises requests but doesn't evict a resident model: a second runtime (llama-server next to Ollama) is
    started per job and stopped after, or everything moves to one runtime (ROADMAP phase 2).
18. Guards are a per-run pydantic-ai capability (`franziska/guards.py`), not code in each agent: the loop guard on
    every route, and the reply checks each route gets (`guards.CHECKS`: dates for the secretary, figures for
    finance, each backed by a tool result or the user's message; finance's own digit validator is gone; the "saved" /
    "done" phrase checks were dropped 2026-10-30, decision 35). The CLIs run through the same guard
    (`guards.run_sync`). A stopped run replies with the tool lines; no model is involved.
19. One local runtime and one model: llama.cpp's `llama-server` with gemma-4-26B-A4B (UD-Q4_K_M, `--fit on`, the
    MoE experts that don't fit partly in RAM) serves every agent, the router, triage and mail, as a systemd user
    unit (`deploy/franziska-llm.service`, `FRANZISKA_BASE_URL`). It won the mail sweep (18/21 multi-step against
    4/7 for gemma4:12b) at 38 tok/s, and one resident model ends the Ollama/llama-server VRAM clash of decision 17.
    The router reads logprobs from `/completion` (`franziska/jev_server.py`, top 50, matched by token id); Ollama
    stays a fallback (`FRANZISKA_BASE_URL` empty). Cost: routing is ~2 s per message (4 short prefills that Gemma's
    sliding-window cache can't share), against ~0.8 s on the 12B. Evals that need other models stop the unit and
    start it again after.

## 6. Agent memory (design 2026-10-28, built the same day)

**Context comes from the agents, not from the router.** The router still only picks the agent. Each agent gathers
the state it needs through its own tools (`open_tasks`, `agenda`, `plan_overview`, `recall`, `month_summary`...).
We don't inject a state snapshot into the instructions. What the tools can't give is what the assistant has
*learned*, and that is kept in memory files. Conversation history stays as it is (`HISTORY_TURNS = 6` in
`bot.py`). Budgeting history by tokens is backlog.

### Three kinds of memory

| Kind | What | Written by | Removed |
|---|---|---|---|
| **notes** (exists, `secretary/memory.py`) | facts the user asked to keep: gate code, sizes, document numbers | the user, through `remember` | only by the user (`forget`) |
| **profile** | stable facts about the user that every agent and workflow can rely on: home and flat, condominium, utilities, name on bills | a ✅ tap on a proposal, the user editing the file, or `/memory` | replaced by a newer value; never expires |
| **cache** | one domain's working knowledge: "condominium mail comes from studio-rossi@…", "'il dentista' is Dr. Bianchi", "the user always snoozes the gym reminder to the evening" | the agent or a workflow, on its own | expires when unused; replaced; contradicted; `/memory` |

Notes and profile stay separate. Notes are the user's notebook, searched only when they ask. Profile is what the
assistant relies on, and the agent sees it on every turn.

### Storage: one markdown file per entry

`data/memory/<scope>/<name>.md`, under `Settings.data_dir` (git-ignored like the rest of `data/`; the user may
`git init data/memory` to get history of what the bot learned). Scopes follow the **domain of the data**, named
like the router's routes, not the caller:

| Scope | Read by | Written by | Leaves the machine? |
|---|---|---|---|
| `profile` | every agent (incl. chat) and workflow | only on a ✅ tap, by `/memory`, or by hand | no (not on MCP in v1) |
| `life` | secretary agent | secretary agent, life workflows | no (not on MCP in v1) |
| `money` | finance agent | finance agent | never (decision 2) |
| `mail` | mail agent, mail workflows (`mail_bills`) | the same | never (decision 9) |

File format: flat `key: value` frontmatter (parse it by hand; no YAML dependency), then the body:

```markdown
---
name: condominium-sender
description: condominium mail (rate, convocazioni) comes from studio-rossi@example.it
kind: cache
source: mail_bills 2026-10-28
created: 2026-10-28
last_used: 2026-10-28
---
Also PEC from amministrazione@pec.studio-rossi.it. The rate arrive as PDF attachments named "Rata_N.pdf".
```

- `name` is the identity: lowercase slug `[a-z0-9-]{2,48}`, made by code from whatever the model passes (slugify,
  truncate). Writing to an existing name **replaces** the entry (keeps `created`), so a fact is updated rather
  than duplicated.
- `description` ≤ 120 chars, and it **states the fact itself** when it fits, because it is what the agent sees in
  the index. The body (≤ 1000 chars, truncated by code) holds detail only.
- `kind`: `profile` in the `profile` scope, `cache` everywhere else.
- Writes are atomic: write `<name>.md.tmp`, then `os.replace`. Only the bot process writes (the MCP server
  doesn't, in v1), and workflows run in its threads, so that is enough.

**Well-known profile names**, so code can read them without a model (workflows use these, and agents should reuse
them rather than invent synonyms): `home` (address, scala, interno), `condominium` (name, administrator, the
administrator's mail addresses), `utilities` (one line per utility: supplier, contract/POD/PDR code if known),
`name-on-bills` (how the user's name is written on bills and statements). Agents may propose other profile names.
Profile holds no amounts and no secrets (codes and passwords go to notes).

### Service: `franziska/memory.py` (rung 1)

This is new and unrelated to `secretary/memory.py`, which is the notes FTS store. All the rules live here, and
the tools, `/memory` and workflows are thin wrappers.

```python
@dataclass(frozen=True)
class Entry:
    scope: str; name: str; description: str; body: str; kind: str; source: str
    created: date; last_used: date

class MemoryStore:
    def __init__(self, root: Path, clock: Callable[[], datetime]): ...
    def get(self, scope: str, name: str, touch: bool = True) -> Entry | None   # touch → last_used = today
    def learn(self, scope, name, description, body="", source="") -> Entry      # cache write; profile scope refused
    def set_profile(self, name, description, body="", source="") -> Entry      # only from a tap / /memory / code
    def forget(self, scope, name) -> Entry | None
    def entries(self, scopes: Iterable[str]) -> list[Entry]
    def index(self, scope: str) -> str      # the text added to the agent's instructions (below)
    def expire(self) -> list[Entry]         # nightly; returns what it removed (logged)

@dataclass
class MemoryView:                           # what one agent run gets
    store: MemoryStore; scope: str          # its own scope; profile is always readable
    proposals: list[Proposal] = field(default_factory=list)   # workflows.Proposal, shown as buttons after the reply
```

Rules (all deterministic, no model):
- **Index** (`index(scope)`): a header line, then one line per entry, `- profile/home: Via X 12, scala B, interno 7`.
  Profile first, then the scope's own entries by `last_used` descending. Cap at ~1500 chars; past the cap, add
  `(+N older: read_memory needs the exact name)`. If both scopes are empty, return `""` and add nothing.
- **Expiry**: cache entries whose `last_used` is more than **60 days** old are deleted by `expire()` (a constant
  next to the store; move it to `.env` only if it needs tuning). Profile never expires. `last_used` moves on
  `get(touch=True)` and on `learn`. Known limit: a fact the agent uses straight from the index line is never
  "read", so it ages even while useful. Re-learning refreshes it, and `/memory` lets the user prune. Don't try
  to track index use.
- **Cap per scope**: at most 40 cache entries; `learn` beyond that deletes the least recently used one first.
- **Contradiction**: no special machinery. The agent instructions say "if a tool result contradicts a memory,
  trust the tool and `learn` the new value (same name) or `forget_memory` it".

### Agent tools: three, the same for every agent

Built by `memory_toolset(view) -> FunctionToolset` (pydantic_ai.toolsets), with the view captured in a closure.
They are **added where the agent is run, not to the agent definitions**. `Agent.run` takes `toolsets=` and
`instructions=` (Pydantic AI 2.61; first check that run-time `instructions` are *added to* the agent's own,
not replacing them):

- `read_memory(name)`: looks in the view's scope, then `profile`; returns the description and body, or "no
  memory named X; the index lists what exists".
- `learn(name, description, body="", profile=False)`: with `profile=False`, writes a cache entry in the view's
  scope and returns "saved memory X". With `profile=True`, it **writes nothing**: it appends a Proposal
  ("💾 Save to profile: home = Via X 12, scala B, interno 7") whose `apply` calls `set_profile`, and returns
  "proposed; the user will confirm with a button". The agent must not tell the user it was saved.
- `forget_memory(name)`: own scope only (profile is changed through `/memory` or a new proposal).

The chat agent gets no tools, only the profile index (read-only).

Instruction block added with the index (one shared constant, kept short for a 12B model):

```
Your memory (what you learned before; read_memory(name) for detail):
<index lines>
- Facts the user asks you to keep ('ricordati che…', codes, sizes) are notes: use remember, not learn.
- learn what helps you next time and no tool can tell you: how the user names people and places, where something
  was found after searching, a preference they repeat, a correction they made. Never tasks, appointments or
  amounts; those live in their tools.
- A stable fact about the user (home, flat, condominium, utilities, name on bills): learn(..., profile=True); it
  is saved only after they confirm.
- If a tool result contradicts a memory, trust the tool and update (learn, same name) or forget_memory it.
```

For the secretary the notes/memory boundary is the risky part (`remember` vs `learn`). Check it by hand first
(below).

### Where it plugs in

- `config.Settings`: `memory_dir` property → `data_dir / "memory"`.
- `bot.py`: one `MemoryStore` in `Bot.__init__`. In the agent call (the `route` branch around
  `agent.run(...)`), the scope is the route (`life`, `money`, `mail`; `chat` → profile only, no tools). Build a
  `MemoryView`, pass `instructions=view_instructions(view)` and `toolsets=[memory_toolset(view)]`. After the
  reply, if `view.proposals`, send them as buttons the same way workflow proposals are sent (`wf:` callbacks and
  `chat_data`, around `bot.py:304`). Reuse that code rather than adding a parallel `mem:` path, unless that turns
  out awkward.
- `mail_agent.build(settings)`: the mail agent is built once, and its toolsets are MCP, so it gets the memory toolset
  and instructions at run time through the same call site as the other agents. Nothing changes in `mail/agent.py`.
- `/memory [word]`: lists entries grouped by scope (profile first), filtered by word if given, with a 🗑 button
  each (max ~20, profile and money included: it is the user's own chat). A tap calls `forget`. No LLM.
- Nightly job: `app.job_queue.run_daily(bot.nightly, 04:00)`, calls `store.expire()` and logs the result (and,
  since the turn log, its retention).
  It needs no model, so it runs on the box too.
- Workflows: `Ctx.memory: MemoryStore | None`. A workflow **reads profile by well-known name in code**
  (`ctx.memory.get("profile", "condominium")`) and **writes cache in code** (e.g. `mail_bills` learns the
  condominium sender once it has seen it, scope `mail`). When it extracts a plausible profile fact (the name on a
  bill), it adds a Proposal like any other write (decision 11). When a needed profile fact is missing, the report
  says so in one line ("Non so qual è il tuo appartamento: scrivimi 'abito in scala B, interno 7'") and the
  workflow carries on without it. That chat message then goes to the secretary, which proposes it for the profile.
- MCP: nothing in v1. Claude Code can read the files directly. If the cloud path comes back (decision 12), expose
  a read-only `read_profile`, never `money` or `mail`.
- Backups: the nightly off-machine backup (CHANGELOG §5e, hosting) must include `data/memory/`.

### Build order and how to check it

1. `franziska/memory.py` (store, view, toolset, index text) + `Settings.memory_dir`.
2. Secretary first: wire it in `bot.py`, then `/memory` and the nightly expiry.
3. Check by hand on the real bot (no unit tests, see CLAUDE.md), each time looking at `data/memory/` afterwards:
   - "ricordati che il codice del cancello è 4471" → a **note**, no memory file.
   - "abito in via Roma 12, scala B interno 7" → a 💾 proposal; nothing in `profile/` until ✅; after ✅,
     `profile/home.md` exists and the next turn's index shows it.
   - "quando dico 'il dentista' intendo il dott. Bianchi" → a `life/` cache entry; a later "sposta il dentista"
     uses it.
   - A repeated statement with a new value replaces the file (same name), and doesn't create a second one.
   - `/memory` lists and deletes; set a `last_used` 61 days back by hand, run the expiry, and the file is gone.
4. Finance and mail agents (same call site, scopes `money` / `mail`).
5. `mail_bills` reads `profile/condominium` + `mail/condominium-sender` to sharpen its Gmail query, and learns the
   sender. That is where this pays off first, and it unblocks CHANGELOG §5a "long documents by page" (which row of a
   rendiconto is the user's).

**As built (2026-10-28).** Differences from the plan above: `learn` takes `fact` and `detail`, not `description`
and `body`, because gemma4 put the fact in `body` and left the required `description` out on every retry; an empty
`fact` falls back to `detail`. The index's header line is added by `memory.instructions(view)`, not by `index()`,
and the notes rule ("use remember, not learn") is shown only to agents that have `remember` (secretary, mail).
`MailHeader` gained `address`, since `sender` is the display name. `mail_bills` learns a sender only when it sent a
condominium bill or started two condominium conversations (a resident's PEC forward was learned otherwise).

**Decisions (2026-10-28)**
14. The router only routes; agents pull context through their tools. No state snapshot is injected into the
    instructions.
15. Learned knowledge is markdown files, one per entry, scoped by data domain (`profile`, `life`, `money`, `mail`).
    Cache entries are written freely and expire by disuse. Profile entries are written only after the user
    confirms. Notes stay the user's own, separate store.
16. Memory housekeeping is code (replace by name, expiry, per-scope cap, `/memory`), not model judgement. A
    model-driven weekly cleanup is optional and comes later, only if stale entries turn out to be a real problem.

### Trust, corrections and links (2026-10-28, ideas from graphify)

[graphify](https://github.com/Graphify-Labs/graphify) was evaluated and not adopted. It's a code knowledge
graph for coding assistants: with no LLM it gets only headings and links from prose, its memory benchmark harness
isn't in the repo, it rebuilds a `graph.json` in batches and doesn't know our privacy scopes. Four of its ideas fit
our memory files, done in code in `franziska/memory.py`:

| graphify | Here |
|---|---|
| Edges tagged `EXTRACTED` / `INFERRED` | `confidence: stated \| inferred`. Stated: the profile (a tap, `/memory`, by hand), or every name, number and address of the fact appears in the user's own message of that run (`said_by_user`, like the reply guard's evidence check). Mail and workflow facts are inferred: mail is data, not the user |
| `reflect`: a lesson is preferred only when corroborated by 2+ results; corrections are kept | Learning the same name again: the same fact, or the same with more specifics (a second address), on another day → `seen + 1`; anything else is a correction → `corrected + 1`, the old fact in `was`. Confirmed = stated or `seen ≥ 2` |
| Signals decay (half-life) | Unconfirmed entries expire after 30 days unused (confirmed: 60) and are evicted first when a scope is full. Shown as `(?)` in the index and in `/memory`; the rules tell the agent to check a `(?)` before relying on it |
| `graph-expand` retrieval (its best LOCOMO configuration) | `[[name]]` / `[[scope/name]]` in a fact or detail. `read_memory` shows linked entries one hop both ways, only within the scopes the agent may read (own + profile), so links never cross a privacy boundary |
| An ambiguous name is refused with candidates | `read_memory` / `forget_memory` on a missing name return up to 3 close entries with their facts; nothing is picked or deleted |

New frontmatter keys, written only when not default: `confidence`, `seen`, `seen_on`, `corrected`, `was`. Old files
read as before (profile → stated, cache → inferred, seen 1).

20. Memory trust is code, not model judgement: provenance from the user's own words, confirmation by repetition
    on distinct days, corrections kept, links followed one hop inside the reader's scopes. No graph library, no
    embeddings; a `relations` table comes only with the Phase 4 stores (people, documents) that need it.

### Past conversations: the turn log (2026-10-28, ideas from agentmemory)

[agentmemory](https://github.com/rohitg00/agentmemory) was evaluated and not adopted. It's a memory server for coding
agents: a Node runtime plus an engine binary, English-only matching (a Porter stemmer, a coding synonym table, an
English embedding model), automatic capture with model-written summaries (decisions 11 and 16 say no), and one store
shared by every agent over MCP (our privacy scopes say no). Its trust and duplicate-name ideas overlap graphify's and
are covered by the section above. What it had that nothing here did: **search over what was said in earlier turns**
(its episodic memory). With `HISTORY_TURNS = 6`, "cosa avevamo deciso per il bagno?" had no answer unless it had
become a note.

**One job, one place.** This adds no new kind of memory, and nothing is copied between stores:

| Job | Lives in |
|---|---|
| Facts the user asked to keep (gate code, sizes) | notes: `remember` / `recall` |
| What the assistant learned | memory files (trust, corrections, links: section above) |
| What was said in earlier turns | the turn log, read only through `past_conversations` |
| Record of runs for evals and debugging | the same turn log |
| Reverting a change | each store's own undo (`tasks.undo` behind `/undo`); the turn log records, it doesn't revert |
| Matching words (accents, Italian plurals, stop words) | `franziska/fts.py`: notes, the turn log and `candidates` use it |

**`franziska/fts.py`**: `fold` (accents off, lowercase), `words` (drop words of ≤ 2 letters and a short stop list,
cut the last letter of words of 5+, so 'scarpa' meets 'scarpe' and 'dentista' meets 'dentist'), `match` (the FTS5
query, quoted prefix terms joined by OR). It replaces copies in `NoteStore.search` and `MemoryStore.candidates`.

**The turn log: `franziska/turns.py`, `turns.sqlite` in the data dir** (`Settings.turns_db`). This is ROADMAP phase
1's turn log.

- One row per answered message: `at, chat_id, route, p, text, reply, tools, stopped, ms`. `tools` is the JSON of
  `Guard.calls`, which the guard already records (name, arguments, ok, result cut to 500 chars), so there is no new
  instrumentation. `stopped` is the guard's reason, `busy` or `error: <type>`. FTS5 over `text` and `reply`.
- Written in `Bot.answer` (every exit: reply, guard stop, busy, error), `run_workflow` (route = the workflow's name)
  and `answer_cloud` (route `cloud`). `answer` takes `p` from the router; it's NULL when the chat is pinned or a
  button chose. A failing write is logged and swallowed: the log never breaks a reply.
- **Its own file**: it holds money figures and mail content, and the `life` MCP server opens `life.sqlite`.
  `mcp_server.py` and `cloud.py` never import `turns`.
- Retention: `turns.KEEP_DAYS = 365`, in the 04:00 job, now `Bot.nightly` (memory expiry plus turn retention).
- Not logged yet: button taps and jobs. When the audit log (ROADMAP safety 1) needs them, they become rows in this
  table (route `tap` / `job`), not a second log.
- SQLite rather than the JSONL ROADMAP first named: recall needs FTS over the same rows. Evals read the table.

**`past_conversations(query)`**, the fourth memory tool, in `memory.toolset(view)` when the view has a turn log:
- It exists only in local runs started by the bot: never on MCP, never in `/think` or a desktop-off cloud turn.
- It searches **only the rows of the view's own scope**: its route plus the workflows in that scope
  (`Workflow.scope`), so life sees life turns, money money, mail mail and `mail_bills` runs, and chat has no tools.
  It's the memory-scope rule, so a money figure never reaches the secretary.
- At most 5 rows, best bm25 first (text weighted 2, reply 1), as `[2026-09-23] user: … | you: …`.
- One rule line, shown only when the tool is there: when the user refers to an earlier conversation, call it; what
  was said then may be out of date, and tools and memories win.
- Not part of `recall`: `recall` is a `Life` method that the MCP server exposes, and a note is a fact the user chose
  to keep, while a past turn is only what was said. Not injected automatically: decision 14.

**Guard fix found on the way**: `guards.Call.wrote` no longer counts a result starting with `already` as a write
(like `proposed`). Before, "already in the profile as X" let a reply saying "l'ho salvato" pass the reply check.

**Checked** (2026-10-28):
- On scratch stores, no model: 'bagni' finds a turn about 'bagno'; a money turn never comes back for life; a
  400-day-old turn expires; notes 'scarpa' → 'scarpe', 'citta' → 'città'; `read_memory('dentista')` → the
  `dentist` candidate.
- On gemma-4-26B through the week harness: the new `evals/memory.py` step "cosa avevamo deciso per il bagno?" (a
  life turn and a money turn about the bathroom, seeded 40 days back) called `past_conversations` and answered
  "rifaresti il bagno a marzo con l'idraulico Marco", without the 864 €. The row was in the log with its tool calls.

**Not taken, and when to look again**

| Idea | Why not now | Look again when |
|---|---|---|
| Embeddings / hybrid search over notes and the turn log | DESIGN §3: FTS5 is enough at this size (documents have it since §11). agentmemory's gain over plain keyword search was measured in English on bigger corpora, and an embedding model would compete for the GPU lock | `past_conversations` measurably misses turns that only a synonym would have found |
| Model-written summaries of conversations into memories | Decisions 11 and 16. The log stays raw and is searched | not planned |
| Version history of memory files (git) | Every agent `learn` is in the turn log with its arguments; corrections keep `was` | taps on profile proposals need history (audit log) |
| agentmemory as a dependency | Above | not planned |

21. One job, one place: the user's facts are notes, what was learned is memory files, what was said is the turn
    log, and reverting is each store's undo. No store is copied into another, and word matching is `fts.py`.
22. The turn log is one SQLite table (`turns.sqlite`) for evals, debugging, recall and, later, the audit log.
    Agents read it only through `past_conversations`, only their own scope, never over MCP or in a cloud run.

### open-ontologies (2026-10-31, evaluated, not adopted)

[open-ontologies](https://github.com/fabio-rovai/open-ontologies) (Rust, v2.1.0, MIT; a pyoxigraph-based
`open-ontologies-lite`) checks changes to OWL/RDF ontologies before they ship (plan, blast radius, apply, drift,
rollback). Its derivation certificates are checked by a verified Lean checker, and it serves 118 `onto_*` MCP tools.
Nothing here is an ontology. Memory is markdown files with `[[links]]` and privacy scopes (decision 20: no graph
library), and the Phase 4 `relations` table is small enough for SQLite. The finance categories are 11 flat groups,
so subclass inference adds nothing a `GROUP BY` doesn't. The certificates prove RDFS/OWL entailments, not SQL sums,
so they can't replace the grounding check. Its retrieval-slice checks need triples, and the library retrieves text
chunks. 118 tools would swamp a local model whose agents have about ten. One idea is worth keeping: show what a
change will reach before applying it. A category correction ('Q8 is transport') that rewrites past payments could
say "23 payments, 412 €, 8 months" and wait for a tap. That would be plain SQL, if `/uncat` doesn't show it already.

## 7. Investments and the financial analyst (design 2026-10-28)

Full plan: `docs/INVEST.md`. Not built yet.

23. DEGIRO lives in `ledger.sqlite` (`inv_` tables, account kind `broker`, the money privacy class). Trades come from
    the Transactions export, every other cash event from the Account statement, and the Portfolio export is only a
    check (quantities and cash on its day). Derived tables are rebuilt by code; the model never computes a figure.
24. The import proves completeness: the statement's balance chain per currency (cash-sweep rows excluded: 0 breaks
    on the real file), order ids and fees matched across the two files, quantities and cash matched to a snapshot,
    content-hash ids so overlapping exports are idempotent.
25. Prices from Yahoo Finance's unofficial chart endpoint, FX from the ECB. A ticker mapping counts only when its
    close is within 1% of a snapshot's closing price; without a price the value is the last snapshot's, dated.
26. Web search is a self-hosted SearXNG. Code writes the brief's queries; a model-written query with any number
    other than a year is rejected. A run that read a web page is tainted: its writes are proposals.
    (2026-10-30) The web route (`search.py`) writes queries for the user's own questions: a number in one must be a
    year or in the user's message. It has no personal data (no memory, no profile) and no writes; without SearXNG
    it searches DuckDuckGo's HTML page and Google News' feed.
    (2026-10-30) SearXNG runs (`deploy/searxng`, Docker, local only). The web route falls back to DuckDuckGo /
    Google News when it doesn't answer; web_search takes `recent` and `page`, read_page reads PDFs and long pages
    in parts.
27. The market brief is two outputs: a market report from public data only (`market.sqlite`), which may run on a
    cloud model through pi later (`FRANZISKA_MARKET_MODEL`), and a portfolio overlay computed locally by code. No
    quantity, weight or amount enters the market report's input.
28. Weekdays a short brief at 07:30 after the life brief; Monday the past week and the week ahead. It always sends
    something: without the model, the overlay plus headlines; without search, the overlay alone, saying which step
    failed.
29. The analyst never trades and never says "buy now": ideas come as options run through `what_if` (allocation,
    costs, tax, concentration) and checked against the stated `profile/investor`. The tax helper is backlog.
30. (2026-10-28) The market report is a PDF (WeasyPrint), daily on weekdays and weekly on Mondays; the Telegram
    message is its caption. Until SearXNG runs, news comes from RSS feeds and code-written Google News search
    feeds; closes from Yahoo with FRED / ECB fallbacks. The model's three calls (pick, digest, write) use the
    output's JSON schema as llama-server's grammar (`Ctx.extract(native=True)`): as a tool call, the nested report
    failed to parse in 2 of 5 runs. Numbers are checked against the cited sources, as in decision 18.

## 8. Cloud models per route (2026-10-29)

31. A cloud model (OpenRouter or DeepInfra, OpenAI-compatible) answers the routes in `FRANZISKA_CLOUD_ROUTES`; every
    other route stays on the local model. The rule is no personal data in a cloud request, so `llm.CLOUD_OK` is
    `chat` (its cloud turns get no profile memory) and `markets` (public data, decision 27); the schedule and notes
    (personal by nature), money and mail are refused at startup if listed. The router stays local (it needs logprobs).
    `llm.Models.for_route` is the one place that picks: agents, workflows, the scheduled report and the CLIs all ask
    it. OpenRouter requests require zero-retention, no-training endpoints that support every parameter sent, and no
    4-bit quantisation. A failed cloud call falls back to the local model (`FallbackModel`); every cloud-route
    request's tokens and billed cost go to `usage.sqlite` (`/usage`). Evals on synthetic data may put every route on
    a cloud model (`evals/week.py --cloud`) to compare models on the harness; live runs follow the allowlist.

## 9. Rules the user can see, tool routing (2026-10-30)

32. Each agent's standing rules (its INSTRUCTIONS) are editable in the web UI (Memory → Rules): an edit is
    `<data_dir>/instructions/<agent>.md` and wins from the next turn (`franziska/instructions.py`); saving the
    built-in text or an empty box removes the file, so later code changes apply again. Only the bot and the web UI
    read edits; evals and the MCP server run on the code's rules. Keep them few: a rule the model keeps missing goes
    into code (decision 33), not into a longer prompt.
33. A preference about when things are reminded is data the store applies, not a memory the model must apply:
    `reminder_rules` (words in the title → time of day; `*` the default), used by first/due/day reminders, nagging,
    the "still needed?" question and the snooze buttons. Set by the life agent's `reminder_rule` or in the web UI.
34. The mail agent sees its tools by group: Gmail read, attachments and memory always; drafts, organising,
    Drive/Docs/Sheets and the schedule (life MCP + calendar_events) when an AnyJev yes/no on the message (and the
    previous mail message) says so, p ≥ 0.3 (`mail_agent.select`). `more_tools(group)` adds a group mid-run when
    that was short. Without deps (evals, scripts) it sees everything.
35. No phrase lists or keyword rules for meaning. Which tasks a reminder rule covers is an AnyJev decision on the
    title (`Router.reminder_rules`, stored as `tasks.rule_id`), not words matched in it; the guard checks only facts
    with a fixed written form (dates, times, figures) and no longer greps replies for "saved" / "done" claims. A
    weaker model will sometimes say it did something it didn't: that shows up in the evals and the turn log (tool
    calls are logged), and the answer is a better model or a clearer request, not one more pattern.

## 10. Assistants that pass work on (2026-10-31, docs/PROPOSALS.md part 1)

36. Delegation is one primitive in our core, `Core.delegate(route, request) → StepResult` (`franziska/delegation.py`):
    the assistant runs on the request alone (no conversation history, its own usage limit, not the caller's), with
    its guard, memory view and toolset; it returns its answer, its tool results (evidence for the caller's reply
    checks: `Guard.sources`), its writes as proposals, and whether it read untrusted content. Built on pydantic-ai's
    documented delegation pattern, not `pydantic_ai_harness.SubAgents` (0.x; a child's deferred writes can't reach
    the parent, which our taint buttons need). The turn log links an asked turn to the turn that asked (`parent`);
    the chat history doesn't show it.
37. Every assistant can `hand_off(assistant, why)` once per message: the run stops and the same message is answered
    on that route (labelled "💬 Chat → 🌐 Web"). Not in a pinned conversation, not after the run wrote something,
    and a message carrying a document goes only to `DOCUMENT_ROUTES`. The second run has no hand_off, so a message
    can't bounce. Chat's rule "tell them to rephrase" became "hand off".
38. `ask(assistant, request)` mid-run, by the table `delegation.ASK`: life → mail, money → life and mail. Depth 1
    (the callee has neither tool), at most 2 per run, local routes only. The callee is read-only: its writes come
    back as buttons on the caller's reply. An answer that read mail, a page or a document taints the caller, so the
    caller's writes after it are buttons too. Not life → money until money gives typed answers (`check_payment`,
    FRANZISKA.md F1; decision 6); nobody asks web (a request an agent writes can carry personal data into a query).

## 11. The document library (2026-10-31, docs/PROPOSALS.md part 2)

39. Every document the user sends (Telegram, web UI) and every mail attachment the mail agent reads is kept in
    `library.sqlite` (`franziska/library.py`): its text by page, passages of ~400 tokens that never cross a page,
    FTS5 over them, and one vector per passage. A document has a **privacy class** from the route it came in on
    (`library.SAVES`), and each route's tools search only the classes `library.READS` gives it, in the SQL: web sees
    `public`, money `money` + `personal`, life and mail `personal` + `mail`; chat has no tools. Tools:
    `search_documents`, `read_document`, `list_documents`, `keep_document`; web's `keep_page(url)` saves a page as
    `public` when the user asks. Search and read results are UNTRUSTED (guards.py): a run that used them is tainted.
    `/mail_bills` keeps the attachments of every bill and notice it finds; the mail agent's `index_attachments(query)`
    does the same for any Gmail search, as an open-ended request (code loops, the model picks the search; 30
    messages a call). A document sent to an agent still carries its first 6000 characters, plus its number and, when cut, how to read
    the rest. Unkept documents unused for 180 days go in the nightly housekeeping.
40. **Hybrid search**: FTS5 BM25 (query without function words, `QUERY_STOP`) and dense vectors (brute force in
    numpy over a BLOB column), top 30 each, fused by weighted Reciprocal Rank Fusion (k=60, words ×0.5), at most 3
    passages per document, adjacent hits of a page merged. Each passage is embedded with its document's title and
    section heading in front. Not sqlite-vec: this Python's sqlite3 can't load extensions, and at personal scale
    brute force is milliseconds. A cross-encoder reranker is supported (`FRANZISKA_RERANK_URL`) and off.
41. Embeddings run on the **CPU**, in their own llama-server (`deploy/franziska-embed.service`, EmbeddingGemma-300M,
    4 threads): no VRAM and no GPU lock. A document is searchable by words at once; the bot's `library` job embeds
    new passages every 2 minutes while no chat has had a message for 90 s and no turn runs. Without
    `FRANZISKA_EMBED_URL`, or with the server down, search is words only.
42. Chosen by `evals/library.py` (5 synthetic Italian documents, 38 questions labelled by page, 2026-10-31):
    recall@5 words 0.79, meaning 0.97, hybrid 0.95 (r@1 0.74 vs 0.68 for meaning alone); EmbeddingGemma-300M equal
    to Qwen3-Embedding-0.6B at a quarter of the time (25 vs 100 ms a query); the reranker raises r@1 to 0.89 at
    6.5 s a query on the CPU, so it stays off. The corpus and the questions are by one hand: rerun it with
    `--docs`/`--questions` on the user's own documents before changing any of these.

## 12. Sandboxed code and coding jobs (2026-10-31, docs/PROPOSALS.md part 3)

43. No agent receives a host shell. Code runs only through `franziska/sandbox.py`, with an empty Bubblewrap root,
    no network or host home, a read-only dedicated interpreter and only copied `/in` plus writable `/work` and
    `/tmp`. It runs in a cgroup-limited transient user service (memory, CPU, process count, wall clock and output
    size). Private inputs and network never coexist in a sandbox.
44. Level 1: `run_python(script, inputs, documents)` on money and chat, local model only, offered only when the
    runtime is ready (`Sandbox.problem`). Inputs are copied in: text the model passes (rows from `query_ledger`,
    pasted text) and, on money, library documents by number, filtered by `library.READS` like `read_document`
    (`/in/doc<N>.<ext>`, the original file or its text). A run over documents taints the turn (guards.py). Regular
    files under `/work/out` are copied out and go with the reply: photos on Telegram, inline in the web chat; kept
    30 days. The interpreter is `python -m franziska.sandbox bootstrap`: the distro Python plus pandas, matplotlib,
    openpyxl and pypdf, wheels only, installed by the administrator; model code never installs anything. Swap is off
    for jobs, so going over MemoryMax kills at once. `python -m evals.sandbox` verifies host files, Docker, network,
    other processes, system writes, memory, process count and timeout.
45. **This computer is a route, not a shell** (2026-10-31, `franziska/pc.py`, 🖥 PC, FRANZISKA_PC). Level 2 as
    proposed (one `run` tool over an allowlist of our CLIs) is still not built: no request needed it. What was asked
    for is questions about the machine and its files, and those are rung 1: typed tools over /proc, statvfs, `ps`
    (command names, never arguments), `systemctl --user`/`journalctl --user` for Franziska's own four units, `du`,
    and the file system inside FOLDERS (FRANZISKA_PC_FOLDERS, default the XDG user folders plus the coding
    repositories; never a hidden path or a key-like name, symlinks resolved first). Arguments are pydantic-typed and
    passed as lists. Reading a file or a log taints the run (`guards.UNTRUSTED`). It changes nothing by itself:
    move/rename, trash (`gio trash`), restarting a unit and starting a coding job are buttons (`workflows.Proposal`),
    and a coding job is never proposed by a tainted run. `send_file` attaches a file to the reply (up to 50 MB).
    Local model only (not in `llm.CLOUD_OK`). Live on the 26B, 2026-10-31: 9/9 messages routed right (6 pc, and a
    schedule, money and contract question that stayed on life/money), 4–11 s a turn.
46. **Level 3 coding jobs** (2026-10-31, `franziska/code.py`): started only by you (`/code`, the web UI's Code view,
    or a tap on a 🛠 button from an untainted pc turn). A job is a row in `code.sqlite` and a transient user unit
    `franziska-code-<id>` (it outlives a bot restart, one job runs at a time, `RuntimeMaxSec` is the backstop to
    FRANZISKA_CODE_MINUTES). The workspace is a `git clone --no-hardlinks --no-checkout` of an allowlisted repository
    (FRANZISKA_CODE_REPOS) or a new 'scratch' one, **not a worktree**: a worktree's metadata lives in the original's
    .git, which would have to be writable from the sandbox. Every command, the checkout included, runs in
    `Sandbox.shell`: the clone at /repo, /out, the repository's `.venv` and the interpreter it was made from
    read-only at their own paths (scratch: the sandbox venv), no network, a throwaway HOME, 4 GB / 256 tasks / 300 s
    a command. At the end the sandbox commits, runs the tests and writes a git bundle; the host fetches it into the
    original repository as `franziska/job-<id>` (scratch: `code_dir/scratch.git`) with fsck on, and makes the diff,
    stat and files there with `--no-ext-diff --no-textconv`: **no host git ever runs inside a tree the job wrote**,
    so a planted `core.fsmonitor`, alias or attribute can't run on the host or hide a change from the review. You
    get the report, the diff and buttons: ✅ merge (only into the branch it started from, checked out, with no
    uncommitted tracked changes; a conflict aborts), 💬 change (a follow-up job on the same branch), 🗑 discard,
    and after merging Franziska itself, 🔄 restart. Nothing pushes. `/pause` stops a job between two commands.
47. **The coding loop is mini-swe-agent's shape, not its package**: one `bash` tool, each command a fresh process,
    a linear history, the model's final answer as the report; on pydantic-ai and the local model through
    `gpu.Local`, so each step takes the GPU lock and the bot's turns interleave. The package would add LiteLLM, and
    its bubblewrap environment has neither our empty root nor the cgroup limits. A 32k window can't keep every
    output: past the latest 6 steps, outputs and long commands shrink to 400 characters and old reasoning goes
    (`code._trim`). The same command three times in a row is refused. Thinking on (FRANZISKA_CODE_THINKING), 4096
    tokens a response, 60 steps. OpenCode/Codex as cloud coders (C4) are still to evaluate.

## 13. What monthsim showed, fixed in the harness (2026-10-31, evals/reports/2026-10-31/monthsim-analysis.md)

Each fix is aimed at a cause behind several failures, not at one step. The three AnyJev questions below were measured
on the live 26B before use; their numbers sit next to the constants in `core.py`.

48. **A write the user asked for runs, even in a tainted run or an asked assistant.** Before a write becomes a
    button (`guards.Guard`, taint or `delegate`), one AnyJev yes/no (`Core._authorizer`): is this action, by its
    button label ("➕ controllare il conto · 2026-11-14 09:00"), one the user's message asks for, with these details?
    Its input is only the user's own words, today's date and the label: email, page and document text never reach
    it, so injected content can't make its own action pass. A wrong day or an action nobody asked for stays a button.
    Fixes "ho impostato il promemoria" over a ➕ the user never tapped (P1) and the user's own write demoted after an
    unneeded ask (P3). An asked assistant's answer now says what it did (`StepResult.wrote`).
49. **A reply that says a button's write is done is sent back once** (`Guard._check_claims`, `Core._claimer`): one
    AnyJev yes/no per waiting label on the reply, only on turns with buttons. Phrase lists stay dropped.
50. **Tools never answer ambiguously.** An edit with nothing to change is refused with the right tool to use
    (`edit_task`, `edit_cash`); a ledger query with no rows or only NULLs says "none / 0 EUR"; a Google service that is
    off (Drive API not enabled) is a final "unavailable" result, not a retry; a stopped run's fallback says what was
    looked up (tools and queries), never the raw result, except public results written for people (`READABLE`:
    web_search, news), which it shows.
51. **Searches have a budget** (`guards.SEARCHES`): from the 4th call of one search tool in a run its results say
    "not found is an answer"; the 9th stops the run. Gmail search hits come one line each in one untrusted-content
    wrapper instead of gog's wrapper around every field (`mail.agent.compact`), ~3× less text to read.
52. **Web searches before it answers** (`CHECKS["web"]` has `searched`): a reply with no tool call goes back once.
53. **A message that asks several things is planned** (`franziska/plan.py`, PROPOSALS D3 light): one AnyJev yes/no
    "two or more separate things?" (~1 s per message on an assistant route, auto mode, the user's own text), then one
    grammar-constrained plan from the message alone (≤4 steps, one per assistant, finders before doers), run in
    order through `delegate` with the user's words as authorization (48). A step that uses earlier results gets them
    with "decide its condition from these and act now" (the secretary otherwise searched its notes and claimed the
    reminder); a web step never gets other steps' results. The reply is each answer under its label, no composer generation. Fewer than two assistants in the
    plan → the normal turn. Hand-off and ask stay for what the plan doesn't cover.
54. **A note that is a lasting fact about the user also gets a 💾 profile button** (`Core._profile_offers`, one
    AnyJev yes/no per `remember`), so the other assistants (web included, via the profile) can use it.
55. **Documents are described when they come in** (`Core._describe`, `Library.describe`): a typed generation adds
    what the document is and the everyday words people search with to its title, which every chunk carries in FTS
    and in its embedding header. A contract that never says 'palestra' is found by 'palestra'.
56. **Life counts dates in code** (`shift_date`): '30 giorni prima del 15/01/2027' is a tool call, not arithmetic.
57. **Edited rules remember their base** (`instructions.stale`): the web UI marks an edit whose built-in rules
    changed since ("built-in changed"). Evals copy `instructions/`, so they run what production runs; the mail
    agent's `life` MCP server reads the simulated clock from `FRANZISKA_CLOCK_FILE`.
58. **A repeated read gets its result back once** (`Guard._check_loop`): an identical read with no write since
    returns the earlier result with a note, without running; the next identical repeat stops the run. Stopping on the
    first repeat killed a run that already had its answer (The Batch was already read). Every retry the guard sends
    the model back for is kept in `Guard.retries` and written to eval rows, since evals record no traces.

## 14. Claude on the subscription, as a model (2026-11-02)

59. **Claude is a third place a route can run** (`franziska/claude.py`, `llm.CLAUDE_OK`, FRANZISKA_CLAUDE_*): a
    pydantic-ai model over the Claude Agent SDK, signed in with the user's Claude plan (Claude Code's own login;
    Anthropic's support article says the Agent SDK and `claude -p` signed in with a plan draw from its limits).
    Not pi + pi-claude-bridge: Franziska's agents, guards, buttons and privacy views stay the harness, and only the
    model changes, in `Models.for_route`. The bridge's mechanism is copied, not its code: Claude Code runs with no
    built-in tools, no settings files and no MCP servers but an in-process one serving the run's tools; a tool call
    is held open, goes back to pydantic-ai as an ordinary call, and its result releases it (paired by Claude Code's
    `claudecode/toolUseId`). One process per agent run, nothing written to `~/.claude/projects`. Claude Code's
    PreToolUse `defer` was tried first and dropped: with two parallel calls only one came back, and resuming without
    a new prompt failed. API-key variables are blanked for the child and a run whose init frame reports a key is
    refused; a failed call (plan limit, not signed in) falls back to the local model.
60. **What may run there**: `CLAUDE_OK` = life, chat, web, pc, and the jobs plan, summary, learn, code. Money, mail,
    the workflows and document cards (`describe`) are wired through the same switch but refused at startup. A run on
    Claude sees `privacy.CLAUDE_SEES` (public, personal): past conversations, the thread (no cross-class follow-up),
    documents and the assistants it may ask are narrowed to those, so money and mail never reach it, also not
    through a plan step's earlier results, a summary or a fact pass (`Models.for_job` puts those on Claude only when
    every route they read is). Live 2026-11-02: `evals.week --until mon --claude sonnet` 11/11, median 10.8 s.

## 15. Sources
- mini-swe-agent (bash-only agent, linear history): https://github.com/SWE-agent/mini-swe-agent
- Agent Skills format and progressive disclosure: https://atlan.com/know/ai-agent/ai-agent-skills/what-are-agent-skills/
- smolagents (code actions vs JSON tool calls): https://github.com/huggingface/smolagents
- gogcli: https://github.com/openclaw/gogcli
- Anthropic, Claude Code legal and compliance: https://code.claude.com/docs/en/legal-and-compliance
- OpenAI, Sign in with ChatGPT preview limitations: https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
- ChatGPT subscription in third-party tools (2026-10-13): https://yage.ai/share/chatgpt-subscription-oauth-en-20261013.html
- Five ways to access AI subscriptions programmatically: https://yepanywhere.com/subscription-access-approaches
- Anthropic third-party subscription ban: https://gigazine.net/gsc_news/en/20260220-anthropic-third-party-block/ ,
  https://winbuzzer.com/2026/02/19/anthropic-bans-claude-subscription-oauth-in-third-party-apps-xcxwbn/
- Anthropic vs OpenAI on third-party agents: https://www.mindstudio.ai/blog/anthropic-restricts-third-party-agents-openai-opens-codex-comparison
- pi package and docs: https://www.npmjs.com/package/@earendil-works/pi-coding-agent (docs inside the 1.4.0 tarball:
  `docs/sdk.md`, `docs/providers.md`, `docs/cli-integration.md`, `docs/mcp.md`, `docs/models.md`)
- Claw comparisons: https://www.aimastery.page/articles/openclaw-alternatives , https://www.bitdoze.com/openclaw-alternatives/ ,
  https://www.aimagicx.com/blog/openclaw-alternatives-comparison-2026 , https://www.modemguides.com/blogs/ai-infrastructure/openclaw-alternatives-2026-secure-ai-agents
