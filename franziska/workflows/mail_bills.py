"""Bills and notices in the mailbox over a period: 'trova le bollette e gli avvisi del condominio dell'ultimo mese'.

1. search   Gmail conversations: the user's topic when they name one, otherwise words that bills use
            (promotions and social skipped), plus the conversations the triage job already called a bill or an
            action, whatever their words; one request per thread for every subject in it
2. group    threads with the same subject once Re:/Fwd:/PEC prefixes are stripped (the same notice sent three
            times, a forward, the PEC copy). The oldest message is the original
3. decide   cheapest first. About the topic? A subject containing the topic word is, with no model call; only
            the others get an AnyJev yes/no on sender + subject. Then bill / notice / other, one AnyJev choice
            on sender + subject + snippet
4. read     one request per thread for every body; the original in full, replies without their quoted text.
            Attachments are downloaded one by one (bills and notices first by name) only until the text budget
            is full; images, signatures and PEC envelopes never
5. extract  issuer, a one-line summary, what the replies add, amount and date: one typed call per conversation.
            An amount that is not in the text is dropped rather than shown
6. keep     every readable attachment of a bill or notice (up to 6 a conversation, also past the text budget) goes
            into the document library, kept, class 'mail'
7. report   upcoming by date, past notices in a short 'earlier' list, bills with the same amount and date
            merged, and a ➕ button per bill still to pay to track it in finance

Memory (franziska/memory.py), in code, for any topic: the addresses in the profile entry of that name (when it is
one of memory.WELL_KNOWN: the condominium, utilities) and in the learned mail/<topic>-sender widen the Gmail query
and put their mail on topic without a model call. Each run learns who the topic's mail came from ('luce e gas' →
mail/luce-e-gas-sender), and for a well-known topic proposes its profile entry (💾) when it isn't saved yet.

Everything runs on the local model; nothing of the mail leaves the machine.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from pydantic import BaseModel, Field

from franziska import documents, memory
from franziska.finance.service import eur
from franziska.workflows import Ctx, Proposal, Report, Workflow

log = logging.getLogger(__name__)

# Without a topic: words bills use. Generic ones ('avviso', 'notice', 'payment') matched every job alert.
KEYWORDS = ('bolletta bollettino fattura rata scadenza sollecito pagopa f24 tari imu canone condominio '
            '"avviso di pagamento" "da pagare" invoice')
MAX_THREADS = 150
MAX_READ = 20
ORIGINAL_CHARS = 6000  # the first message with its attachments: ~2k tokens
REPLY_CHARS = 600  # each reply, quoted text removed
REPLIES = 6  # at most this many replies per conversation, the newest
CONDO = "condomin"  # condominio, condominiale, condominium: its memory keeps the English, well-known name
MAX_SENDERS = 5  # addresses kept in mail/<topic>-sender

KINDS = {
    "bill": "bill: asks the recipient to pay an amount that is still to be paid (bollettino, rata, F24, a utility "
            "bill to pay)",
    "notice": "notice: an official communication from an administrator, a utility, an office or a landlord, with "
              "something to know or do (a meeting, works, access to the flat, a deadline)",
    "other": "other: job offers and job alerts, newsletters, marketing, terms-of-service updates, receipts and "
             "invoices of payments already made, personal mail",
}

_PREFIX = re.compile(r"^\s*((re|r|fwd|fw|i|inoltro|rif|posta certificata|anomalia messaggio)\s*:\s*)+", re.I)
MAX_ATTACHMENTS = 6  # downloaded per conversation
_READABLE = (".pdf", ".txt")  # attachments worth downloading; the rest (images, .p7s, daticert.xml, .eml) are not
_FIRST = re.compile(r"bollett|avviso|rata|fattur|convocaz|scadenz|pagament", re.I)  # likely the one that matters
# Where a reply's quote of the earlier message starts (Gmail, Outlook, Apple Mail; Italian and English).
_QUOTE = re.compile(r"^(>|Il giorno .+ ha scritto|On .+ wrote|-{2,}\s*(Original Message|Messaggio originale|"
                    r"Forwarded message|Messaggio inoltrato)|Da: |From: )", re.I | re.M)


class Params(BaseModel):
    days: int = Field(30, ge=1, le=365, description="How many days back to look ('ultimo mese' = 30)")
    about: str | None = Field(None, description="Only mail about this, in the user's words, e.g. 'condominio' or "
                                                "'luce e gas'; null for every bill and notice")


class Found(BaseModel):
    issuer: str = Field(description="Who is paid or who it is from. For anything about a building, the "
                                    "condominium's name ('Condominio Via Roma 12'), never the administrator's "
                                    "studio; otherwise the company or office, e.g. 'Enel Energia'")
    summary: str = Field(description="One short sentence in Italian: what the original message is and what the "
                                     "user has to do. Say when it is paid by direct debit (addebito diretto / SEPA), "
                                     "and give every date a meeting has (prima and seconda convocazione)")
    replies: str | None = Field(None, description="If there are replies: one short sentence in Italian on what they "
                                                  "add (a question, a dispute, a correction); null if none or "
                                                  "nothing new")
    amount_eur: float | None = Field(None, description="The amount still to pay in euros, if the text states one")
    due_on: date | None = Field(None, description="The payment deadline, or the date of the meeting or works; the "
                                                  "latest one if several")


EXTRACT = ("You read one email conversation for the user: the original message with its attachments, then any "
           "replies. Fill the fields from the text only: when it doesn't state an amount or a date, leave it null. "
           "Never compute or guess an amount.")


def run(ctx: Ctx, p: Params) -> Report:
    calls = ctx.gmail.client.calls
    topic = f" about {p.about}" if p.about else ""
    words = [w for w in re.findall(r"\w+", p.about or "") if len(w) > 2]
    key = _topic_key(p.about) if p.about else None
    senders = _senders(ctx, key) if key else []
    terms = " ".join(words + [f"from:{a}" for a in senders])
    query = f"newer_than:{p.days}d -category:promotions -category:social {{{terms or KEYWORDS}}}"
    threads = ctx.gmail.thread_ids(query, limit=MAX_THREADS)
    if not p.about and ctx.mail is not None:  # the keywords are recall in Italian; the triage read every subject
        since = (ctx.now() - timedelta(days=p.days)).isoformat(timespec="minutes")
        triaged = [t for t in ctx.mail.threads(("bill", "action"), since) if t not in threads]
        threads += triaged[:max(0, MAX_THREADS - len(threads))]
    groups = _conversations({t: ctx.gmail.thread_headers(t) for t in threads}, p.days, ctx.now())
    n_mails = sum(len(g.headers) for g in groups)
    ctx.progress(f"🔎 {n_mails} emails ({len(groups)} conversations) in the last {p.days} days; sorting them…")

    from anyjev import Question

    kind_q = Question.choice("What is this email for its recipient?", list(KINDS.values()), name="kind")
    about_q = Question.noul(f"Is this email about {p.about}, or could it be?", name="about") if p.about else None
    stems = [w.lower()[:max(4, len(w) - 2)] for w in words]  # condominio → condomin: also condominiale
    kept, asked, dropped = [], 0, []
    for g in groups:
        h = g.headers[0]
        on_topic = any(st in h.subject.lower() for st in stems) or h.address in senders
        if about_q and not on_topic:
            asked += 1
            about_p = ctx.router.decide(f"From: {h.sender}\nSubject: {h.subject}\n\n{h.snippet}",
                                        [about_q])["about"].p_true
            if about_p < 0.5:
                log.info("mail_bills %r: off topic p=%.2f", h.subject[:60], about_p)
                dropped.append(f"{h.sender} · {h.subject[:60]}")
                continue
        d = ctx.router.decide(f"From: {h.sender}\nSubject: {h.subject}\n\n{h.snippet}", [kind_q])["kind"]
        kind = next(k for k, v in KINDS.items() if v == d.argmax)
        log.info("mail_bills %r (%d msgs): %s p=%.2f", h.subject[:60], len(g.headers), kind, d.confidence)
        if kind != "other":
            kept.append((g, kind))
        else:
            dropped.append(f"{h.sender} · {h.subject[:60]} (not a bill or notice)")
    notes = f"Gmail query: {query}\nSet aside as off topic or not a bill/notice: " + ("; ".join(dropped[:25]) or "none")
    if not kept:
        return Report(f"No bills or notices{topic} in the last {p.days} days ({n_mails} emails checked).", notes=notes)

    items = []  # (original header, kind, Found, number of messages)
    for i, (g, kind) in enumerate(kept[:MAX_READ], 1):
        ctx.progress(f"📖 Reading {i} of {min(len(kept), MAX_READ)}: {g.headers[0].subject[:50]}")
        text = _conversation_text(ctx, g)
        found = ctx.extract(Found, EXTRACT, text)
        if found.amount_eur is not None and not _mentions(text, found.amount_eur):
            log.warning("mail_bills %r: amount %s not in the text, dropped", g.headers[0].subject[:60],
                        found.amount_eur)
            found.amount_eur = None
        items.append((g.headers[0], kind, found, len(g.headers)))
    log.info("mail_bills: %d conversations, %d topic questions to the model, %d read, %d Gmail requests",
             len(groups), asked, len(items), ctx.gmail.client.calls - calls)
    report = _report(ctx, p, items, checked=n_mails, capped=len(threads) >= MAX_THREADS,
                     skipped=max(0, len(kept) - MAX_READ))
    report.notes = notes
    if ctx.memory is not None and key:
        _learn_senders(ctx, key, p.about, stems, items, report)
    return report


def _topic_key(about: str) -> str | None:
    """The memory name of a topic: 'condominio' → 'condominium' (a well-known profile name), else its slug
    ('luce e gas' → 'luce-e-gas'); None for a topic with no usable name (then nothing is learned)."""
    if CONDO in about.lower():
        return "condominium"
    try:
        return memory.slug(about)
    except memory.MemoryRefused:
        return None


def _senders(ctx: Ctx, key: str) -> list[str]:
    """The topic's addresses: those in its profile entry (a well-known one), then those learned from earlier runs."""
    if ctx.memory is None:
        return []
    profile = ctx.memory.get("profile", key) if key in memory.WELL_KNOWN else None
    found = memory.emails(profile) + memory.emails(ctx.memory.get("mail", f"{key}-sender"))
    return list(dict.fromkeys(found))


def _learn_senders(ctx: Ctx, key: str, about: str, stems: list[str], items: list, report: Report) -> None:
    """Learn who the topic's mail comes from; for a well-known topic, propose its profile entry or say it's missing.

    A conversation is the topic's own when its issuer or subject names the topic. A sender counts when it sent such
    a bill or started two such conversations: a resident who forwarded one notice doesn't."""
    own = [(h.address, kind, f.issuer) for h, kind, f, n in items
           if h.address and any(st in f"{f.issuer} {h.subject}".lower() for st in stems)]
    seen = Counter(a for a, _, _ in own)
    new = [a for a, kind, _ in own if kind == "bill" or seen[a] > 1]
    name = f"{key}-sender"
    learned = memory.emails(ctx.memory.get("mail", name, touch=False))
    if new:
        addresses = list(dict.fromkeys(learned + new))[:MAX_SENDERS]
        more = " (and others, in the detail)" if len(addresses) > 1 else ""  # no count: a new address confirms
        topic = "condominium" if key == "condominium" else about.strip()
        link = f" The {key}: [[{key}]]." if key in memory.WELL_KNOWN else ""
        ctx.memory.learn("mail", name, f"{topic} mail comes from {addresses[0]}{more}",
                         f"Addresses: {', '.join(addresses)}. Seen on: {own[0][2]}.{link}",
                         source=f"mail_bills {ctx.now():%Y-%m-%d}")
    if key not in memory.WELL_KNOWN or ctx.memory.get("profile", key, touch=False) is not None:
        return
    issuers = {issuer for _, _, issuer in own}
    if len(issuers) == 1:  # one issuer for the topic: a plausible profile fact, saved only on a tap
        issuer, sender = issuers.pop(), new[0] if new else ""
        line = f"{issuer}; mail comes from {sender}" if sender else issuer
        if key == "condominium" and sender:
            line = f"{issuer}; the administrator writes from {sender}"
        report.proposals.append(Proposal(
            f"💾 Save to profile: {key} = {issuer}"[:64],
            lambda: "💾 Saved to your profile: " + ctx.memory.set_profile(
                key, line, source=f"mail_bills, confirmed {ctx.now():%Y-%m-%d}").name))
    elif key == "condominium":
        report.text += ("\n\nNon so qual è il tuo condominio: scrivimi 'il mio condominio è Condominio Via …, "
                        "l'amministratore scrive da …'.")


@dataclass
class _Group:
    threads: list[str]
    headers: list  # MailHeader, oldest first, only those inside the period


def _subject_key(subject: str) -> str:
    return " ".join(_PREFIX.sub("", subject).lower().split())


def _conversations(threads: dict[str, list], days: int, now) -> list[_Group]:
    """Threads with the same subject merged into one group, keeping the messages inside the period."""
    since = (now - timedelta(days=days)).isoformat(timespec="minutes")
    groups: dict[str, _Group] = {}
    for thread_id, headers in threads.items():
        headers = [h for h in headers if h.received_at >= since] or headers[-1:]
        if not headers:
            continue
        key = _subject_key(headers[0].subject)
        g = groups.setdefault(key, _Group([], []))
        g.threads.append(thread_id)
        g.headers = sorted(g.headers + headers, key=lambda h: h.received_at)
    return list(groups.values())


def _keep(ctx: Ctx, a, text: str, data: bytes) -> None:
    """The attachment of a conversation the workflow called a bill or a notice goes into the library, kept (not
    expired), class 'mail': later questions find it with search_documents. A failure here never stops the report."""
    if ctx.library is None or not text.strip():
        return
    try:
        ctx.library.add(a.filename, text, f"mail:{a.message_id}", "mail", a.mime, data, kept=True)
    except Exception:
        log.exception("library: could not keep %s", a.filename)


def _conversation_text(ctx: Ctx, g: _Group) -> str:
    wanted = {h.id for h in g.headers}
    msgs = sorted((m for t in g.threads for m in ctx.gmail.thread(t) if m.header.id in wanted),
                  key=lambda m: m.header.received_at)
    original, replies = msgs[0], msgs[1:][-REPLIES:]
    h = original.header
    text = f"From: {h.sender}\nSubject: {h.subject}\nDate: {h.received_at}\n\n{original.text}"[:ORIGINAL_CHARS]
    read = []
    for a in sorted((a for a in original.attachments if a.filename.lower().endswith(_READABLE)),
                    key=lambda a: not _FIRST.search(a.filename))[:MAX_ATTACHMENTS]:
        full = len(text) >= ORIGINAL_CHARS
        if full and ctx.library is None:
            break
        data = ctx.gmail.download(a)
        t = documents.text_of(a.filename, a.mime, data)
        read.append(f"{a.filename} ({len(t)} chars)")
        _keep(ctx, a, t, data)  # every attachment of a bill or notice, also the ones past the text budget
        if not full:
            text = (text + f"\n\n--- attachment {a.filename} ---\n{t}")[:ORIGINAL_CHARS]
    for r in replies:
        body = r.text[:m.start()] if (m := _QUOTE.search(r.text)) else r.text
        text += f"\n\n--- reply from {r.header.sender}, {r.header.received_at} ---\n{body.strip()[:REPLY_CHARS]}"
    log.info("mail_bills %r: %d message(s), %d repl%s, attachments read: %s (of %d)", h.subject[:60], len(msgs),
             len(replies), "y" if len(replies) == 1 else "ies", ", ".join(read) or "none", len(original.attachments))
    return text


def _mentions(text: str, amount: float) -> bool:
    """Whether the amount appears in the text, in any of the usual ways of writing it (864, 864,00, 1.024,50 …)."""
    whole, cents = f"{amount:.2f}".split(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    forms = {f"{whole},{cents}", f"{whole}.{cents}", f"{grouped},{cents}", f"{int(whole):,}.{cents}"}
    if cents == "00":
        forms |= {whole, grouped}
    flat = text.replace(" ", " ")
    return any(re.search(rf"(?<![\d.,]){re.escape(f)}(?![\d]|[.,]\d)", flat) for f in forms)


def _report(ctx: Ctx, p: Params, items: list, checked: int, capped: bool, skipped: int) -> Report:
    today = ctx.now().date()
    seen, upcoming, earlier = set(), [], []
    for h, kind, f, n in sorted(items, key=lambda x: x[0].received_at, reverse=True):  # newest copy of a bill wins
        if kind == "bill" and f.amount_eur is not None:
            if (f.amount_eur, f.due_on) in seen:
                continue
            seen.add((f.amount_eur, f.due_on))
        past_notice = kind == "notice" and f.due_on is not None and f.due_on < today
        (earlier if past_notice else upcoming).append((h, kind, f, n))
    upcoming.sort(key=lambda x: (x[2].due_on or date.max, x[0].received_at))

    topic = f" about {p.about}" if p.about else ""
    lines = [f"🧾 Bills and notices{topic}, last {p.days} days ({len(upcoming) + len(earlier)} from {checked} emails):"]
    proposals, total = [], 0
    for h, kind, f, n in upcoming:
        icon = "💶" if kind == "bill" else "📢"
        money = f" · {eur(round(f.amount_eur * 100))}" if f.amount_eur is not None else ""
        due = f" · {'due' if kind == 'bill' else 'on'} {f.due_on:%a %d %b}" if f.due_on else ""
        late = " ⚠️ past" if kind == "bill" and f.due_on and f.due_on < today else ""
        lines += [f"\n{icon} {f.issuer}{money}{due}{late}", f"   {f.summary}"]
        if f.replies:
            lines.append(f"   💬 {f.replies}")
        lines.append(f"   ✉️ {h.sender}, {h.received_at[:10]}" + (f" (+{n - 1} more in the conversation)" if n > 1 else ""))
        if kind == "bill" and f.amount_eur and (not f.due_on or f.due_on >= today):
            total += round(f.amount_eur * 100)
            if ctx.money is not None and f.due_on:
                if tracked := ctx.money.tracked_bill(f.amount_eur, f.due_on):
                    lines.append(f"   ✅ already tracked: {tracked}")
                else:
                    proposals.append(_track(ctx, f))
    if total:
        lines.append(f"\nStill to pay: {eur(total)}")
    if earlier:
        lines.append("\nEarlier:")
        lines += [f"• {f.due_on:%d %b} {f.issuer}: {f.summary}" for h, kind, f, n in earlier]
    if capped:
        lines.append(f"(Only the newest {checked} emails were checked: narrow the period or the topic.)")
    if skipped:
        lines.append(f"(+{skipped} more conversations not read: narrow the period or the topic.)")
    return Report("\n".join(lines), proposals)


def _track(ctx: Ctx, f: Found) -> Proposal:
    payee, amount, due = f.issuer[:40], f.amount_eur, f.due_on
    return Proposal(f"➕ {payee[:22]} {amount:.2f} € · {due:%d %b}",
                    lambda: ctx.money.add_bill(payee, amount, due))


WORKFLOW = Workflow(
    name="mail_bills",
    title="🧾 Mail bills",
    description=("mail_bills: go through the emails the user received over a period and summarize the bills and "
                 "notices found in them (not the bank account, not what was spent)"),
    params=Params,
    run=run,
    needs=("gmail",),
    scope="mail",
)
