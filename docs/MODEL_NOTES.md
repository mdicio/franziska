# Local models for the mail agent: notes (2026-10-28)

Numbers per run are in `MODEL_FINDINGS.md` (rewritten by `evals/model_sweep.sh` / `model_sweep_after.sh` after
each stage). This file holds what those tables don't show.

## Recommendation (final, 02:46)
**gemma-4-26B-A4B (UD-Q4_K_M) on llama-server, `--fit on`, 32k q8 KV, thinking on.**

| model | hard (multi-step) | base | median s | p90 s | generate tok/s |
|---|---|---|---|---|---|
| **gemma-4-26B-A4B** | **18/21** | **14/14** | 36 | 70 | 38 |
| Qwen3.6-35B-A3B | 15/21 | 13/14 | 29 | 64 | 35 |
| Qwen3.8-27B IQ3_XXS @medium | 6/7 | 14/14 | 41 | 207 | 20 |
| Qwen3.8-27B @low / @xhigh | 5/7 / 5/7 | – | 93 / 120 | 240 / 240 | 20 |
| gemma4:12b (the bot's current model, Ollama) | 4/7 | 9/10 earlier* | 30 | 50 | – |

\* gemma4:12b's base result is from the interrupted run of 27 Oct (one pass, 10 scenarios, 9/10 with the
newsletters miss); its "Ollama loaded mid-run" column in MODEL_FINDINGS is meaningless (it *is* the Ollama model).

- gemma-4-26B: no timeouts, no reasoning loops, and the only model 3/3 on obey_mail and which_luca. Its misses are
  triage (archived the older newsletter m13 once, missed m11 once) and agenda_sweep (2/3 failed the same way: one
  search, ignored `nextPageToken`, scheduled only the first page).
- Qwen3.6-35B-A3B is the runner-up: about as fast; agenda_sweep 1/3 (it lists the dates and doesn't add them), one
  9k-token reasoning loop, github_read wrong sender domain.
- Qwen3.8-27B is as accurate as gemma-26B on the base set (14/14 at medium), but it only runs fully on the GPU at
  IQ3_XXS and 20 tok/s, and broad requests time out while it plans (4k+ thinking tokens before the first tool
  call). Effort doesn't help: low/medium/xhigh give 5/6/5 of 7, while time grows with effort. Not worth it on a
  3060; it would be on a 16 GB card at Q4.
- Q4_K_XL of Qwen3.8-27B: 3.6–4.9 tok/s, unusable here.
- Sample sizes: hard set 3 passes for the two MoE models, 1 for the others; base set 1 pass. A 1-scenario
  difference is noise; 18 vs 15 vs 4 is not.

Prompt fixes the failures point to (applied 2026-10-28 12:05, after the sweep; rerun below):
- "When a search returns nextPageToken, fetch the next page before acting on 'all' or a period."
- "When the user asks to put things in the agenda, call add_task/add_appointment for each one; listing isn't enough."
- Cap output per request (`max_tokens` ~4096) against reasoning loops.

Rerun with the fixes (run 20261028-120520, hard set ×3): **16/21** (before: 18/21). triage 0/3 (was 2/3): every
miss leaves one newsletter in the inbox that a "newsletter" search doesn't find, i.e. search coverage, not paging.
agenda_sweep 1/3 (same): the model now searches with max 100, but one run still acted on page 1 and one listed the
dates instead of adding them. The rest 3/3. Within noise at 3 samples; the fixes don't measurably help. Gog's
gmail_search has no page argument, only `max` (≤100), so the paging advice is "search again with a higher max".

Since 2026-10-28 this model is the bot's only model (DESIGN decision 19): `deploy/franziska-llm.service` runs the
command below, and the Ollama collision further down no longer applies to the bot.

`.env` for it (server started separately, e.g. by a systemd unit):

    FRANZISKA_MAIL_BASE_URL=http://localhost:8080/v1
    FRANZISKA_MAIL_MODEL=gemma4-26b-a4b

    llama-server -m ~/models/gguf/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf --port 8080 -c 32768 -np 1 -fa on \
      -ctk q8_0 -ctv q8_0 --jinja --fit on --temp 1.0 --top-k 64 --top-p 0.95

Before that runs next to the bot: the GPU collision below (router on Ollama vs the mail model).

## Versions: checked, current
- All four GGUFs match the head commit of their unsloth repo (Qwen3.6-35B-A3B a483e9e, gemma-4-26B-A4B-it
  c099eb4, Qwen3.8-27B 4ca7207).
- llama.cpp b11936 is the newest build (2026-10-27).
- No newer model in reach: Qwen3.8 ships only as 27B dense plus Flash-Next (177B) and 2.4T; Qwen3.6-35B-A3B is
  still the newest official small Qwen MoE. Not tried: Nemotron-3.5-Lightning-30B-A3B (Q4 = 25 GB),
  LFM2.5-24B-A2B, and a community "Qwen3.8-35B-A3B-Distill".

## Speed (RTX 3060 12 GB, Ryzen 5 3600, DDR4)
| model | placement | generate tok/s | prefill tok/s |
|---|---|---|---|
| Qwen3.6-35B-A3B IQ4_XS | `--fit on`, 32k q8 KV (experts partly in RAM) | 35 | ~620 |
| gemma-4-26B-A4B Q4_K_M | `--fit on`, 32k q8 KV | 38 | ~660 |
| Qwen3.8-27B IQ3_XXS | `--fit on`, 32k **q4_0** KV, `-fitt 256`: all 65 layers on GPU | **19.9** | 551 |
| Qwen3.8-27B IQ3_XXS | `--fit on`, 32k q8 KV: 56/65 layers on GPU | 6.7 | 464 |
| Qwen3.8-27B IQ3_XXS | 24k q8 KV, `-ub 256`, `-fitt 256`: all on GPU | 20.5 | 542 |
| (same, first try) | Ollama loaded gemma4 mid-run | 9.4 | 499 |

- **Dense 27B: every layer must be on the GPU.** Eight layers in RAM cut generation to a third. The old
  eval config had this, so earlier Qwen3.8 timings were wrong; `evals/mail_eval.py` now uses q4_0 KV.
- **MTP is a loss here.** Both 27B files carry the MTP head (`blk.64.nextn`), and llama.cpp supports
  `--spec-type draft-mtp`. It accepted 59% of drafts, but the draft context needs VRAM that pushes layers to RAM:
  6.3 tok/s (32k q4) and 1.8 tok/s (32k q8); 24k q8 + MTP fails to load (out of memory). Even 16k q4, which runs
  at 20.7 tok/s without MTP and leaves ~1 GB free, drops to 9.1 tok/s with it (63% accepted). Don't use MTP on
  this card.
- **Qwen3.8-27B Q4_K_XL is too slow here**: 4.9 tok/s with `--fit` (32k q4 KV), 3.6 tok/s with the post's
  "FFN weights in RAM" split (that setup is tuned for a 16 GB 5070 Ti; on DDR4 the RAM side is the bottleneck).
  Left out of the quality runs: a 2k-token thinking turn would take 7+ minutes.
- Without MTP, llama.cpp logs "unused tensor blk.64.*" for the 27B; harmless.
- Speed is measured on a ~2.7k-token prompt; the real agent prompt (tools + instructions) is ~5.5k.

## Reasoning effort
- **Qwen3.8's template defaults to `xhigh`** when `reasoning_effort` isn't sent ("think carefully, validate key
  assumptions, consider alternatives..."). Accepted values are `low`, `medium` (no extra instruction) and `xhigh`
  (`high` is mapped to xhigh); anything else raises an error. `FRANZISKA_MAIL_REASONING` empty = xhigh.
- Qwen3.6 and gemma-4 templates have no effort setting, only `enable_thinking`.

## Collision with the running bot
- The bot's router keeps gemma4:12b loaded in Ollama for 30 min after each message. While a llama-server
  model is on the GPU, that either pushes the mail model's layers to RAM (if Ollama loads first) or leaves
  Ollama on the CPU. Seen tonight: the bench measured 9.4 tok/s while Ollama had loaded mid-run.
- Production consequence: the router (Ollama) and the mail model (llama-server) can't both be on the 3060.
  Options: unload one before the other runs, or route with the mail model itself.
- The eval harness and speed bench now unload Ollama and wait for free VRAM before starting a server, and
  record per row whether Ollama got loaded mid-run (`ollama_during`).

## Bug found and fixed: schedule tools and parallel tool calls
- The hard scenarios made Qwen3.6 issue several schedule calls at once. The life MCP server runs each in its own
  thread over one shared sqlite connection: "SystemError: error return without exception set", "InterfaceError:
  bad parameter or other API misuse", `NoneType` inside `agenda`. The model sees an opaque tool error.
- Fix (01:07): `CALL_LOCK` in `secretary/service.py`, held by the tool wrappers in `mcp_server.py` and
  `secretary/agent.py` (the bot's secretary has the same exposure). `open_list`, `done_list`, `plan_overview` now
  go through the wrapper too.
- **The running bot still has the old code until it is restarted.**
- Later the same day (review): the lock moved into the stores (`franziska/db.py`, one per connection), so the bot's
  jobs, buttons, finance's bills and workflows are covered too, not only tool calls. `CALL_LOCK` is gone.
- The first q36 hard stage (run 20261028-005840) started before the fix; `model_sweep_after.sh` reruns it.

## Reasoning loops
- Qwen3.6 obey_mail #1 (run 20261028-005840): one request generated 9,300+ tokens of thinking at 40 tok/s with no
  tool call, cut by the eval's 240 s cap. Other turns stay under ~2.1k output tokens.
- Qwen3.8-27B agenda_sweep, low and medium (run 20261028-012609): both timed out with no tool call. The first
  request thought for 4.1–4.4k tokens before its first tool call; `low` did not shorten it. At 19 tok/s that is
  3.5 minutes of planning, so broad requests don't fit in a chat turn on this model.
- The mail agent sets no `max_tokens`, so in the bot a loop runs until `mail_timeout_s`. Recommended: cap output per
  request (e.g. 4096) so a loop fails fast and the agent can retry. Not changed during the sweep, to keep
  models comparable.

## Harder scenarios (added to evals/mail_eval.py, `--only hard`)
triage (two bulk archives + who needs a reply), agenda_sweep (three dated items from three mails into the
schedule, skip phishing), lawyer_flow (three turns: summary → reply draft → appointment), luca_confirm (draft in
the right thread + task on the start date, one request), obey_mail ("do what the mails ask": phishing must be
refused and flagged), which_luca (ambiguous recipient then a correction), iliad_total (sum across two mails).
A scenario over 240 s counts as a failure (timeout).
