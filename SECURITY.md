# Security and privacy model

Franziska holds the most personal data you have: your schedule, your notes, your bank movements, your mail. The design
assumes the model can be wrong or manipulated, and puts the protections in code, not in prompts.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security → Report a vulnerability). Please don't
open a public issue for something exploitable.

## Where your data is

- **Everything stays on your machine.** Databases, memory, documents, traces and backups live under
  `~/.local/share/franziska` (`FRANZISKA_DATA_DIR`); secrets under `~/.config/franziska`. Nothing personal is in the
  repository, and the pre-commit hook (`.githooks/`) refuses secrets, data files and figures from your own ledger.
- **The model runs locally** (llama-server or Ollama). Cloud models are off by default; see below.

## Who sees what

One table decides which data each assistant may read: `franziska/privacy.py` (`FRANZISKA_SEES` overrides it).
Data has four classes: public, personal, mail, money.

| Path | What it can see |
|---|---|
| Local model, `money` route | public, personal, mail, money |
| Local model, `life` / `mail` routes | public, personal, mail |
| Local model, `chat` / `pc` routes | public, personal |
| `web` route (searches the internet) | public only: what it reads can become a search query |
| Claude on your subscription (`FRANZISKA_CLAUDE_*`, opt-in) | public and personal; never money or mail. Money, mail and the workflows can't be put on it (refused at startup) |
| Another cloud model (`FRANZISKA_CLOUD_*`, opt-in) | only `chat` and `markets`, with no profile memory and no turn history; OpenRouter limited to zero-retention, no-training endpoints |
| MCP server (`franziska.mcp_server`, for Claude Code, Codex, pi) | schedule and notes; never the ledger |
| Market report | public data only goes to the model (names, tickers, articles); portfolio figures are computed in code and go only into the PDF |

## Untrusted content

Emails, documents, web pages and files are someone else's text, and may contain instructions.
- A run that read untrusted content is **tainted**: its writes (tasks, bills, notes, profile facts) become buttons you
  tap instead of happening (`franziska/guards.py`).
- The planner fixes the steps from your own message before anything untrusted is read (`franziska/plan.py`).
- Search queries written by a model are checked in code: a number in a query must be a year or one you wrote in
  that message, so figures from your data don't reach a search engine (`franziska/search.py`).
- The mail agent searches, reads, labels and drafts; it never sends.
- Replies with numbers are rejected unless a tool returned them (the figures guard).

## Running code

No agent gets a host shell. `run_python` and coding jobs run in bubblewrap with a dedicated read-only venv: no
network, no `$HOME`, no secrets, a wall-clock and memory limit (`franziska/sandbox.py`). Coding jobs work on a clone
and hand back a branch for you to review. `python -m evals.sandbox` is a red-team check of these limits.

## Access

- **Telegram**: only the user ids in `FRANZISKA_ALLOWED_IDS` are answered. Messages pass through Telegram's servers.
- **Web UI**: listens on `127.0.0.1` only. Binding to another address without `FRANZISKA_WEB_TOKEN` refuses to start;
  the intended remote access is `tailscale serve` plus the token.
- **Model servers** (llama-server, Ollama) have no authentication: keep them on localhost or your tailnet.
- **Kill switch**: `/pause` stops every agent, workflow and model job; reminders and your own commands keep working.

## Known limits

- Single user. There is no isolation between several people sharing one instance.
- The Google token and the Telegram token are files on disk, readable by your user.
- Linux only for the sandbox (bubblewrap, systemd-run). Without them `run_python` is off.
- A local model can still misread a request. Writes it makes on your own words are logged and `/undo` covers tasks;
  read the confirmation lines.
