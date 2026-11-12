"""Guards around one agent run (CHANGELOG §3), as a pydantic-ai capability the bot builds per run.

- **Loop guard.** A tool call identical to an earlier one in the same run stops the run when the earlier one was
  rejected (the retry will be rejected again) or was a write that succeeded (a duplicate). A read repeated with no
  write since gets the earlier result back once, with a note, without running again; the next identical repeat
  stops the run. (Stopping on the first repeat killed a run that had its answer: the newsletter was already read,
  monthsim 2026-10-31.) `request_limit` stays as the outer cap.
- **Reply check**, by route (CHECKS): `dates` (the secretary) every time (14:30) and date (28 Oct, 28/10) in the reply
  must be in a tool result of this turn, the user's own message, or today's date; `figures` (finance) every number
  must be in a tool result or the user's message; `links` (web) every link must be in a tool result. The first miss
  asks the model to rewrite the reply once; the second stops the run. Only facts with a fixed written form are checked: phrase lists for what a reply claims ("ho
  salvato", "done") were dropped on 2026-10-30, as they grew with every wording the model found and still leaked. The mail agent has no reply check: its replies quote other
  people ("ho aggiunto il file"), so it is judged on mailbox state instead (evals/mail_eval.py).

- **Taint** (ROADMAP "Safety guardrails" 2–3). A run that has read untrusted content (an email, a Drive file, a
  document the user sent) is tainted. From then on a write is not run: `defer` turns it into a proposal, a button
  the user taps, and the model is told it waits for that tap. The prompt-injection defence that doesn't depend
  on the model behaving: an email saying "archive everything from the bank" can at most produce a button. Drafts
  (never sent) and the agent's own memory (unconfirmed, '(?)') are exempt. One exception: a write the user's own
  message asked for, with those details ("ricordami domani alle 9 di…" after a mail search), runs. That is one AnyJev
  decision (`authorize`) whose input is only the user's words, today's date and the button's label: untrusted text
  never reaches it, so an email can't make its own action pass.

A stopped run raises `Stopped`; the bot replies with `Guard.fallback()`: the tool lines of what did get saved, or
what was looked up (tools and queries, never their raw results). No model is involved in the fallback, so it can't invent anything.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ToolRetryError

from franziska import obs

# Tools that change state, across the secretary, finance, memory, the life MCP server and gog (mail agent).
WRITES = frozenset({
    "add_appointment", "add_task", "edit_task", "set_priority", "complete", "move_appointment",
    "cancel", "remember", "forget",
    "set_budget", "add_bill", "set_payee_category", "record_cash", "edit_cash", "bill_paid", "remove_bill",
    "learn", "forget_memory",
    "gmail_archive_messages", "gmail_create_draft", "gmail_create_label", "gmail_mark_read", "gmail_mark_unread",
    "gmail_modify_messages", "gmail_modify_thread", "gmail_restore_messages", "gmail_trash_messages",
    "gmail_update_draft",
})

# Permission tiers of the writes (ROADMAP "Safety guardrails" 3); code reads them, not the prompt. Nothing is
# irreversible today: mail never sends, trash is recoverable, Docs and Sheets keep version history.
DRAFT = frozenset({"gmail_create_draft", "gmail_update_draft"})  # nothing leaves until the user sends it
MEMORY = frozenset({"learn", "forget_memory"})  # the agent's own notes; unconfirmed ones show '(?)'
EXTERNAL = frozenset({  # outside the assistant's own stores
    "gmail_archive_messages", "gmail_create_label", "gmail_mark_read", "gmail_mark_unread", "gmail_modify_messages",
    "gmail_modify_thread", "gmail_restore_messages", "gmail_trash_messages", "docs_write", "sheets_update_range",
})
# Reads whose content someone other than the user wrote: after one, the run is tainted.
UNTRUSTED = frozenset({
    "gmail_search", "gmail_get_message", "gmail_get_thread", "gmail_get_draft", "drive_search", "drive_get",
    "docs_get", "sheets_read_range", "calendar_events", "read_attachment", "index_attachments",
    "web_search", "news", "read_page", "search_documents", "read_document",
    "read_file", "service_log",  # pc.py: a file on the computer, a service's log
    "past_conversations",  # other assistants' answers, which quote mail and pages (privacy.SEES widened it)
})
WRITES = WRITES | EXTERNAL  # docs_write and sheets_update_range were missing from the list above


def needs_tap(name: str) -> bool:
    """A write a tainted run may not do on its own."""
    return name in WRITES and name not in DRAFT | MEMORY


def label(name: str, args: dict) -> str:
    """The button for a deferred write: what it will do, in a few words."""
    def ids() -> int:
        found = args.get("message_ids") or args.get("ids") or args.get("thread_ids") or []
        return len(found) if isinstance(found, list) else 1
    when = str(args.get("starts_at") or args.get("due_at") or "")[:16].replace("T", " ")
    text = {
        "add_task": f"➕ {args.get('title', 'task')}" + (f" · {when}" if when else ""),
        "add_appointment": f"📅 {args.get('title', 'appointment')} · {when}",
        "edit_task": f"✏️ Change #{args.get('item_id')}", "set_priority": f"✏️ #{args.get('item_id')} {args.get('priority')}",
        "complete": f"✅ Done #{args.get('item_id')}", "cancel": f"🗑 Remove #{args.get('item_id')}",
        "move_appointment": f"📅 Move #{args.get('item_id')} · {when}",
        "remember": f"📝 Note: {args.get('subject', '')}", "forget": f"🗑 Forget note {args.get('note_id')}",
        "add_bill": f"➕ Bill {args.get('payee', '')} {args.get('amount_eur', '')} €",
        "record_cash": f"💶 Cash {args.get('amount_eur', '')} €", "set_budget": f"💶 Budget {args.get('category', '')}",
        "edit_cash": f"💶 Fix cash {args.get('amount_eur', '')} € on {args.get('on', '')}",
        "bill_paid": f"✅ Bill {args.get('bill_id')} paid", "remove_bill": f"🗑 Remove bill {args.get('bill_id')}",
        "set_payee_category": f"🏷 {args.get('payee', '')} → {args.get('category', '')}",
        "gmail_archive_messages": f"🗄 Archive {ids()} email(s)", "gmail_trash_messages": f"🗑 Trash {ids()} email(s)",
        "gmail_mark_read": f"✉️ Mark {ids()} read", "gmail_mark_unread": f"✉️ Mark {ids()} unread",
        "gmail_restore_messages": f"↩️ Restore {ids()} email(s)", "gmail_create_label": f"🏷 New label {args.get('name', '')}",
        "docs_write": "📝 Append to the Doc", "sheets_update_range": f"📊 Update {args.get('range', 'the Sheet')}",
    }.get(name, f"✅ {name}")
    return text if len(text) <= 60 else text[:59] + "…"


_MONTHS = {"jan": 1, "gen": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "mag": 5, "jun": 6, "giu": 6, "jul": 7,
           "lug": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "ott": 10, "nov": 11, "dec": 12, "dic": 12}
_MONTH = "|".join(_MONTHS)
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:di\s+)?({_MONTH})[a-zà]*\b", re.I)
_MONTH_DAY = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december)"
                        r"\s+(\d{1,2})\b", re.I)  # English only: 'mar 28' is Italian for Tuesday the 28th
_ISO = re.compile(r"\b20\d\d-(\d\d)-(\d\d)\b")
_SLASH = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_LINK = re.compile(r"https?://[^\s)\]>\"']+")
# A weekday the user names, Italian or English: lunedì → 0 … domenica → 6.
_WEEKDAYS = [re.compile(rf"\b(?:{w})\b", re.I) for w in (
    r"luned[iì]|monday", r"marted[iì]|tuesday", r"mercoled[iì]|wednesday", r"gioved[iì]|thursday",
    r"venerd[iì]|friday", r"sabato|saturday", r"domenica|sunday")]
DAY_ARGS = ("starts_at", "due_at")  # not remind_at: 'giovedì, ricordamelo il giorno prima' is another day


def named_weekday(text: str) -> int | None:
    """The one weekday the message names (0 = Monday); None for none or several ('da venerdì a lunedì')."""
    named = [i for i, w in enumerate(_WEEKDAYS) if w.search(text)]
    return named[0] if len(named) == 1 else None


_HINT = re.compile(r" \([^()]*\bthe user\b[^()]*\)")  # tool text addressed to the model: '(priority … was guessed…)'


# The reply checks each route's agent gets. Chat has no tools to back anything; mail: see the module docstring.
# `searched` (web): a reply with no tool call this turn is sent back once to search first; it asked the user for
# their tastes or their phone plan instead of searching (monthsim 2026-10-31: Testaccio, Iliad, a gift).
CHECKS: dict[str, tuple[str, ...]] = {"life": ("dates",), "money": ("figures",), "web": ("links", "searched")}
# Search tools: after SEARCH_NOTE calls of one of them in a run its results carry a note that "not found" is an
# answer; the call after SEARCH_STOP of them stops the run. Seven gmail_search for a flight that isn't in the
# mailbox ran to the 300 s limit (monthsim 2026-10-31). Paged reads (read_attachment, read_document) aren't here.
SEARCHES = frozenset({"gmail_search", "drive_search", "search_documents", "search_attachment", "web_search", "news",
                      "recall", "query_ledger", "calendar_events", "past_conversations", "read_memory"})
SEARCH_NOTE, SEARCH_STOP = 4, 8
# Tools whose results are what the agent learned or was told before, not data of this turn.
MEMORY_TOOLS = frozenset({"read_memory", "learn", "forget_memory", "past_conversations"})
# Another assistant's answer (delegation.py): its prose is not evidence; its tool results are (Guard.sources).
RELAYED = frozenset({"ask"})




# Tools whose result is public and written for people (titles, links, snippets): a stopped run shows it.
READABLE = frozenset({"web_search", "news"})
READABLE_CHARS = 1500


class Stopped(Exception):
    """A guard stopped the run; `Guard.fallback()` is the reply."""


@dataclass
class Call:
    name: str
    args: str
    ok: bool
    result: str

    @property
    def wrote(self) -> bool:
        return self.ok and self.name in WRITES and not self.result.startswith(("proposed", "already"))


def dates(text: str) -> set[tuple[int, int]]:
    """(day, month) of every date written in `text`, in the shapes the tools and the models use."""
    found = {(int(d), _MONTHS[m[:3].lower()]) for d, m in _DAY_MONTH.findall(text)}
    found |= {(int(d), _MONTHS[m[:3].lower()]) for m, d in _MONTH_DAY.findall(text)}
    found |= {(int(d), int(m)) for m, d in _ISO.findall(text)}
    found |= {(int(d), int(m)) for d, m in _SLASH.findall(text)}
    return {(d, m) for d, m in found if 1 <= d <= 31 and 1 <= m <= 12}


_NUMBER = re.compile(r"\d+")  # digit runs: '410,00 €' and 'EUR 410,00' share 410 and 00


def times(text: str) -> set[tuple[int, int]]:
    return {(int(h), int(m)) for h, m in _TIME.findall(text)}


def unsupported(reply: str, evidence: str, checks: tuple[str, ...] = ("dates",), grounded: bool = True) -> list[str]:
    """What the reply states that `evidence` (tool results, the user's message, today) doesn't back. `grounded`: a
    tool returned data this turn."""
    problems = []
    if "dates" in checks:
        problems += [f"the time {h}:{m:02d}" for h, m in sorted(times(reply) - times(evidence))]
        problems += [f"the date {d}/{m}" for d, m in sorted(dates(reply) - dates(evidence))]
    # A figure the user's message holds (a document sent to the bot, an amount they said) is backed too: a bill photo
    # described with no tool call was refused (real 26B, 2026-10-30).
    if "figures" in checks and not grounded and set(_NUMBER.findall(reply)) - set(_NUMBER.findall(evidence)):
        problems.append("numbers (no tool returned data: call one before giving a figure)")
    if "links" in checks:
        problems += [f"the link {u}" for u in sorted(links(reply) - links(evidence))]
    return problems


def links(text: str) -> set[str]:
    return {u.rstrip(".,;:!?") for u in _LINK.findall(text)}


class Guard(AbstractCapability):
    def __init__(self, text: str, now: datetime, checks: tuple[str, ...] = (), tainted: bool = False,
                 defer: Callable[[str, dict], object | None] | None = None,
                 authorize: Callable[[str], Awaitable[bool]] | None = None):
        """`tainted`: the request itself carries untrusted content (a document). `defer(tool, args)` makes a
        proposal of a write (None: it can't, so the write runs); without it the taint is only recorded.
        `authorize(label)`: whether the user's own message asked for that write (Core._authorizer); if so, a tainted
        run does it instead of making a button."""
        self.text, self.now, self.checks = text, now, checks
        self.authorize = authorize
        # Set by the core after the run's buttons are known (Core._turn): the proposals waiting for a tap, and which
        # of their labels a reply says were done (an AnyJev decision). A reply claiming one is sent back once.
        self.pending: Callable[[], list] | None = None
        self.claimed: Callable[[str, list[str]], Awaitable[list[str]]] | None = None
        self._claims_checked = False
        self._search_asked = False
        self.calls: list[Call] = []
        self.reason = ""
        self._rewrites = 0
        self.tainted, self.defer = tainted, defer
        self.deferred: list = []  # proposals made of writes this run wasn't allowed to do
        self.sources: list[str] = []  # tool results of the assistants this run asked (delegation.py)
        # The route's own earlier turns: the user's messages (a document they sent) and the tool results. A figure from
        # there is backed: 'quanto era l'importo?' after a bill was read was stopped as invented (2026-11-12).
        self.earlier = ""
        self._weekday_checked = False
        self._repeated: set[tuple[str, str]] = set()  # reads already answered once from an earlier identical call
        self.retries: list[str] = []  # what the model was sent back for, in order (evals record these)

    def _retry(self, what: str, message: str) -> ModelRetry:
        self.retries.append(what)
        return ModelRetry(message)

    # ---- loop guard and taint --------------------------------------------------------------------
    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        key = json.dumps(args, sort_keys=True, default=str)
        if (again := self._check_loop(call.tool_name, key)) is not None:
            self.calls.append(Call(call.tool_name, key, True, again))
            return again
        if "dates" in self.checks:
            self._check_weekday(args)
        if self.tainted and self.defer and needs_tap(call.tool_name):
            proposal = self.defer(call.tool_name, dict(args))
            if proposal is not None and self.authorize and await self.authorize(proposal.label):
                obs.event("guard.authorized", tool=call.tool_name, label=proposal.label)  # the user asked: it runs
            elif proposal is not None:
                obs.event("guard.defer", tool=call.tool_name, label=proposal.label)
                self.deferred.append(proposal)
                result = (f"proposed, not done: the user's message didn't ask for exactly this, and this request "
                          f"involves content from email, a page or a document, or came from another assistant, so "
                          f"the user confirms it. A button '{proposal.label}' waits for their tap. Say it is ready "
                          "to confirm; don't say it was done.")
                self.calls.append(Call(call.tool_name, key, True, result))
                return result
        try:
            result = await handler(args)
        except ToolRetryError:  # the tool rejected the arguments (LifeError / MoneyError → ModelRetry)
            self.calls.append(Call(call.tool_name, key, False, ""))
            raise
        if call.tool_name in SEARCHES:
            result = self._search_budget(call.tool_name, result)
        self.calls.append(Call(call.tool_name, key, True, str(result)))
        # run_python over library documents prints what someone else wrote, like read_document.
        untrusted = call.tool_name in UNTRUSTED or (call.tool_name == "run_python" and bool(args.get("documents")))
        if untrusted and not self.tainted:
            obs.event("guard.tainted", tool=call.tool_name)
            self.tainted = True
        return result

    def _search_budget(self, name: str, result):
        """The SEARCH_NOTE-th search of a kind and later carry the note; past SEARCH_STOP the run stops."""
        n = 1 + sum(1 for c in self.calls if c.ok and c.name == name)
        if n > SEARCH_STOP:
            self.reason = f"{name} called {n} times without an answer"
            obs.event("guard.searches", tool=name, n=n)
            raise Stopped(self.reason)
        if n < SEARCH_NOTE:
            return result
        note = (f"(This is search {n} with {name} this turn. If what you are looking for isn't in these results, it "
                "is probably not there: tell the user you didn't find it, and what you searched, instead of "
                "searching again.)")
        if isinstance(result, str):
            return f"{result}\n{note}"
        if isinstance(result, dict):
            return {**result, "note": note}
        return result

    def _check_loop(self, name: str, key: str) -> str | None:
        """None: the call runs. A string: the earlier identical read's result, returned instead of running it."""
        for i in range(len(self.calls) - 1, -1, -1):
            prev = self.calls[i]
            if (prev.name, prev.args) != (name, key):
                continue
            if prev.ok and not prev.wrote and any(c.wrote for c in self.calls[i + 1:]):
                return None  # again after a write: the result may have changed
            if prev.ok and name not in WRITES and (name, key) not in self._repeated:
                self._repeated.add((name, key))
                self.retries.append(f"{name} repeated: earlier result returned")
                obs.event("guard.repeat", tool=name, args=key[:500])
                return (f"{prev.result}\n(You already called {name} with exactly these arguments in this turn; this "
                        "is the same result. Answer from it: if it doesn't hold what you need, say so, or try "
                        "different arguments.)")
            self.reason = f"{name} called again with the same arguments"
            obs.event("guard.loop", tool=name, args=key[:500])
            raise Stopped(self.reason)
        return None

    def _check_weekday(self, args: dict) -> None:
        """The user said 'venerdì' and the call writes a Saturday: the model miscounted (it did, with the agenda
        showing Friday's date). Sent back once per run with the right date, not recorded as a rejected call, so a
        deliberate repeat goes through."""
        if self._weekday_checked or (want := named_weekday(self.text)) is None:
            return
        for name in DAY_ARGS:
            value = args.get(name)
            if isinstance(value, str):
                try:
                    value = date.fromisoformat(value[:10])
                except ValueError:
                    continue
            if isinstance(value, datetime):
                value = value.date()
            if isinstance(value, date) and value.weekday() != want:
                obs.event("guard.weekday", arg=name, value=str(value), said=want)
                self._weekday_checked = True
                day = self.now.date() + timedelta(days=(want - self.now.weekday()) % 7 or 7)
                raise self._retry(f"weekday: {name} {value:%Y-%m-%d}",
                                  f"{name} {value:%Y-%m-%d} is a {value:%A}, but the user said {day:%A}: the next "
                                  f"{day:%A} is {day:%Y-%m-%d}. Call again with the day they meant.")

    # ---- reply check -----------------------------------------------------------------------------
    async def after_output_process(self, ctx, *, output_context, output):
        if not isinstance(output, str):
            return output
        await self._check_claims(output)
        if not self.checks:
            return output
        grounded = any(c.ok and c.name not in MEMORY_TOOLS for c in self.calls)
        if "searched" in self.checks and not any(c.ok for c in self.calls) and not self._search_asked:
            self._search_asked = True  # once; its own count, so a link check after it still gets its rewrite
            obs.event("guard.rewrite", problems=["no search"], reply=output[:500])
            raise self._retry("no search",
                              "You answered without searching. Search first (web_search, news or read_page) with "
                              "what the user wrote, then answer from the results. Ask the user something only if the "
                              "results can't answer without it.")
        problems = unsupported(output, self.evidence(), self.checks, grounded)
        if not problems:
            return output
        self._rewrites += 1
        obs.event("guard.stop" if self._rewrites > 1 else "guard.rewrite", problems=problems, reply=output[:500])
        if self._rewrites > 1:
            self.reason = "reply not backed by the tools: " + "; ".join(problems)
            raise Stopped(self.reason)
        raise self._retry(
            "unbacked: " + "; ".join(problems),
            f"Your reply states {'; '.join(problems)}, but no tool result in this turn shows it. Rewrite the reply "
            "saying only what the tool results above say. Call a tool only if the information is missing; never "
            "repeat a call that already succeeded.")

    async def _check_claims(self, output: str) -> None:
        """Once per run: the reply says a write was done that is only a button. The user then doesn't tap, and nothing
        is saved (monthsim 2026-10-31: "ho impostato il promemoria" on a ➕). An AnyJev decision per waiting label,
        not phrase lists (those were dropped on 2026-10-30)."""
        if self._claims_checked or not (self.pending and self.claimed):
            return
        labels = [p.label for p in self.pending()]
        if not labels:
            return
        self._claims_checked = True
        said = await self.claimed(output, labels)
        if said:
            obs.event("guard.claim", labels=said, reply=output[:500])
            raise self._retry(
                "claimed done: " + "; ".join(said),
                f"Your reply says this was done, but it was not: {'; '.join(said)}. It waits for the user to tap its "
                "button under your reply. Rewrite the reply: say it is ready and they confirm it with the button. "
                "Don't call any tool.")

    def evidence(self) -> str:
        return "\n".join([self.text, f"{self.now:%Y-%m-%d %H:%M}", self.earlier,
                          *(c.result for c in self.calls if c.ok and c.name not in RELAYED), *self.sources])

    # ---- the reply when a guard stopped the run ---------------------------------------------------
    def fallback(self) -> str:
        if writes := [c for c in self.calls if c.wrote]:
            return "\n".join(_HINT.sub("", c.result) for c in writes)
        if proposed := [c for c in self.calls if c.ok and c.result.startswith("proposed ")]:  # a 💾 waiting for a tap
            return "\n".join(_HINT.sub("", c.result).removeprefix("proposed ") for c in proposed)
        if reads := [c for c in self.calls if c.ok]:
            if reads[-1].name in READABLE:  # a list of public results written for people: worth showing
                found = _HINT.sub("", reads[-1].result)
                found = found if len(found) <= READABLE_CHARS else found[:READABLE_CHARS] + "…"
                return f"I couldn't finish this, but here is what I found:\n{found}"
            # Otherwise what was looked up, not the raw result: rows of [[None]] and 48 KB of mail JSON reached the
            # user (monthsim 2026-10-31).
            return ("I couldn't finish this. I looked with " + _looked(reads)
                    + ". Nothing was changed; try asking more narrowly or another way.")
        return "I got stuck and stopped. Nothing was changed; try saying it another way."


def _looked(calls: list[Call]) -> str:
    """'gmail_search ×3 ("volo vienna", "Austrian") and agenda': each tool with its count and first few queries."""
    by: dict[str, list[str]] = {}
    for c in calls:
        try:
            args = json.loads(c.args)
        except ValueError:
            args = {}
        texts = [v for v in args.values() if isinstance(v, str) and v.strip()] if isinstance(args, dict) else []
        text = max(texts, key=len, default="")  # the query, not a short option like lang='it' 
        by.setdefault(c.name, []).append(" ".join(text.split())[:50])
    parts = []
    for name, queries in by.items():
        shown = list(dict.fromkeys(q for q in queries if q))[:3]
        parts.append(name + (f" ×{len(queries)}" if len(queries) > 1 else "")
                     + (" (" + ", ".join(f'"{q}"' for q in shown) + ")" if shown else ""))
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]


def run_sync(agent, text: str, route: str, now: datetime, **kwargs) -> str:
    """One turn outside the bot (the CLIs), with the guards the bot gives this route: the reply, or the fallback."""
    guard = Guard(text, now, CHECKS.get(route, ()))
    try:
        return agent.run_sync(text, capabilities=[guard], **kwargs).output
    except Stopped:
        return guard.fallback()
