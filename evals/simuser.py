"""A simulated user (ROADMAP 2): a stronger model plays the user, Franziska answers, the stores are judged. On demand.

Scripted evals (week, memory, xturn) send fixed messages, so a reply that asks a fair question ("quale bolletta?") is
a failure there, and a wording the script didn't foresee is never tried. Here Claude on the subscription plays the user
of a scenario: it knows a goal and some facts, writes one message at a time like a person on Telegram, answers the
assistant's questions, sends a document or taps a button when that is what a person would do, and says DONE when the
goal is reached or clearly won't be. Missing context added in a follow-up is normal use, not a failure; what counts
is the state at the end (tasks, bills, notes, memory, the turn log), checked as in evals/week.py, and how many messages
it took. The user's own verdict is recorded next to the checks, as a signal.

The bot is the real one, as in evals/week.py: scratch stores, invented data, a simulated clock, the configured routes
(FRANZISKA_CLAUDE_ROUTES included). A scenario has phases at given times ('later that day'); the chat and the stores
carry over between scenarios, so each one is on its own day and every setup runs before the first. Add a scenario for every real miss: invented
data here, real-data ones in the private overlay (pass them to `run`).

    uv run --env-file .env python -m evals.simuser                    # every scenario, ~10 min, budget 30 min
    uv run --env-file .env python -m evals.simuser --only bill,plan   # some scenarios
    uv run --env-file .env python -m evals.simuser --list             # scenarios, phases, checks
    uv run --env-file .env python -m evals.simuser --user opus        # who plays the user (default sonnet)

Results: evals/runs/simuser-<time>/ (scenarios.jsonl with every message, summary.md). Holds the GPU like the week eval.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pydantic_ai import Agent

from evals import week, xturn
from evals.week import Check, Doc, Out, Say, Tap, bill, reply, task
from franziska import gpu
from franziska.claude import ClaudeCodeModel
from franziska.config import ROOT, Settings

BUDGET_S = 30 * 60
MAX_MESSAGES = 6  # per phase: past it the phase ends as it is (a person would have given up)
GAP_MIN = 2  # simulated minutes between two messages of a phase


@dataclass
class Phase:
    at: str  # 'mon 09:00' or 'd3 16:00', as week.Step
    goal: str  # what the user wants now, in the user's terms


@dataclass
class Scenario:
    name: str
    persona: str  # who the user is and what they know (facts they can give when asked)
    phases: list[Phase]
    checks: list[Check]  # on the state at the end, and on every reply and tool call of the scenario
    docs: dict[str, tuple[bytes, str]] = field(default_factory=dict)  # files the user can send: name → (data, mime)
    setup: Callable[[week.World], None] | None = None
    known: str = ""  # a known failure: reported, doesn't fail the run


USER = """\
You play a real person writing to their personal assistant on Telegram, in Italian, to test it. Stay in the role:
- Write the next message only: short and casual, as people text (no greetings, no explanations of the test).
- Pursue the goal you are given. Give a fact the assistant asks for if you know it; don't volunteer facts a person
  would expect the assistant to know already (something said or sent earlier in the chat).
- To send a file, write exactly: SEND <file name>. To tap a button under the assistant's last reply, write exactly:
  TAP <button label>.
- When the goal is reached, or the assistant clearly can't reach it, write exactly: DONE ok, or DONE failed: <why,
  in a few words>. Judge by what the assistant says it did; if it only promises, ask.

Who you are and what you know:
{persona}"""


def _at(when: datetime) -> str:
    """A datetime as a week.Step time: 'd<days after MONDAY> HH:MM'."""
    return f"d{(when.date() - week.MONDAY.date()).days} {when:%H:%M}"


def _transcript(lines: list[str]) -> str:
    return "\n".join(lines) if lines else "(nothing yet: you write first)"


class User:
    """The model playing the user: one stateless run per message, over the whole chat so far."""

    def __init__(self, model: str):
        self.model = ClaudeCodeModel(model, timeout_s=180)
        self.label = model

    async def next(self, scenario: Scenario, phase: Phase, chat: list[str], now: datetime) -> str:
        agent = Agent(self.model, output_type=str, instructions=USER.format(persona=scenario.persona))
        files = f"\nFiles you can send: {', '.join(scenario.docs)}." if scenario.docs else ""
        prompt = (f"It is {now:%A %d %B %Y, %H:%M}. Your goal now: {phase.goal}{files}\n\n"
                  f"The chat so far (you are 'Io'):\n{_transcript(chat)}\n\nYour next message:")
        result = await agent.run(prompt)
        return result.output.strip().strip('"')


def _step(text: str, at: str, scenario: Scenario, buttons: list[str]) -> week.Step | None:
    """The user's message as a harness step: a file, a tap or text. None: a file or button that isn't there."""
    if m := re.fullmatch(r"SEND\s+(.+)", text):
        if (name := m[1].strip()) not in scenario.docs:
            return None
        data, mime = scenario.docs[name]
        return Doc(at, [], name=name, data=data, mime=mime)
    if m := re.fullmatch(r"TAP\s+(.+)", text):
        label = m[1].strip()
        if label not in buttons:
            return None
        return Tap(at, [], message="", button=f"^{re.escape(label)}$")
    return Say(at, [], text=text)


async def play(world: week.World, user: User, scenario: Scenario, log=print) -> dict:
    """One scenario, phase by phase; its row for scenarios.jsonl."""
    chat: list[str] = []
    every = Out(before=world.fingerprint())  # the whole scenario's replies, buttons and runs, for the checks
    turns, verdict, buttons = [], "", []
    for phase in scenario.phases:
        start = week.Say(phase.at, []).when
        chat.append(f"--- {start:%A %H:%M} ---")
        for i in range(MAX_MESSAGES):
            now = start + timedelta(minutes=GAP_MIN * i)
            world.now = now
            text = await user.next(scenario, phase, chat, now)
            if text.startswith("DONE"):
                verdict = text
                chat.append(f"Io: {text}")
                break
            step = _step(text, _at(now), scenario, buttons)
            if step is None:
                chat.append(f"Io: {text}\n(no such file or button)")
                continue
            t0 = time.time()
            try:
                out = await asyncio.wait_for(world.step(step), week.STEP_TIMEOUT_S)
            except TimeoutError:
                out = Out(replies=[f"(no answer in {week.STEP_TIMEOUT_S} s)"])
            buttons = out.buttons
            answer = "\n".join(out.replies) or "(no reply)"
            chat += [f"Io: {text}", f"Assistente ({out.route or '-'}): {answer}"
                     + (f"\n[buttons: {' | '.join(buttons)}]" if buttons else "")]
            every.replies += out.replies
            every.buttons += out.buttons
            every.guards += out.guards
            turns.append({"at": _at(now), "said": text, "route": out.route, "seconds": round(time.time() - t0, 1),
                          "replies": out.replies, "buttons": buttons,
                          "calls": [(c.name, c.ok, c.args[:1000], (c.result or "")[:800])
                                    for g in out.guards for c in g.calls],
                          "guard": [g.reason for g in out.guards if g.reason]})
            log(f"   {_at(now)}  {out.route or '':6} {text[:90]}")
        else:
            verdict = verdict or "(message limit)"
    failed = [c.what for c in scenario.checks if not week._safe(c, world, every)]
    return {"scenario": scenario.name, "ok": not failed, "known": scenario.known, "failed": failed,
            "messages": len(turns), "verdict": verdict, "chat": chat, "turns": turns}


def run(scenarios: list[Scenario] | None = None, user_model: str = "sonnet", model: str = "",
        budget_s: float = BUDGET_S, log=print) -> list[dict]:
    """Each scenario in order on one bot; every scenario's setup runs first."""
    scenarios = SCENARIOS if scenarios is None else scenarios
    base = Settings.from_env()
    settings = dataclasses.replace(base, data_dir=Path(tempfile.mkdtemp(prefix="simuser-")), google=False,
                                   gog_account="", mail_every_min=0, allowed_ids=frozenset({week.USER}),
                                   model=model or base.model)
    where = (f"Claude {settings.claude_model} on {', '.join(sorted(settings.claude_routes))}, else {settings.model}"
             if settings.claude_model and settings.claude_routes else settings.model)
    run_dir = ROOT / "evals" / "runs" / f"simuser-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=True)
    user, rows, started = User(user_model), [], time.time()
    loop = asyncio.new_event_loop()
    with gpu.hold("simuser eval"), (run_dir / "scenarios.jsonl").open("w") as out:
        world = week.World(settings)
        for s in scenarios:
            if s.setup:
                s.setup(world)
        log(f"simuser: {user_model} plays the user; Franziska on {where}; data in {settings.data_dir}\n")
        for s in scenarios:
            if time.time() - started > budget_s:
                rows.append({"scenario": s.name, "ok": False, "known": s.known, "failed": ["skipped: budget"],
                             "messages": 0, "verdict": "", "chat": [], "turns": [], "skipped": True})
                log(f"⏭  {s.name} skipped (budget)")
                continue
            log(f"▶ {s.name}")
            try:
                row = loop.run_until_complete(play(world, user, s, log))
            except Exception as e:  # a crash is a failed scenario, not a failed run
                row = {"scenario": s.name, "ok": False, "known": s.known, "failed": [f"{type(e).__name__}: {e}"],
                       "messages": 0, "verdict": "", "chat": [], "turns": []}
            rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            mark = "✅" if row["ok"] else ("⚠️" if s.known else "❌")
            log(f"{mark} {s.name}: {row['messages']} message(s), user: {row['verdict'] or '-'}")
            for f in row["failed"]:
                log(f"      ✗ {f}")
    text = summary(rows, user_model, where)
    (run_dir / "summary.md").write_text(text + "\n")
    log("\n" + text + f"\n\n{run_dir}")
    return rows


def summary(rows: list[dict], user_model: str, where: str) -> str:
    lines = [f"| simuser: {user_model} plays the user; Franziska on {where} | |", "|---|---|",
             f"| scenarios passed (state) | {sum(r['ok'] for r in rows)}/{len(rows)} |",
             f"| unexpected failures | {sum(not r['ok'] and not r['known'] for r in rows)} |",
             f"| user said ok | {sum(r['verdict'].startswith('DONE ok') for r in rows)}/{len(rows)} |",
             f"| messages | {sum(r['messages'] for r in rows)} |", "", "| scenario | state | messages | user |",
             "|---|---|---|---|"]
    lines += [f"| {r['scenario']} | {'ok' if r['ok'] else '; '.join(r['failed'])} | {r['messages']} | "
              f"{r['verdict'] or '-'} |" for r in rows]
    return "\n".join(lines)


# ---- scenarios ----------------------------------------------------------------------------------------------------
# The first ones are xturn's cases as goals. Each on its own day (MONDAY = 2 Nov 2026), so their dates don't meet.
SCENARIOS: list[Scenario] = [
    Scenario(
        "bill", "Ti è arrivata la bolletta della luce di ACME LUCE (il file bolletta_acme.txt). Ricordi che è "
        "quella della luce, non l'importo né la scadenza: sono nel file.",
        [Phase("mon 09:00", "manda la bolletta e fatti ricordare di pagarla 3 giorni prima della scadenza"),
         Phase("mon 16:00", "fai segnare quella bolletta tra le cose da pagare, così si spunta da sola quando paghi")],
        # xturn checks what life on Claude may see; here the user may type the amount, so only the outcome counts
        [task("bollett|acme|luce|pag", remind_at="2026-11-17"), bill("acme|luce", 8420)],
        docs={"bolletta_acme.txt": (xturn.BILL, "text/plain")}),
    Scenario(
        "budget", "Fai la spesa al supermercato (Esselunga) e a volte mangi fuori. Non sai quanto hai speso.",
        [Phase("tue 10:00", "scopri quanto hai speso al supermercato a ottobre (solo supermercato, non ristoranti) e "
                            "fatti ricordare venerdì di rivedere il budget, con quella cifra nel promemoria")],
        [xturn.task_has("budget", r"146")], setup=xturn.seed),  # October's food & drink, the bread task
    Scenario(
        "plan", "Hai un task 'comprare il pane'. A ottobre hai mangiato fuori qualche volta, non sai quanto.",
        [Phase("wed 09:00", "in un solo messaggio: sposta il task del pane a domani, e se a ottobre hai speso più di "
                            "100 euro in ristoranti fatti ricordare sabato di cucinare di più")],
        [task("pane", due_at="2026-11-05"), task("cucin", due_at="2026-11-07")]),
    Scenario(
        "ponza", "A settembre hai chiesto all'assistente su Telegram se per la gita in barca a Ponza con Giulia serve "
        "lo skipper. Non ricordi la risposta.",
        [Phase("thu 10:00", "fatti dire cosa ti aveva risposto l'assistente sullo skipper per la gita a Ponza")],
        [xturn.called("past_conversations"), reply(r"patente|skipper")],
        setup=xturn.seed_boat),
]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", default="sonnet", help="the Claude model that plays the user")
    p.add_argument("--model", default="", help="override FRANZISKA_MODEL for this run")
    p.add_argument("--only", default="", help="comma-separated scenario names")
    p.add_argument("--list", action="store_true", help="print the scenarios, run nothing")
    p.add_argument("--budget-min", type=float, default=BUDGET_S / 60, help="stop starting scenarios after this long")
    args = p.parse_args()
    chosen = [s for s in SCENARIOS if not args.only or s.name in args.only.split(",")]
    if args.list:
        for s in chosen:
            print(f"{s.name}" + (f"   (known: {s.known})" if s.known else ""))
            for ph in s.phases:
                print(f"   {ph.at}  {ph.goal}")
            for c in s.checks:
                print(f"            · {c.what}")
        return
    rows = run(chosen, args.user, args.model, args.budget_min * 60)
    sys.exit(1 if any(not r["ok"] and not r["known"] for r in rows) else 0)


if __name__ == "__main__":
    main()
