# Web UI: Franziska as an app

Written 2026-10-28. ROADMAP phase 3 ("talking to it, beyond Telegram"), first cut. The MVP is
built and was checked by hand on a copy of real stores (below).

## 1. Why

Telegram stays the place for pushes (reminders, the brief) and quick capture. The web UI is for what chat is bad
at: seeing the week at a glance, ticking tasks off a list, the month's money side by side, pruning what the agents
learned. It is served by the always-on box, reached over Tailscale, and installed on the phone and desktop as an
app (PWA). That also takes the replies, figures included, off Telegram's servers for whatever is done here.

## 2. Shape

```
 phone / laptop (PWA) ──HTTPS (tailscale serve)──► franziska.web (FastAPI, uvicorn, one worker, on the box)
                                                      │  Web adapter: one conversation's state + NDJSON streaming
 Telegram ──────────────────────────────────────► franziska.bot (Bot = Telegram adapter)
                                                      │
                                    both are thin over franziska.core.Core
                         services (Life, Money, MemoryStore, TurnLog), router, agents, workflows, desktop
                         choose() · turn() · workflow_turn() · cloud_turn() · actions() / apply()
```

- **One core, two adapters.** `core.py` holds what `bot.py` used to do with Telegram objects in the way: the
  route choice (pinned mode, router, "unsure" options), the agent run with history, memory view, guard and turn log,
  workflows with a progress callback, the cloud turn, a file from the user (`receive`: import a bank or DEGIRO
  export, else read the text; `document`: that text as a message and its route), and the buttons: proposals (`Action("wf:<n>", label)` →
  `apply(state, n)`) and choices (`route:<n>:<route>`, `off:<n>:cloud|wake`, `/uncat`'s `cat:<n>:<i>`). What a
  button acts on is held under its own number (`Core.hold` / `take`), never in one slot per kind, so a tap on an
  older message can't act on a newer message's text. A conversation's state is a plain dict, so Telegram keeps it
  in `chat_data` and the web in its session. `class Bot(Core)`: every bot method and job is unchanged; only
  `answer`, `run_workflow`, `answer_cloud` and the ➕/💾 tap call the core now. (Decision 10: no second implementation.)
- **Its own process for now.** `python -m franziska.web` next to the bot: both open the same SQLite files (WAL, as
  the MCP server already does), and the GPU lock is cross-process (`gpu.py`), so turns from the two never overlap
  on the model. The web chat is its own conversation (history, pending questions, proposals), logged with chat id
  `0` in the turn log. Memory files may now be written by two processes. Writes are atomic `os.replace`, so the
  worst case is that one of two writes in the same second wins.
- **Reads from the services, writes through them.** Today = `Life.upcoming` (new: the structured version of what
  the agenda lists by day, which `_by_day` now uses too) + `tasks.open_tasks()` + `tasks.done()`. Money =
  `balances()`, `freshness()` and the existing text reports. Memory = `MemoryStore.entries()`. The writes are
  the bot's buttons: `Life.complete`, `Life.undo`, `MemoryStore.forget`, `Core.apply`.

## 3. API

| | |
|---|---|
| `GET /api/status` | desktop up?, can wake, mode, labels, today |
| `GET /api/chat` | the web conversation's last 40 turns (turn log) |
| `GET /api/workflows` | every workflow: `title`, `about`, `fields` (its Params as a form: kind integer / number / string / boolean / choice / date, default, bounds, choices, help), `ready` and what it is `missing`, the last 3 `runs` (any chat) |
| `POST /api/workflows/{name} {params}` | validated against its Params (400 with the field's error), then the same NDJSON stream as the chat; a desktop that is off is woken first (Run is an explicit tap, and workflows never go to ChatGPT). Logged as `/<name> key=value…` |
| `POST /api/chat {text}` | NDJSON stream: `status` lines ("Choosing an assistant…", "📅 Life · thinking…", a workflow's progress, "Waking the desktop…"), then one `reply {text, route, label, actions[], stopped}` |
| `POST /api/action {id}` | a button: `route:<n>:<k>` (which assistant), `off:<n>:cloud` / `off:<n>:wake` (desktop off), `wf:<n>` (apply a proposal → an `applied` line) |
| `POST /api/mode {mode}` | auto / life / money (Telegram's /auto /life /money) |
| `GET /api/today` | `week` (next 7 days, Tasks and Google appointments), `open` (the rest), `done` (today) |
| `POST /api/tasks/{ref}/done`, `POST /api/undo` | ✅ and ↩️ |
| `GET /api/money?month=YYYY-MM` | balances, freshness, summary, budgets, monthly charges, bills |
| `GET /api/memory`, `DELETE /api/memory/{scope}/{name}` | what was learned; 🗑 |
| `GET /api/rules`, `PUT /api/rules/agents/{name} {text}`, `POST /api/rules/reminders {words, at}`, `DELETE /api/rules/reminders/{id}` | Memory → Rules: each agent's standing rules (an edit wins from the next turn; the built-in text or an empty one resets) and the reminder-time rules |
| `GET /api/data` | every `*.sqlite` in the data dir (the set the nightly backup takes): size with its WAL, changed, tables and views with row and column names (no FTS indexes or their shadow tables); the report PDFs; the backup nights with their size; the memory count |
| `GET /api/data/{store}/{table}?q=&sort=&desc=&offset=` | 50 rows, newest first (`rowid DESC`) or by a column; `q` matches any column's text. The store is opened `mode=ro`, table and column names are checked against the schema before they reach SQL |
| `POST /api/files` (multipart `file`, `caption`) | up to 20 MB, into a temporary folder deleted after the turn; NDJSON like the chat. An export → the import report (logged as `📎 name` on money); else read, then an agent with the caption as the request (`said`) and the text `tainted`, so writes are buttons; desktop off → it says so, never the cloud |
| `POST /api/desktop/wake` | Wake-on-LAN and wait |
| `GET /api/context` | the context files: each memory file as on disk, the memory index each agent is shown, the agents' standing instructions |
| `GET /api/telemetry`, `POST /api/telemetry/start` / `stop` | Phoenix's state (up / starting / off / failed / missing); start waits for its UI |
| `/sql/…` | Datasette, mounted in this process (§9) |
| `/phoenix/…` | Phoenix's UI and API, proxied (prefix stripped); while it is off, a page that starts it |

Streaming is NDJSON over a plain `fetch` (not SSE): it is one POST with a body, it needs no extra dependency, and it
passes through `tailscale serve`. A turn runs as a task under the conversation lock, so it finishes and is logged
even if the browser goes away; a second tab waits for it.

## 4. Access and privacy

- **Tailscale only.** The server listens on `127.0.0.1:8765`; `tailscale serve --bg 8765` publishes it on the tailnet
  with a real HTTPS certificate, which is what lets a phone install it as an app. Nothing is reachable from the internet.
- **A token on top.** `FRANZISKA_WEB_TOKEN`: open `https://<box>.<tailnet>.ts.net/login?token=…` once per device; the
  cookie is HttpOnly, SameSite=Strict, Secure on HTTPS, valid for a year. Without a token the server answers only a
  browser on the same machine with no proxy in between: `tailscale serve` adds `X-Forwarded-For`, so a missing
  token can't open it to the tailnet by accident. Binding to a non-loopback address without a token refuses to start.
- **The same privacy classes as the bot**, because it is the same core. Money questions never go to the cloud, even
  from the desktop-off choice. Mail stays on the local model. ChatGPT is used only on an explicit ☁️ tap.
- No third-party requests from the page: no CDN, no fonts, no analytics. The service worker caches only the shell
  (HTML, CSS, JS, icon). `/api` always goes to the box, so no ledger data is stored in the browser.

## 5. Design

**Feel**: a calm notebook, not a dashboard. Warm paper background (`#f6f3ee`), ink text, one sage accent
(`#2f6f62`), a serif for headings and day titles (system Palatino/Iowan/Georgia: no web font to fetch), system sans
for text. Dark theme from the OS (`prefers-color-scheme`), with the same tokens. Each assistant has a quiet colour
used only on its chip: 📅 Life sage, 💶 Money amber, ✉️ Google blue, 💬 Chat violet, workflows the accent.

**Layout**: wide screens get a left rail (Chat, Today, Money, Memory, and a "Desktop on/off" pill that wakes it on
tap). From 1180 px the chat has "Today & tomorrow" beside it, so tasks can be ticked while talking. Phones get a
bottom tab bar, 16 px gutters, safe-area insets for the installed app, the desktop pill in the corner, and Enter
makes a new line (send is the button).

**Chat**: your messages are accent bubbles on the right. Franziska's are paper cards on the left with the chip of
the assistant that answered, and a warning chip when a guard stopped the run or the GPU was busy. While a turn
runs, one live status line shows the routing, the assistant, a workflow's steps, or the wake-up. Buttons sit under
the reply (➕ bill, 💾 profile, which assistant, ☁️ / 🖥️); a tapped proposal turns into its "✓ saved…" line, and a
second tap on an old one says it expired. A segmented control switches Auto / 📅 Life / 💶 Money. An empty chat
offers four example requests. Light markdown from the agents (bold, code, bullets, links) is rendered; the rest
stays as written.

**Today**: the date as the title. "This week" grouped Today / Tomorrow / weekday, with a time column (an
appointment's start–end, a task's due time; a bare-day deadline has none). Tasks have a round check, appointments
a dot (blue for Google). Chips for overdue, high priority, place and repeats. Below that the other open tasks by
priority, and what was done today, struck through. A tick gives a toast with Undo, and the header has "Undo last
change" (`/undo`).

**Money**: ‹ month › in the header, balance tiles per account with their date, a freshness warning when an export
is old, then the month summary, budgets, monthly charges and bills as cards (the service's own text, tabular figures).

**Workflows**: one card per workflow, built from `/api/workflows`, so a new workflow or param needs no change in the
page (`static/flows.js` draws the form for both views): what it does (the router's description, "the user" → "you"),
its settings with their defaults (a choice of up to 4 is a segmented control, else a select), Run, the live status,
and the result with its buttons (➕, ✅ Confirm all…), then "Recent runs" with time, duration, failed/done and the
reply. A workflow that can't run here says what it needs. The run is also a turn of the chat. In the chat, `/` at the
start of a message lists the workflows (Tab completes) and `/<name> words` runs it like the Telegram command; an
unknown `/name` lists the real ones instead of going to the router. A reply that lands while another view is open puts
a dot on Chat; a run that ends there gives a toast with "Show". The desktop pill says "Can't reach the box" when
`/api/status` fails.

**Data**: the stores on the left, each with its tables and views (row counts; empty ones greyed), the open one as a
grid: sticky header, click a column to sort (again to flip), search all columns (as you type), ‹ › by 50, `*_cents`
shown in euros, numbers right-aligned, NULL as a dash. A row opens in a side sheet with every column in full and JSON
(the turn log's `tools`) pretty-printed, so this is also the turn-log view. With nothing open: a tile per store, the
report PDFs (open in a tab), the backup nights, a link to Memory. Deep links: `#data/ledger/movements`. Read-only on
purpose: changes go through the assistants and their buttons. On the phone the list and the grid are two screens.

**Files**: drop one or more anywhere (an overlay says what happens to them), or 📎 in the composer; each is a turn
of the chat, one after the other. A bank or DEGIRO export is imported and the reply is the import report; a PDF,
photo, .eml or .txt is read on the box (text layer, then OCR) and goes to Life, Money or Chat (pinned mode, else the
router) with what was typed as the request, the same as a Telegram file with a caption. The chat shows `📎 name ·
size` and the caption; after a reload, the turn log's long document text is shown as `📎 name` again. The world takes
drops and 📎 too.

**Memory**: grouped About you / Life / Money / Mail, each entry with its name, fact, detail, a "guessed" chip when
not confirmed, last used, source. Filter box, and Forget takes two taps instead of a browser dialog.

## 6. Checked (2026-10-28)

On a copy of the real `life.sqlite` and `ledger.sqlite` in a scratch data dir, Google off, gemma-4-26B on
llama-server:
- "cosa devo fare oggi?" → routed to life, the four tasks due today, 12 s, streamed status lines.
- "abito in via Roma 12, scala B interno 7" → a 💾 proposal; the tap saved `profile/home`, a second tap on the
  same button said it expired. (The secretary's reply was stopped by the guard for claiming "saved" before the tap.
  The fallback text is the raw tool line, the same as on Telegram; see below.)
- "ho speso 25 euro dal barbiere oggi, in contanti" → routed to money, cash recorded.
- Today: ✅ completed a task, Undo put it back. Money: both Revolut balances, October's summary, charges. Memory:
  the new profile entry. A request with `X-Forwarded-For` and no token → 401.
- Desktop 1440 px and phone 400 px layouts, dark theme. The 6 flow tests pass after the core split
  (`tests/test_flows.py` and `evals/week.py` now patch `core.clock` / `core.guards` / `core.cloud`).

Not checked yet: the full week eval through the refactored bot (`evals/week.py`, needs the GPU), real Telegram
after the refactor, `tailscale serve` + installing on the phone, the desktop-off and wake paths in the web, a
workflow (`mail_bills`) with live progress (it needs Google on).

## 7. Next, by value

| | Item | Notes |
|---|---|---|
| 1 | Run it on the box | `deploy/franziska-web.service`, `tailscale serve --bg 8765`, token, "Add to home screen" |
| 2 | One process | Run the bot's polling and uvicorn in one asyncio loop (PTB's `initialize/start/updater.start_polling`). One Core, so the web and Telegram share a conversation, proposals and jobs, and the memory files have one writer again |
| 3 | Push to the app | Reminders and the brief as Web Push (VAPID, `pywebpush`) or ntfy, with ✅ / snooze actions in the notification; then Telegram becomes optional |
| 4 | Edit in place | Task detail sheet (title, due, priority, recur → `Life.edit_task`), drag a task to another day, `/uncat` as a list with category chips (`Money.add_rule`), memory edit |
| 5 | Charts | Month spending by category and the 12-month trend, from the ledger's aggregates, once they exist |
| 6 | ~~Drop zone~~ | Done 2026-10-28 (Files, above) |
| 7 | Turn log view | The Data view shows the `turns` table with tools in full; still to do: a filter by route / stopped, and labelling a turn's route for the router eval (ROADMAP phase 1) |
| 8 | Guard fallback text | "I got stuck, so here is what I found: proposed; the user will confirm…" shows a tool line meant for the model. Better: when the only blocked claim is a pending proposal, reply "Tap 💾 to save it" |

## 8. World (`/world`)

Written 2026-10-28. The same conversation as Chat, in a 3D landscape: Franziska as a core with rings at the centre of
an island, each member of the team in a region of its own (ski slopes, a harbour islet, a glacier, meadows, a surf
break, dunes, a pine forest), linked to her by lines of light. While a turn runs her rings spin up, pulses run out to
the member at work and come back with the reply, and the camera follows unless you moved it in the last 5 s. The sky
follows the local time (`?hour=21` to try another); snow falls over the snowy regions. "Classic" goes back to `/`.

**Kept to the high level on purpose.** The page knows only who is on the team and who is working now:
`GET /api/world` → `{lead, members: [{key, label, kind: assistant|workflow, about, turns}], days}` (routes and
workflows from the core, `turns` from `TurnLog.counts`), and the `route` on the chat stream's `status` lines. A new
route or workflow gets a region, a station, a link and a team-bar pill with no change to the page. What a member
does inside (tasks, ledger, mail) stays in the classic views.

**Code** (`static/world/`, ES modules, no build step): `main.js` lays out the sites (assistants on an inner circle,
workflows in the gaps further out) and runs the loop; `biomes.js` is the catalogue of landscapes, each with a height
function, colours, props and an optional landmark with its own animation (`PREFER` in `main.js` maps a member to a
landscape; anyone else takes the next free one); `land.js` blends the regions into one terrain with soft borders;
`atmos.js` sky, sea, snow; `team.js` the core, stations, links and pulses; `fly.js` the camera; `hud.js` the glass
panels and the chat. Three.js r170 is vendored under `static/vendor/three/` (MIT), so the page still makes no
third-party request and works offline as part of the app shell.

**Moving**: drag to look, right-drag or WASD to move, wheel or pinch to zoom, tap a station or a team pill to fly
there, 1–9 to jump, H home, Enter or / to type. "Full" / "Lite" switches the graphics (Lite: no bloom, fewer trees,
coarser terrain; the default on touch screens).

Checked in Chrome against the real stores: the island with 4 assistants and 3 workflows, flying to each station,
day / sunset / night, a 400 px phone frame, and a real message: routed to Life, pulses out, reply burst back (the
GPU was busy with an eval, so the reply was the busy line). Not checked: a real phone's frame rate.

**Cards** (added 2026-10-28): each member's `glance` in `/api/world`, a few counts, never contents: Life "2 left
today, next at 14:00 · 1 overdue", Money "2 charges expected this week · N payee groups to confirm · bank data is
old", a workflow "last run … · 12 s · failed". A workflow's card has its form and Run (the same `flows.js`): the run
streams through the chat panel, so the core spins up, the pulse goes out to that station and comes back. After every
turn the counts are fetched again. "Open Today ↗" / "Open Money ↗" / "All runs ↗" go to the classic view. "🖥️ Wake"
in the top bar while the desktop sleeps and can be woken.

Next: sound, voice with the rings pulsing as she speaks; glance items on the station itself, not only on its card.

## 9. Tools: SQL, telemetry, context files, search

Added 2026-10-30. Two open-source tools behind the same sign-in (`franziska/web/tools.py`), and smoother ways around the
classic UI.

- **SQL: Datasette** (datasette.io) at `/sql/`, an ASGI app mounted in the web process over every `*.sqlite` in the data
  dir (a new store shows after a restart). Any SELECT (5 s limit), facets, filters, foreign-key links, CSV/JSON out.
  Reads are `mode=ro`; with no signed-in Datasette actor its write API is refused. Themed with the app's tokens
  (`static/datasette.css`, served by Datasette's own static mount as `/sql/franziska/…`) and a "‹ Franziska" crumb.
- **Telemetry: Phoenix** (arize-phoenix) at `/phoenix/`. A systemd user unit, `deploy/franziska-phoenix.service`
  (installed, not enabled): the rail's Telemetry link opens `/phoenix/` in a new tab, which starts it if it is off
  (~15 s) and reloads into it. Its UI listens on 127.0.0.1:6006 only and is proxied here, so it works from the phone
  over Tailscale with the same cookie and is never on the tailnet without it. The bot and web send it spans via
  `FRANZISKA_OTLP`; older runs stay in `traces.sqlite` only. Stop it from the Data home's tile, the palette, or
  `systemctl --user stop franziska-phoenix`. A dot on the link shows it running.
- **Data view**: a filter over the tables (Enter opens the first), stores fold (remembered), column types under the
  headers, ↑↓ / j k between rows and Enter to open, the row sheet steps with ‹ › / ← →, Copy as JSON, "Explore ↗"
  (the table in Datasette) and "SQL ↗" (a query on it). The list scrolls to a deep-linked table.
- **Context files** (`#data/@context/<id>`): what the agents work from: each memory file (front matter as fields,
  `[[links]]` clickable, a missing target marked), the memory index each agent is shown, its standing instructions.
  Read-only.
- **Search, Ctrl/⌘ K** (and the rail's Search): views, every table, SQL per store, context files, workflows, and
  actions (new chat, undo, mode, open/stop Phoenix), matched by letters in order (`ldmov` → ledger › movements).
- **Chat**: each reply shows its time and a Copy button on hover.

Not checked: a real phone (the tools and Search are hidden from the phone's tab bar; the Data home links them),
Phoenix's playground (it may want websockets, which the proxy doesn't pass).
