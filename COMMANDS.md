# Commands

Run everything from your clone of the repo. The CLIs below use `uv run --env-file .env python -m …`;
shortened to `fz` here: `alias fz='uv run --env-file .env python -m'`.

## Services (systemd user units)

| Unit | What |
|---|---|
| `franziska-bot` | the Telegram bot and all its scheduled jobs |
| `franziska-llm` | local model (llama-server, port 8080), used by every agent |
| `franziska-web` | web UI (port 8765) |
| `franziska-code-<id>` | one coding job's runner (transient, gone when it ends; `code.py`) |

```bash
systemctl --user status franziska-bot franziska-llm franziska-web   # running?
systemctl --user is-active franziska-bot franziska-llm franziska-web
systemctl --user restart franziska-bot franziska-web                # after code changes
systemctl --user stop franziska-bot                                 # stop (start / enable --now / disable likewise)
journalctl --user -u franziska-bot -f                               # live log (-n 100 --no-pager for the tail)
journalctl --user -u franziska-bot --since today | grep -i "error\|catch-up"
loginctl show-user $USER -p Linger                                  # Linger=yes: units start at boot, no login needed
```

SearXNG (web search, Docker, `deploy/searxng`): `docker compose -f deploy/searxng/compose.yml up -d` (restart,
`logs`, `down` likewise); engine errors at http://127.0.0.1:8888/stats.

First install / unit changes: `cp deploy/*.service ~/.config/systemd/user/ && systemctl --user daemon-reload`
(details at the top of each unit file, box setup in `deploy/BOX.md`).
Free the GPU for evals: `systemctl --user stop franziska-llm`, and start it again after.

## Logs and traces (what happened in one run)

Every message, tap, workflow and scheduled job is one **run**, with an id: the 8 hex digits in the journal's
`[3f2a91c0 life]` (run id, then the route, workflow or job). The journal tells the story, a couple of lines a run;
`traces.sqlite` in the data dir has everything inside it: the router's probabilities, each model request (messages in and
out, reasoning, tokens, GPU wait), each tool call (args, result), the guards' rewrites and stops (`events`). Kept 30
days, never backed up, local only (franziska/obs.py). A repeating tick that did nothing (reminders, calendar, mail) is
not recorded.

```bash
journalctl --user -u franziska-bot --since today | grep -E " (WARNING|ERROR) "     # what went wrong today
journalctl --user -u franziska-bot --since today | grep 3f2a91c0                  # one run's lines
T=~/.local/share/franziska/traces.sqlite
sqlite3 $T "select substr(trace_id,1,8) id, start, name, round(ms/1000,1) s, error from spans where parent is null order by start desc limit 20"   # latest runs
sqlite3 $T "select name, round(ms) ms, error, events from spans where trace_id like '3f2a91c0%' order by start"                                     # one run, step by step
sqlite3 $T "select json_extract(attrs, '\$.\"gen_ai.output.messages\"') from spans where trace_id like '3f2a91c0%' and name like 'chat %'"          # what the model answered, request by request
sqlite3 $T "select substr(trace_id,1,8), start, name, error from spans where error is not null order by start desc limit 20"                       # failures
```

**A UI for the same traces** (optional, only while you dig): Phoenix, made for LLM traces.

```bash
cp deploy/franziska-phoenix.service ~/.config/systemd/user/ && systemctl --user daemon-reload   # once
echo 'FRANZISKA_OTLP=http://localhost:6006/v1/traces' >> .env && systemctl --user restart franziska-bot franziska-web
```

Then the web UI's **Telemetry** link (left rail, or Ctrl K) starts Phoenix and opens it at `/phoenix/`, behind the
web sign-in, so it works from the phone too. `systemctl --user stop franziska-phoenix` (or Stop on the Data page)
stops it. New runs show up in both places; older runs stay in `traces.sqlite` only. For SQL on any store, the
**SQL** link is Datasette at `/sql/`.

## When the box was off (catch-up)

- **Reminders**: every overdue reminder is sent on the first minute tick after the bot starts.
- **Mail fetch, calendar sync**: run 30 s / 1 s after start, then on their interval; they pick up everything missed.
- **Daily jobs** (`franziska/catchup.py`): brief, markets, evening check-in, weekly review, Monday week summary, monthly
  summary on the 1st, nightly backup. The bot records the slot each one served (`~/.local/share/franziska/jobs.sqlite`).
  At start, and every 5 min (covers suspend/resume), a job whose latest slot has no record runs late **if still worth it**,
  and Telegram says "🔌 I was offline… catching up: …". Otherwise it is skipped, so a week off doesn't send seven briefs:

  | Job | Runs late for |
  |---|---|
  | nightly (backup, expiry) | 20 h |
  | brief | 12 h (until ~19:30) |
  | markets | 10 h, weekdays only |
  | review (Sunday 18:00) | 18 h |
  | evening check-in | 3 h |
  | Monday week summary | 3 days |
  | month summary (1st) | 7 days |

  Tune the windows in `franziska/bot.py` (`main`). Check what was recorded:
  `sqlite3 ~/.local/share/franziska/jobs.sqlite "select * from ran"`. Force a job to re-run on the next check by
  deleting its row (`delete from ran where job='brief'`) then restart the bot, or just run it by hand (below).

## Context window (chat memory)

- **Clear it**: Telegram `/new`, or the **New chat** button / `/clear` in the web UI (or `POST /api/new`), which also wipes the
  chat off the screen (a reload shows only what came after). It forgets the recent
  turns of every assistant; tasks, notes and learned memory stay. `/new` also resets the web counter below.
- **See it**: bottom right of the web chat, `3.8k / 32k` plus a bar (amber from 75 %). It is what llama-server counted
  for the last call of the last turn (prompt + answer), so it is what the next turn starts from. Each reply also has a
  collapsible "N steps": the tool calls and, when thinking is on, the model's reasoning.
- **Budget** (`franziska/budget.py`): history turns, attachment and Gmail output sizes and the room tool results may
  take are shares of the server's window (read from `/props`), so a bigger `-c` scales them with no code change. When a
  run's tool results exceed their share, the oldest become a one-line stub. Each turn's token count is in
  `turns.sqlite` (`select route, tokens from turns`).
- **Rolling summary** (`franziska/summary.py`): turns that age out of the history (past the turn cap), or that are
  older than the newest 2 once the window passes 60 %, are folded by the local model into one short summary per chat
  and route, in the background after the reply. The next run reads it as its first message. `/new` clears it. Check:
  `journalctl --user -u franziska-bot | grep summary:` and `select key, value from conversations where key like 'summary:%'`
  in `turns.sqlite`. The originals stay in the turn log.
- **Attachments** (`franziska/doccache.py`): a PDF's text (up to 40 pages with a text layer, 5 of a scan) is extracted once
  and kept in `~/.local/share/franziska/cache/docs/` (unused for 90 days, or beyond 200 MB, the nightly job deletes it).
  The mail agent reads it in pages (`read_attachment … page=N`, page size scales with the window) or asks
  `search_attachment` for the passages about an amount, a deadline or a name. Wipe it any time: `rm -r ~/.local/share/franziska/cache`.
- **Housekeeping** (`franziska/housekeeping.py`): the 04:00 job runs one pass per store and logs what it removed to
  `maintenance.sqlite`: turn log (365 d), memory cache (60 d unused, 30 d unconfirmed), handled events (365 d), triaged
  mail rows (180 d), document cache (90 d / 200 MB), report PDFs (60 d / 150 MB), SQLite WAL checkpoint nightly and a
  vacuum of stores that are over 25 % free pages, weekly. A failing pass shows in the brief as a health warning.
  Telegram `/housekeeping` shows the last 7 days and disk use; `/housekeeping run` or
  `fz franziska.housekeeping` does it now.
- **Consolidation** (`franziska/consolidate.py`, job `consolidate` at 04:30, needs the desktop's model, retried every 5 min
  until it is up): (1) from the new life and money turns the model proposes up to 3 durable facts per assistant, written as
  cache memories: `inferred` unless every name and number is in your own words, confirmed when seen again on another
  day, gone after 30 days unseen (mail turns are skipped on purpose: third-party text). (2) Turns older than 90 days are
  summarised per week and assistant into `digests` (found by `past_conversations`), then deleted; the hard cap stays
  365 days. See what it did with `/housekeeping` (rows `facts`, `digests`); inspect with `/memory`; bad guesses: 🗑 there.
- **Size**: `-c 32768` in `deploy/franziska-llm.service`. KV cost on the 3060: ~11 KB per token of window (q8_0 cache;
  only 5 of the 30 layers keep the full window, the rest use a fixed 1k sliding window). So +32k of window is about
  +360 MB of VRAM, and `--fit on` pays for it by moving a little more of the expert weights to RAM.
  After editing: `cp deploy/franziska-llm.service ~/.config/systemd/user/ && systemctl --user daemon-reload &&
  systemctl --user restart franziska-llm`. Prefill is ~400-500 tok/s, so a full 64k prompt takes over two minutes.

## In Telegram

`/help` lists them all. The useful ones:

| | |
|---|---|
| `/today` `/week` `/tasks` `/done` `/notes` | agenda, open tasks, finished this week, notes |
| `/undo` · `/new` | revert last change · fresh conversation |
| `/pause` · `/resume` | stop every assistant and scheduled push (reminders go on) · start again |
| `/memory [word]` | what I learned about you, with 🗑 |
| `/month [YYYY-MM]` `/weekly` `/budget` `/subs` `/bills` | spending reports (no model) |
| `/categorise` `/uncat` | group new payees · check guesses |
| `/portfolio` | DEGIRO value and positions |
| `/markets [weekly]` | market report PDF now |
| `/mail` · `/mail_bills [what]` | mail needing you · bills in email |
| `/think <q>` | one answer from ChatGPT (schedule + notes only) |
| `/desktop` · `/desktop wake` · `/usage` | GPU machine up? · wake it · cloud tokens |
| `/life` `/money` `/auto` | pin the chat to one assistant · back to routing |

Send a bank `.xlsx` or DEGIRO `.csv` to import it; voice notes work as text.

## Run a workflow / report by hand (no Telegram)

```bash
fz franziska.workflows                              # list workflows
fz franziska.workflows mail_bills "condominio, ultimi 60 giorni"
fz franziska.workflows categorise
fz franziska.workflows markets                      # market report
fz franziska.market daily|weekly [--day YYYY-MM-DD] [--no-model]
fz franziska.secretary --brief                      # the morning brief, no model
fz franziska.secretary "remind me to call the dentist tomorrow at 10"
```

## Finance and investments

```bash
fz franziska.finance import FILE.xlsx ...           # import bank exports (idempotent)
fz franziska.finance --month [YYYY-MM] | --week | --recurring | --budgets | --review
fz franziska.finance --categorise
fz franziska.finance "how much did I spend on food in September?"
fz franziska.invest import FILE.csv ... [--on YYYY-MM-DD]
fz franziska.invest show
fz franziska.market tickers [--force]               # symbol per holding · `ticker ISIN SYMBOL` sets one
```

## Backup, web, evals

```bash
fz franziska.backup                                 # same as the nightly job; lands in ~/.local/share/franziska/backup/<date>
fz franziska.web                                    # web UI in the foreground (normally the unit)
tailscale serve --bg 8765                           # expose the web UI on the tailnet
uv run --env-file .env python -m evals.funnel --models NAME   # a model through speed → router → smoke (evals/README.md)
uv run python -m evals.bench --list                 # the bench tasks (tier, tags); --check: judges vs references, no model
evals/queue.sh [label]                              # model evals, waits for a free GPU (evals/reports/2026-10-29/queue-results.md)
uv run python -m evals.library [--rerank]           # document search: words vs embeddings vs hybrid, CPU only
uv run python -m evals.library --docs DIR --questions q.jsonl   # ...on your own documents (stays local)
uv run python -m franziska.sandbox status          # run_python's runtime: ready, and which packages
uv run python -m franziska.sandbox install         # add/upgrade its packages (then restart the bot and web)
uv run python -m evals.sandbox                     # red-team check of the sandbox (~10 s, no model)

# Coding jobs (franziska/code.py): Telegram /code, the web UI's Code view, or a 🛠 button from the 🖥 PC assistant
fz franziska.code start franziska "aggiungi …"     # queue + start its unit (franziska-code-<id>); --wait: run here
fz franziska.code list                              # the latest jobs; show|diff|merge|discard|stop <id>
journalctl --user -u franziska-code-7 -f           # one job's runner (its commands run in the sandbox)
git log --oneline main..franziska/job-7            # a ready job is a branch in the repository it changed
```

Data lives in `~/.local/share/franziska/` (sqlite stores, memory, reports, backups). Settings: `.env` (see `.env.example`).
