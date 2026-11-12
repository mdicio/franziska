"""The finance tools as plain methods: one implementation behind the Pydantic AI agent, the CLI and the bot's commands.

Like `Life`, methods return short text for a model (or a phone) to read and raise MoneyError for input the model
should correct. Everything here is deterministic: summaries, budgets, recurring charges and bill matching are SQL
and arithmetic, never a model. The agent only decides which of these to call, or writes a SELECT for anything else.
The one exception is `categorise`, which is handed the model's decision function by its caller (categories.py).

Link to the secretary: a bill is a payment the user expects to make. Its due date and reminder are a task in `Life`
(titled with the payee only); the amount stays in the ledger. When an import contains a matching payment, the task
is completed (a monthly bill moves to next month), so a bill paid is never reminded about.
"""

import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

from franziska.finance import categories, importer, ledger, links, mapped, merchants
from franziska.finance.ledger import Transaction
from franziska.secretary.service import Life, LifeError, Recur
from franziska.secretary.tasks import advance

STALE_DAYS = 10  # a ledger whose newest bank movement is older than this is called out as stale
BUDGET_WARN = 0.8  # share of a monthly budget at which the brief starts warning
PRICE_CHANGE = 0.15  # a recurring charge this far off its usual amount is reported after an import
BILL_TOLERANCE = 0.2  # a payment within this share of a bill's amount (and the window below) settles it
MAX_PAYEES = 5  # a correction naming more payees than this is too vague to apply
BILL_WINDOW = (timedelta(days=25), timedelta(days=15))  # before / after the due date


class MoneyError(ValueError):
    """A tool call the model should fix and retry (bad month, unknown category, bill without a date)."""


def eur(cents: int) -> str:
    return f"{cents / 100:,.2f} €".replace(",", "'")


def _month(month: str | None, today: date) -> tuple[date, date]:
    """First day of `month` ('YYYY-MM', default this month) and first day of the next."""
    try:
        first = date.fromisoformat(f"{month}-01") if month else today.replace(day=1)
    except ValueError as e:
        raise MoneyError(f"month must look like 2026-05, not {month!r}.") from e
    return first, advance(datetime.combine(first, time()), "monthly").date()


@dataclass(frozen=True)
class Recurring:
    merchant: str
    category: str
    typical_cents: int  # median, > 0
    variable: bool  # amounts spread more than 25% around the median
    count: int
    last_on: date
    next_on: date  # last + one month, projected forward to today or later
    active: bool  # charged within ~1.5 months of the ledger's newest movement
    payee: str = ""  # the identity it was grouped by (categories.payee_of), as categories and price checks use it

    def line(self) -> str:
        amount = ("~" if self.variable else "") + eur(self.typical_cents)
        when = f"next ~{self.next_on:%d %b}" if self.active else f"last {self.last_on:%d %b %Y}, stopped?"
        return f"{self.merchant} · {amount}/month · {self.category} · {when}"


@dataclass
class Money:
    db: Path
    timezone: str = "Europe/Rome"
    clock: Callable[[], datetime] | None = None
    life: Life | None = None  # where bill reminders go; without it, bills are unavailable
    owner: str = ""  # the user's name as banks write it: tells own transfers from other people's (links.py)
    events: object | None = None  # events.Events: where 'bill.paid' goes (Core's outbox); None: one in memory

    @classmethod
    def open(cls, db: Path, timezone: str = "Europe/Rome", life: Life | None = None,
             clock: Callable[[], datetime] | None = None, owner: str = "") -> "Money":
        ledger.connect(db, migrate=True).close()  # create / migrate once, so every later connection finds the schema
        mapped.DIR = db.parent / "banks"  # the bank formats saved from a confirmed mapping, next to the ledger
        return cls(db, timezone, clock or (life and life.clock), life, owner)

    def now(self) -> datetime:
        return self.clock() if self.clock else datetime.now(ZoneInfo(self.timezone)).replace(tzinfo=None)

    def today(self) -> date:
        return self.now().date()

    @contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        con = ledger.connect(self.db)
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- reading --------------------------------------------------------------------------------
    def query(self, sql: str) -> dict:
        """A model-written SELECT on a read-only connection (ledger.run_select)."""
        return ledger.run_select(self.db, sql)

    def data_until(self) -> dict[str, date]:
        """Per bank account, the last day its imported statements cover (a statement's end, or its newest row)."""
        with self._con() as con:
            rows = con.execute(
                "SELECT account, max(d) FROM (SELECT account, period_to AS d FROM imports"
                " UNION ALL SELECT account, booked_on FROM transactions WHERE account != 'cash') GROUP BY account"
            ).fetchall()
        return {a: date.fromisoformat(d) for a, d in rows if a in ledger.ACCOUNTS}

    def last_movement(self) -> date | None:
        """The oldest of the accounts' last days: figures after it miss some account."""
        until = self.data_until()
        return min(until.values()) if until else None

    def freshness(self) -> str | None:
        """A line naming the bank accounts whose data is old, or None when all are recent enough to report on."""
        until = self.data_until()
        if not until:
            return "No bank data yet: send the Intesa or Revolut export."
        stale = [f"{a} ends {d:%d %b %Y}" for a, d in sorted(until.items())
                 if a != "revolut_savings" and (self.today() - d).days > STALE_DAYS]
        return f"Bank data {', '.join(stale)}: send a new export for current figures." if stale else None

    def month_summary(self, month: str | None = None) -> str:
        """Income, spending, net and the top categories of one month, against the average of the 3 before."""
        first, end = _month(month, self.today())
        with self._con() as con:
            def totals(a: date, b: date) -> tuple[int, int]:
                return con.execute(
                    "SELECT COALESCE(SUM(MAX(amount_cents, 0)), 0), COALESCE(-SUM(MIN(amount_cents, 0)), 0)"
                    " FROM movements WHERE booked_on >= ? AND booked_on < ?", (a.isoformat(), b.isoformat()),
                ).fetchone()

            income, spent = totals(first, end)
            cats = con.execute(
                "SELECT COALESCE(effective_category, 'uncategorised'), -SUM(amount_cents) FROM movements"
                " WHERE amount_cents < 0 AND booked_on >= ? AND booked_on < ? GROUP BY 1 ORDER BY 2 DESC",
                (first.isoformat(), end.isoformat())).fetchall()
            guessed = con.execute(
                "SELECT COALESCE(-SUM(amount_cents), 0) FROM movements WHERE amount_cents < 0 AND category IS NULL"
                " AND proposed_category IS NOT NULL AND booked_on >= ? AND booked_on < ?",
                (first.isoformat(), end.isoformat())).fetchone()[0]
            before = advance(datetime.combine(first, time()), "monthly", -3).date()
            _, spent_before = totals(before, first)
            months_before = con.execute(
                "SELECT count(DISTINCT substr(booked_on, 1, 7)) FROM movements WHERE booked_on >= ? AND booked_on < ?",
                (before.isoformat(), first.isoformat())).fetchone()[0]
        title = f"💶 {first:%B %Y}"
        if not income and not spent:
            return f"{title}: no movements in the ledger." + (f" {f}" if (f := self.freshness()) else "")
        out = [title, f"In {eur(income)} · out {eur(spent)} · net {eur(income - spent)}"]
        if income > 0:
            out[-1] += f" · saved {max(0, income - spent) * 100 // income}%"
        if months_before:
            avg = spent_before // months_before
            delta = (spent - avg) * 100 // avg if avg else 0
            out.append(f"Spending vs {months_before}-month average {eur(avg)}: {delta:+d}%")
        out += [f"  {c}: {eur(v)}" for c, v in cats[:6]]
        if len(cats) > 6:
            out.append(f"  other {len(cats) - 6} categories: {eur(sum(v for _, v in cats[6:]))}")
        if guessed:
            out.append(f"  ({eur(guessed)} of it grouped by the model's guess: /uncat to check)")
        if self.today() < end:
            out.append(f"(month in progress{'; ' + f if (f := self.freshness()) else ''})")
        return "\n".join(out)

    def week_summary(self, day: str | None = None, weeks_ago: int = 1) -> str:
        """One Monday-to-Sunday week: in, out, against the week before and the 8 before that, spending by category,
        the biggest payments, subscriptions charged, and what is expected in the 7 days from today.

        `day`: 'YYYY-MM-DD', any day of the week to report; else `weeks_ago` (1: last week, 0: this week)."""
        try:
            pick = date.fromisoformat(day) if day else self.today() - timedelta(days=7 * weeks_ago)
        except ValueError as e:
            raise MoneyError(f"day must look like 2026-10-19, not {day!r}.") from e
        first = pick - timedelta(days=pick.weekday())
        end = first + timedelta(days=7)
        span = (first.isoformat(), end.isoformat())
        with self._con() as con:
            def totals(a: date, b: date) -> tuple[int, int]:
                return con.execute(
                    "SELECT COALESCE(SUM(MAX(amount_cents, 0)), 0), COALESCE(-SUM(MIN(amount_cents, 0)), 0)"
                    " FROM movements WHERE booked_on >= ? AND booked_on < ?", (a.isoformat(), b.isoformat()),
                ).fetchone()

            income, spent = totals(first, end)
            _, spent_before = totals(first - timedelta(days=7), first)
            _, spent_8 = totals(first - timedelta(days=56), first)
            cats = con.execute(
                "SELECT COALESCE(effective_category, 'uncategorised'), -SUM(amount_cents), count(*) FROM movements"
                " WHERE amount_cents < 0 AND booked_on >= ? AND booked_on < ? GROUP BY 1 ORDER BY 2 DESC",
                span).fetchall()
            top = con.execute(
                "SELECT booked_on, COALESCE(merchant, payee, description), -amount_cents,"
                " COALESCE(effective_category, 'uncategorised') FROM movements"
                " WHERE amount_cents < 0 AND booked_on >= ? AND booked_on < ? ORDER BY amount_cents LIMIT 5",
                span).fetchall()
            charged = con.execute(
                "SELECT booked_on, COALESCE(merchant, payee, description), -amount_cents FROM movements"
                " WHERE amount_cents < 0 AND effective_category = 'subscriptions' AND booked_on >= ? AND booked_on < ?"
                " ORDER BY booked_on", span).fetchall()
            earned = con.execute(
                "SELECT COALESCE(effective_category, 'uncategorised'), SUM(amount_cents) FROM movements"
                " WHERE amount_cents > 0 AND booked_on >= ? AND booked_on < ? GROUP BY 1 ORDER BY 2 DESC",
                span).fetchall()
        last = end - timedelta(days=1)
        title = f"💶 Week {first:%a %d %b} – {last:%a %d %b %Y}"
        until = self.last_movement()
        if not income and not spent:
            return f"{title}: no movements in the ledger." + (f" {f}" if (f := self.freshness()) else "")
        out = [title, f"In {eur(income)} · out {eur(spent)} · net {eur(income - spent)}"]
        if spent_before:
            out.append(f"Spending vs the week before ({eur(spent_before)}): {(spent - spent_before) * 100 // spent_before:+d}%")
        if spent_8:
            avg = spent_8 // 8
            out.append(f"Spending vs the 8-week average ({eur(avg)}): {(spent - avg) * 100 // avg:+d}%")
        out.append("By category:")
        out += [f"  {c}: {eur(v)} ({n}×)" for c, v, n in cats]
        if earned:
            out.append("Income: " + " · ".join(f"{c} {eur(v)}" for c, v in earned))
        out.append("Biggest payments:")
        out += [f"  {date.fromisoformat(on):%a %d} {who} {eur(c)} ({cat})" for on, who, c, cat in top]
        out.append("Subscriptions charged: " + (" · ".join(f"{who} {eur(c)} ({date.fromisoformat(on):%a %d})"
                                                          for on, who, c in charged) or "none"))
        coming = self.expected(7)
        out.append(f"Expected {self.today():%a %d} – {self.today() + timedelta(days=6):%a %d %b}: "
                   + (" · ".join(coming) if coming else "nothing found (no monthly charge or bill falls due)"))
        if self._budgets():
            out.append(self.budget_status())
        if until and until < last:
            out.append(f"(bank data ends {until:%a %d %b}: the week is incomplete)")
        return "\n".join(out)

    def expected(self, days: int = 7) -> list[str]:
        """Monthly charges and tracked bills due from today for `days` days, soonest first."""
        until = self.today() + timedelta(days=days)
        found = [(r.next_on, f"~{r.next_on:%a %d}: {r.merchant} {'~' if r.variable else ''}{eur(r.typical_cents)}")
                 for r in self.recurring() if r.active and r.next_on < until]
        if self.life is not None:
            with self._con() as con:
                bills = con.execute("SELECT payee, amount_cents, task_id FROM bills").fetchall()
            for payee, cents, task_id in bills:
                task = self.life.tasks.get(task_id)
                if task and not task.done_at and task.due_at:
                    due = datetime.fromisoformat(task.due_at).date()
                    if self.today() <= due < until:
                        found.append((due, f"{due:%a %d}: bill {payee} {eur(cents)}"))
        return [line for _, line in sorted(found)]

    def review(self, limit: int = 10) -> list[tuple[str, int, int, str]]:
        """(payee, payments, cents spent, the model's group) for the biggest payees whose group is only a guess."""
        with self._con() as con:
            return con.execute(
                "SELECT payee, count(*), -SUM(amount_cents), proposed_category FROM movements"
                " WHERE category IS NULL AND proposed_category IS NOT NULL AND amount_cents < 0"
                " GROUP BY payee ORDER BY 3 DESC LIMIT ?", (limit,)).fetchall()

    def categories(self) -> list[str]:
        """The spending groups, then the categories only the bank's own fields set."""
        return [*categories.GROUPS, *categories.FACTS]

    # ---- recurring charges ----------------------------------------------------------------------
    def recurring(self) -> list[Recurring]:
        """Monthly charges found in the bank data: subscriptions, phone, insurance paid monthly.

        A payee qualifies when its payments come about monthly (see _monthly). A payee that does not, such as
        Apple billing several subscriptions plus one-off purchases, is split by exact amount, so each fixed-price
        subscription inside it still shows up.
        """
        with self._con() as con:
            # Grouped by payee: the same identity spending groups and price checks use (categories.payee_of).
            rows = con.execute(
                "SELECT COALESCE(merchant, payee), booked_on, -amount_cents, COALESCE(effective_category, 'uncategorised'),"
                " payee FROM movements"
                " WHERE account != 'cash' AND kind IS NOT 'interest' AND amount_cents < 0 AND payee IS NOT NULL"
                " AND COALESCE(merchant, '') != 'ATM' ORDER BY booked_on").fetchall()
        if not rows:
            return []
        newest = date.fromisoformat(rows[-1][1])
        groups: dict[str, list[tuple[date, int, str, str]]] = defaultdict(list)
        for m, on, cents, cat, payee in rows:
            groups[payee].append((date.fromisoformat(on), cents, cat, m))
        found = []
        for payee, payments in groups.items():
            if series := _monthly(payments):
                found.append(self._recurring(series, newest, payee))
                continue
            by_amount: dict[int, list] = defaultdict(list)  # mixed payee: fixed-price series inside it
            for p in payments:
                by_amount[p[1]].append(p)
            series = [s for s in map(_monthly, by_amount.values()) if s]
            if not series and (chain := _monthly(_chain(payments))):
                # A plan whose price changed, next to one-off top-ups (a subscription at 20.00, then 25.00, next to one-off 50.00 top-ups).
                series = [chain]
            found += [self._recurring(s, newest, payee) for s in series]
        return sorted(found, key=lambda r: (not r.active, r.next_on))

    def _recurring(self, payments: list[tuple[date, int, str, str]], newest: date, payee: str) -> Recurring:
        # The latest three: after a plan change the old price is not what comes next.
        amounts = [c for _, c, _, _ in payments[-3:]]
        median = int(statistics.median(amounts))
        last_on = payments[-1][0]
        nxt = datetime.combine(last_on, time())
        while nxt.date() < self.today():
            nxt = advance(nxt, "monthly")
        return Recurring(
            merchant=statistics.mode(m for *_, m in payments),
            category=statistics.mode(c for _, _, c, _ in payments),
            typical_cents=median,
            variable=(max(amounts) - min(amounts)) > 0.25 * median,
            count=len(payments),
            last_on=last_on,
            next_on=nxt.date(),
            active=(newest - last_on).days <= 45,
            payee=payee,
        )

    def recurring_text(self) -> str:
        found = self.recurring()
        if not found:
            return "No monthly charges found in the bank data."
        active = [r for r in found if r.active]
        out = [f"🔁 Monthly charges: {eur(sum(r.typical_cents for r in active))}/month in {len(active)} payees"]
        out += [f"  {r.line()}" for r in found]
        if f := self.freshness():
            out.append(f"({f})")
        return "\n".join(out)

    # ---- budgets --------------------------------------------------------------------------------
    def set_budget(self, category: str, monthly_eur: float | None) -> str:
        category = category.strip().lower()
        with self._con() as con:
            if not monthly_eur:
                con.execute("DELETE FROM budgets WHERE category = ?", (category,))
                return f"Budget for '{category}' removed."
            if monthly_eur < 0:
                raise MoneyError("monthly_eur is the limit as a positive number, e.g. 300.")
            if category not in self.categories():
                raise MoneyError(f"There is no category '{category}'. Known: {', '.join(self.categories())}.")
            con.execute("INSERT OR REPLACE INTO budgets VALUES (?, ?)", (category, round(monthly_eur * 100)))
        return f"Budget set: {category} {eur(round(monthly_eur * 100))}/month.\n{self.budget_status()}"

    def _budgets(self, month: str | None = None) -> list[tuple[str, int, int]]:
        """(category, limit, spent) this month, in cents."""
        first, end = _month(month, self.today())
        with self._con() as con:
            return con.execute(
                "SELECT b.category, b.monthly_cents, COALESCE(-SUM(t.amount_cents), 0) FROM budgets b"
                " LEFT JOIN movements t ON t.effective_category = b.category AND t.amount_cents < 0"
                " AND t.booked_on >= ? AND t.booked_on < ? GROUP BY b.category ORDER BY b.category",
                (first.isoformat(), end.isoformat())).fetchall()

    def budget_status(self, month: str | None = None) -> str:
        rows = self._budgets(month)
        if not rows:
            return "No budgets set. Example: 'budget 250 a month for food delivery'."
        first, end = _month(month, self.today())
        elapsed = min(1.0, max(0, (self.today() - first).days + 1) / (end - first).days)
        out = [f"🎯 Budgets {first:%B %Y}" + (f" ({elapsed:.0%} of the month gone)" if elapsed < 1 else "")]
        for cat, limit, spent in rows:
            mark = "🔴" if spent > limit else "🟠" if spent >= BUDGET_WARN * limit else "🟢"
            out.append(f"  {mark} {cat}: {eur(spent)} of {eur(limit)} ({spent * 100 // limit}%)")
        if f := self.freshness():
            out.append(f"({f})")
        return "\n".join(out)

    def budget_warnings(self) -> list[str]:
        return [f"{'🔴 over' if spent > limit else '🟠 near'} budget: {cat} {eur(spent)} of {eur(limit)}"
                for cat, limit, spent in self._budgets() if spent >= BUDGET_WARN * limit]

    # ---- writing --------------------------------------------------------------------------------
    def set_payee_category(self, payee: str, category: str) -> str:
        """The user's word for a payee: authoritative for its past and future payments. `payee` is matched without
        spaces or punctuation against the payee and merchant names; an exact payee wins over partial matches."""
        category = category.strip().lower()
        if category not in categories.GROUPS:
            raise MoneyError(f"'{category}' is not a spending group. Groups: {', '.join(categories.GROUPS)}.")
        needle = _squash(payee)
        if len(needle) < 2:
            raise MoneyError("Name the payee as the bank writes it, e.g. 'Q8' or 'Esselunga'.")
        with self._con() as con:
            names = con.execute("SELECT DISTINCT payee, merchant FROM transactions WHERE payee IS NOT NULL").fetchall()
            found = sorted({p for p, _ in names if _squash(p) == needle}
                           or {p for p, m in names if needle in _squash(p) or needle in _squash(m)})
            if not found:
                raise MoneyError(f"No payee matches '{payee}'. Use the name as it appears in the bank movements.")
            if len(found) > MAX_PAYEES:
                raise MoneyError(f"'{payee}' matches {len(found)} payees ({', '.join(found[:8])}, ...); be more "
                                 "specific.")
            now = self.now().isoformat(timespec="seconds")
            con.executemany("INSERT OR REPLACE INTO payees VALUES (?, ?, 'user', NULL, NULL, ?)",
                            [(p, category, now) for p in found])
            categories.apply(con)
            n = con.execute(f"SELECT count(*) FROM transactions WHERE payee IN ({','.join('?' * len(found))})"
                            " AND category_by = 'user'", found).fetchone()[0]
        return f"{', '.join(found)} → {category} ({n} payments, now and in future imports)."

    def confirm_guesses(self) -> str:
        """Every model guess becomes the user's answer, so nothing re-decides it."""
        with self._con() as con:
            n = con.execute("UPDATE payees SET by = 'user' WHERE by = 'model'").rowcount
            categories.apply(con)
        return f"Confirmed the group of {n} payees."

    def categorise(self, decide: Callable[[list[str]], list[dict[str, float]]], model: str = "",
                   progress: Callable[[str], None] = lambda text: None, batch: int = 20) -> str:
        """Give a group to every payee of spending that has none: `decide` answers categories.QUESTION over
        categories.GROUPS' options for a batch of descriptions. Only payees never decided are asked, the most money
        first; each batch is saved as it comes, so an interrupted run resumes where it stopped."""
        with self._con() as con:
            todo = categories.pending(con)
            texts = [categories.describe(con, p) for p in todo]
        for i in range(0, len(todo), batch):
            answers = decide(texts[i:i + batch])
            with self._con() as con:
                categories.record(con, zip(todo[i:i + batch], answers, strict=True), model, self.now())
            progress(f"🏷️ {min(i + batch, len(todo))} of {len(todo)} payees grouped")
        if not todo:
            return "Every payee already has a group."
        with self._con() as con:
            n, cents = con.execute(
                f"SELECT count(*), -SUM(amount_cents) FROM movements WHERE amount_cents < 0 AND category IS NULL"
                f" AND payee IN ({','.join('?' * len(todo))})", todo).fetchone()
        out = [f"🏷️ Grouped {len(todo)} new payees ({n} payments, {eur(cents)}) by the model's guess.",
               "Biggest guesses (say 'X is transport' to correct one, or /uncat):"]
        out += [f"  {p} · {k}× · {eur(c)} → {g}" for p, k, c, g in self.review(8)]
        return "\n".join(out)

    def record_cash(self, on: date | None, amount_eur: float, description: str, category: str) -> str:
        on = on or self.today()
        if amount_eur == 0:
            raise MoneyError("amount_eur is 0. Use a negative number for spending, positive for income.")
        if category.strip().lower() not in self.categories():
            raise MoneyError(f"'{category}' is not a category. Use one of: {', '.join(self.categories())}.")
        if on > self.today():
            raise MoneyError(f"{on} is in the future (today is {self.today()}). Cash entries are things that happened.")
        tx = Transaction("cash", on, None, round(amount_eur * 100), description, "", "chat", category)
        with self._con() as con:
            # A second identical coffee that day is real too: the id's occurrence index counts the earlier ones
            # (ledger._ids), so insert occurrences 0..n and the missing one goes in. Inserted alone it was ignored.
            n = con.execute("SELECT count(*) FROM transactions WHERE account = 'cash' AND booked_on = ? AND "
                            "amount_cents = ? AND description = ?", (on.isoformat(), tx.amount_cents,
                                                                     description)).fetchone()[0]
            if not ledger.insert(con, [tx] * (n + 1)):
                raise MoneyError("Nothing was recorded (the ledger refused the row).")
        return f"Recorded {amount_eur:+.2f} EUR on {on.isoformat()} ({category.strip().lower()})."

    def edit_cash(self, on: date, amount_eur: float, new_amount_eur: float | None = None,
                  new_description: str | None = None, new_category: str | None = None, delete: bool = False) -> str:
        """Correct or delete a cash entry recorded by mistake, found by its day and amount (the latest one if several)."""
        cents = round(amount_eur * 100)
        with self._con() as con:
            row = con.execute("SELECT rowid, amount_cents, description, category FROM transactions WHERE account = "
                              "'cash' AND booked_on = ? AND amount_cents = ? ORDER BY rowid DESC LIMIT 1",
                              (on.isoformat(), cents)).fetchone()
            if row is None:
                found = con.execute("SELECT booked_on, amount_cents, description FROM transactions WHERE account = "
                                    "'cash' AND booked_on BETWEEN ? AND ? ORDER BY booked_on",
                                    ((on - timedelta(days=7)).isoformat(), (on + timedelta(days=7)).isoformat())
                                    ).fetchall()
                near = "; ".join(f"{d} {c / 100:+.2f} {t}" for d, c, t in found) or "none"
                raise MoneyError(f"No cash entry of {amount_eur:+.2f} EUR on {on}. Cash entries that week: {near}.")
            rowid, _, description, category = row
            if delete:
                con.execute("DELETE FROM transactions WHERE rowid = ?", (rowid,))
                return f"Deleted the cash entry {amount_eur:+.2f} EUR on {on} ({description})."
            if new_category is not None and new_category.strip().lower() not in self.categories():
                raise MoneyError(f"'{new_category}' is not a category. Use one of: {', '.join(self.categories())}.")
            if new_amount_eur == 0:
                raise MoneyError("new_amount_eur is 0: to remove the entry, use delete=true.")
            if new_amount_eur is None and new_description is None and new_category is None:
                raise MoneyError("Nothing to change: pass new_amount_eur, new_description, new_category or delete=true.")
            new = (round(new_amount_eur * 100) if new_amount_eur is not None else cents,
                   new_description or description, (new_category or category or "").strip().lower() or None)
            con.execute("UPDATE transactions SET amount_cents = ?, description = ?, category = ? WHERE rowid = ?",
                        (*new, rowid))
        return f"Corrected the cash entry on {on}: now {new[0] / 100:+.2f} EUR, {new[1]} ({new[2]})."

    def import_file(self, path: Path) -> str:
        """Import a bank statement (Intesa, Revolut or a mapped bank, importer.py), refusing one that doesn't add up. Then link own
        transfers and report what changed: per account, checks, links, bills settled, charges that moved in price."""
        st = importer.read(path)
        usual = self._usual_prices()  # before the import, so a new price is compared with the old ones
        now = self.now()
        with self._con() as con:
            replaced = _supersede(con, st)  # raises before anything is written when the file would lose rows
            txs, dropped = _drop_weaker(con, st)
            before = {r[0] for r in con.execute("SELECT id FROM transactions")}
            new = ledger.insert(con, txs)
            ids = dict(zip(map(id, txs), ledger._ids(txs)))
            for account in sorted({t.account for t in st.txs}):
                days = [t.booked_on for t in st.txs if t.account == account]
                stated = [b[1] for b in st.balances if b[0] == account]
                first, last = st.period or (min(days + stated), max(days + stated))
                con.execute(
                    "INSERT INTO imports (file, format, account, period_from, period_to, rows, new, checks, imported_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (path.name, st.format, account, first.isoformat(), last.isoformat(), len(days),
                     sum(1 for t in txs if t.account == account and ids[id(t)] not in before),
                     "\n".join(c for c in st.checks if c.startswith(account)), now.isoformat(timespec="seconds")))
            con.executemany("INSERT OR REPLACE INTO balances VALUES (?, ?, ?, ?)",
                            [(a, d.isoformat(), c, path.name) for a, d, c in st.balances])
            linked = links.link_all(con, self.owner, now)
            pending = links.unmatched_topups(con, self.owner)
            recent = (self.today() - timedelta(days=45)).isoformat()  # an old backfill reports no stale price news
            fresh = con.execute(
                "SELECT id, booked_on, payee, -amount_cents FROM movements WHERE amount_cents < 0 AND booked_on >= ?",
                (recent,)).fetchall()
            ungrouped = len(categories.pending(con))
        fresh = [r for r in fresh if r[0] not in before]
        # Counterparties no pattern read: grouped under their description's first words, which may lump two together.
        unread = Counter(categories.payee_of(None, t.description) for t in txs if ids[id(t)] not in before
                         and not merchants.recognised(t.description, t.details, t.source_format))
        out = [f"Imported {path.name}: {len(st.txs)} movements, {new} new.", *st.checks]
        if replaced:
            out.append(f"♻️ Replaced {replaced} movements from an older export format with this file's (same "
                       "amount and day for each one).")
        if dropped:
            out.append(f"Skipped {dropped} movements already covered by a newer export format.")
        out += [f"Skipped {n} {why} rows." for why, n in st.skipped.items()]
        if linked["savings_move"] or linked["own_transfer"]:
            out.append(f"🔁 Linked {linked['own_transfer']} transfers between your accounts and "
                       f"{linked['savings_move']} savings moves (not counted as income or spending).")
        out += [f"🤔 Not linked, two candidates: {a}" for a in linked["ambiguous"]]
        if maybe := self.transfer_candidates():
            out.append(f"🤔 {len(maybe)} pairs look like a transfer between your accounts, but nothing says the money "
                       "is yours, so each counts as spending and as income: " + "; ".join(label for *_, label in maybe))
        if pending:
            out.append(f"⏳ {len(pending)} top-ups to Revolut ({eur(sum(c for _, c in pending))}) have no Intesa side "
                       f"yet: the Intesa data for those days is missing, so they count as income until it arrives.")
        out += [f"🏦 {a}: {eur(c)} on {d:%d %b %Y}" for a, d, c in self.balances()]
        if unread:
            out.append(f"🔍 {sum(unread.values())} new movements whose counterparty I couldn't read, grouped by their "
                       f"text as: {', '.join(f'{p} ×{n}' for p, n in unread.most_common(8))}")
        if ungrouped:
            out.append(f"🏷️ {ungrouped} payees have no spending group yet: /categorise")
        if new:
            out += self.reconcile()
            out += self._price_changes(fresh, usual)
            out += self.budget_warnings()
        return "\n".join(out)

    def transfer_candidates(self) -> list[tuple[str, str, str]]:
        """(out id, in id, label) of unlinked pairs that look like the user's own transfer (links.candidates)."""
        with self._con() as con:
            pairs = links.candidates(con, self.owner)
        return [(o[0], i[0], f"{o[2]} {o[1]} → {i[1]} {eur(-o[4])}") for o, i in pairs]

    def link_own(self, out_id: str, in_id: str) -> str:
        """The user says this pair is their own transfer: neither side counts as spending or income any more."""
        with self._con() as con:
            done = links.link(con, out_id, in_id, self.now())
        return "🔁 Linked as a transfer between your accounts." if done else "Already linked."

    def balance_text(self) -> str:
        """Each account's latest balance as its bank stated it, for the agent ('how much do I have?')."""
        rows = self.balances()
        if not rows:
            return "No balances in the bank exports."
        out = [f"{ledger.ACCOUNTS.get(a, a)}: {eur(c)} on {d:%a %d %b %Y}" for a, d, c in rows]
        out.append("Cash in the wallet is not tracked as a balance (only cash entries).")
        if f := self.freshness():
            out.append(f)
        return "\n".join(out)

    def balances(self) -> list[tuple[str, date, int]]:
        """Each account's latest balance as its bank stated it: (account, day, cents)."""
        with self._con() as con:
            rows = con.execute("SELECT account, max(on_date), cents FROM balances GROUP BY account ORDER BY account"
                               ).fetchall()
        return [(a, date.fromisoformat(d), c) for a, d, c in rows]

    def _usual_prices(self) -> dict[str, Recurring]:
        series = defaultdict(list)
        for r in self.recurring():
            if r.active:
                series[r.payee].append(r)
        # A payee with several subscriptions (Apple) has no single usual price to compare with.
        return {k: rs[0] for k, rs in series.items() if len(rs) == 1 and not rs[0].variable}

    def _price_changes(self, fresh: list[tuple[str, str, str | None, int]], usual: dict[str, Recurring]) -> list[str]:
        out = []
        for _, on, payee, cents in fresh:
            r = payee and usual.get(payee)
            if r and abs(cents - r.typical_cents) > PRICE_CHANGE * r.typical_cents:
                out.append(f"⚠️ {r.merchant} charged {eur(cents)} on {on}, usually {eur(r.typical_cents)}")
        return out

    # ---- bills (with the secretary) -------------------------------------------------------------
    def _life(self) -> Life:
        if self.life is None:
            raise MoneyError("Bills need the secretary's schedule, which is not connected here.")
        return self.life

    def add_bill(self, payee: str, amount_eur: float, due_on: date, match: str | None = None,
                 recur: Recur | None = None, remind_days_before: int = 3) -> str:
        """Track a payment due on a date: a task reminds before it and is ticked off when the bank shows it paid."""
        life = self._life()
        if amount_eur <= 0:
            raise MoneyError("amount_eur is the expected payment as a positive number, e.g. 864.")
        if due_on < self.today():
            raise MoneyError(f"due_on {due_on} is in the past (today is {self.today()}).")
        pattern = (match or payee).strip()
        if len(pattern) < 3:
            raise MoneyError("match is too short to recognise the payment; use at least 3 characters.")
        if same := self.tracked_bill(amount_eur, due_on):
            raise MoneyError(f"Already tracked: {same}. Nothing added.")
        due_at = datetime.combine(due_on, time(18, 0))
        remind_at = max(datetime.combine(due_on - timedelta(days=remind_days_before), time(9, 0)), life.now())
        try:
            task = life.new_task(f"Pay {payee.strip()}", due_at=due_at, remind_at=remind_at, recur=recur,
                                 notes="bill tracked by the finance assistant")
        except LifeError as e:
            raise MoneyError(str(e)) from e
        with self._con() as con:
            cur = con.execute("INSERT INTO bills (payee, pattern, amount_cents, task_id, created_at) VALUES (?, ?, ?, ?, ?)",
                              (payee.strip(), pattern, round(amount_eur * 100), task.id, self.today().isoformat()))
        settled = self.reconcile()  # an export already in the ledger may show it paid
        saved = (f"Bill {cur.lastrowid} saved: {payee.strip()} {eur(round(amount_eur * 100))}, "
                 f"matched by '{pattern}'. Saved: {task.line()}")
        return "\n".join([saved, *settled])

    def remove_bill(self, bill_id: int) -> str:
        """A bill added by mistake, or no longer owed: the bill goes, and its 'Pay …' task with it if still open."""
        with self._con() as con:
            row = con.execute("SELECT payee, amount_cents, task_id FROM bills WHERE id = ?", (bill_id,)).fetchone()
            if row is None:
                raise MoneyError(f"No bill {bill_id}. The bills: {self.bills()}")
            payee, cents, task_id = row
            con.execute("DELETE FROM bill_payments WHERE bill_id = ?", (bill_id,))
            con.execute("DELETE FROM bills WHERE id = ?", (bill_id,))
        task = self.life.tasks.get(task_id) if self.life else None
        if task is not None and not task.done_at:
            self.life.cancel(task_id)
        return f"Removed bill {bill_id}: {payee} {eur(cents)}" + (" and its reminder task." if task else ".")

    def bill_paid(self, bill_id: int) -> str:
        """The user paid a tracked bill before the bank export shows it: its task is ticked off (a repeating one
        moves on). The export's payment is matched later as usual."""
        with self._con() as con:
            row = con.execute("SELECT payee, task_id FROM bills WHERE id = ?", (bill_id,)).fetchone()
        if row is None:
            raise MoneyError(f"No bill {bill_id}. The bills: {self.bills()}")
        try:
            return self._life().complete(row[1])
        except LifeError as e:
            raise MoneyError(str(e)) from e

    def tracked_bill(self, amount_eur: float, due_on: date) -> str | None:
        """The bill already tracked with this amount and due day ('[3] Condominio 410,00 € · due 2026-12-01'), or None:
        mail_bills offers no ➕ for it and add_bill refuses a second copy."""
        if (life := self.life) is None:
            return None
        with self._con() as con:
            rows = con.execute("SELECT id, payee, amount_cents, task_id FROM bills WHERE amount_cents = ?",
                               (round(amount_eur * 100),)).fetchall()
        for bill_id, payee, cents, task_id in rows:
            task = life.tasks.get(task_id)
            if task and task.due_at and task.due_at[:10] == due_on.isoformat():
                return f"[{bill_id}] {payee} {eur(cents)} · due {due_on:%a %d %b}" + (" · paid" if task.done_at else "")
        return None

    def bills(self) -> str:
        with self._con() as con:
            rows = con.execute("SELECT id, payee, amount_cents, task_id FROM bills ORDER BY id").fetchall()
        if not rows:
            return "No bills tracked."
        life = self._life()
        out = []
        for bill_id, payee, cents, task_id in rows:
            task = life.tasks.get(task_id)
            state = ("task removed" if task is None else "paid" if task.done_at else
                     f"due {task.due_at[:10]}" if task.due_at else "no due date")
            out.append(f"[{bill_id}] {payee} {eur(cents)} · {state}")
        return "\n".join(out)

    def reconcile(self) -> list[str]:
        """Settle open bills whose payment is in the ledger: complete the task (a repeating bill moves on)."""
        if self.life is None:
            return []
        out = []
        with self._con() as con:
            bills = con.execute("SELECT id, payee, pattern, amount_cents, task_id, created_at FROM bills").fetchall()
            for bill_id, payee, pattern, cents, task_id, created in bills:
                # Not before the bill existed (minus a few days for a payment made just before adding it), so
                # last month's payment of a monthly bill never settles this month's.
                earliest = date.fromisoformat(created) - timedelta(days=7)
                while (task := self.life.tasks.get(task_id)) and not task.done_at and task.due_at:
                    due = datetime.fromisoformat(task.due_at).date()
                    # The name compared without spaces or punctuation ('WindTre' is 'WIND TRE S P A'), nearest day first.
                    named = [r for r in _payments(con, cents, due, earliest) if _squash(pattern) in _squash(r[3])]
                    tx = min(named, key=lambda r: abs((date.fromisoformat(r[1]) - due).days), default=None)
                    if tx is None:
                        break
                    con.execute("INSERT INTO bill_payments VALUES (?, ?, ?)", (tx[0], bill_id, due.isoformat()))
                    con.commit()
                    # The secretary completes the task (Life.on_bill_paid): ids and dates only, no amount.
                    self._bus().emit("bill.paid", {"bill_id": bill_id, "task_id": task_id, "due": due.isoformat(),
                                                   "paid_on": tx[1]}, source="finance", privacy="money")
                    out.append(f"✅ Bill paid: {payee} {eur(tx[2])} on {tx[1]} (due {due:%d %b})")
                    if not task.recur:
                        break
        return out

    def unpaid_bills(self) -> list[str]:
        """Tracked bills past their due date with no payment found, once the bank data reaches past that date: one
        line each, with the payments of about that amount in the window when the name didn't match (the bank may
        write it differently). For the brief: a 'Pay X' task that nags with no answer is worse than a question."""
        if self.life is None or (newest := self.last_movement()) is None:
            return []
        out = []
        with self._con() as con:
            for payee, pattern, cents, task_id, created in con.execute(
                    "SELECT payee, pattern, amount_cents, task_id, created_at FROM bills").fetchall():
                task = self.life.tasks.get(task_id)
                if task is None or task.done_at or not task.due_at:
                    continue
                due = datetime.fromisoformat(task.due_at).date()
                if not due < self.today() or newest <= due:  # not due yet, or the bank data doesn't reach it
                    continue
                earliest = date.fromisoformat(created) - timedelta(days=7)
                maybe = [f"{r[1]} {_one_line(r[3], 40)} {eur(r[2])}" for r in _payments(con, cents, due, earliest)][:3]
                out.append(f"🧾 {payee} {eur(cents)} was due {due:%d %b}; no payment matching '{pattern}' in the bank "
                           "data" + (f". Same amount around then: {'; '.join(maybe)}" if maybe else ""))
        return out

    def _bus(self):
        if self.events is None:  # a CLI or script: an outbox in memory, with the secretary listening
            from franziska.events import Events

            self.events = Events.memory(self.now)
            self.life.subscribe(self.events)
        return self.events

    # ---- the morning brief ----------------------------------------------------------------------
    def brief_lines(self, days: int = 7) -> list[str]:
        """Money lines for the secretary's morning brief: charges expected this week, budget warnings, stale data."""
        until = self.today() + timedelta(days=days)
        out = [f"💳 ~{r.next_on:%a %d}: {r.merchant} {'~' if r.variable else ''}{eur(r.typical_cents)}"
               for r in self.recurring() if r.active and r.next_on < until]
        out += self.budget_warnings()
        out += self.unpaid_bills()
        if f := self.freshness():
            out.append(f"💶 {f}")
        return out


def _payments(con: sqlite3.Connection, cents: int, due: date, earliest: date) -> list[tuple[str, str, int, str]]:
    """Unclaimed payments that could settle a bill of `cents` due on `due`: (id, day, cents, text), by amount
    (within BILL_TOLERANCE) and day (BILL_WINDOW around the due date, not before `earliest`) only."""
    return con.execute(
        "SELECT id, booked_on, -amount_cents, COALESCE(merchant, '') || ' ' || description || ' ' || COALESCE(details, '')"
        " FROM movements WHERE amount_cents < 0 AND booked_on BETWEEN ? AND ? AND abs(-amount_cents - ?) <= ?"
        " AND id NOT IN (SELECT tx_id FROM bill_payments) ORDER BY booked_on",
        (max(due - BILL_WINDOW[0], earliest).isoformat(), (due + BILL_WINDOW[1]).isoformat(), cents,
         round(BILL_TOLERANCE * cents))).fetchall()


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _squash(text: str | None) -> str:
    """A name without spaces or punctuation, so 'WIND TRE' and 'WINDTRE' are the same payee."""
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _monthly(payments: list[tuple[date, int, str, str]]) -> list | None:
    """The payments if they look like one monthly charge, else None.

    Every gap must be about 1, 2 or 3 months (a skipped month is still the same subscription) and at least two
    must be about one month, so a payee seen three times in a year is not called monthly.
    """
    gaps = [(b[0] - a[0]).days for a, b in pairwise(payments)]
    months = [round(g / 30.4) for g in gaps]
    if not all(1 <= k <= 3 and abs(g - k * 30.4) <= 8 for g, k in zip(gaps, months, strict=True)):
        return None
    return payments if months.count(1) >= 2 else None


def _chain(payments: list[tuple[date, int, str, str]]) -> list[tuple[date, int, str, str]]:
    """From the newest payment back, the one about a month before each (the closest to 30 days, within 5), so a
    monthly charge is found among a payee's other payments. Oldest first; empty when the chain is under 60% of the
    payee's payments in its span, since any payee paid every few days (a supermarket) has such a chain too, or
    shorter than 4 payments (stricter than _monthly: this series is picked out of noise)."""
    chain = [payments[-1]]
    for _ in range(len(payments)):
        near = [p for p in payments if abs((chain[-1][0] - p[0]).days - 30.4) <= 5]
        if not near:
            break
        chain.append(min(near, key=lambda p: abs((chain[-1][0] - p[0]).days - 30.4)))
    within = sum(chain[-1][0] <= p[0] <= chain[0][0] for p in payments)
    return chain[::-1] if len(chain) >= max(4, 0.6 * within) else []


def _op_day(row_value_on: str | None, row_booked_on: str) -> date:
    """The day a movement happened: Intesa's value date in Lista Movimenti is Lista Operazione's operation day."""
    return date.fromisoformat(row_value_on or row_booked_on)


def _supersede(con: sqlite3.Connection, st: importer.Statement) -> int:
    """Delete the rows of a weaker export format inside the days this stronger statement covers, after proving that
    each one is in the statement (same account and amount, operation day −1…+3). Raises, deleting nothing, if any
    is missing (an incomplete export) or if a bill payment points at one. Returns how many rows were replaced."""
    rank = importer.AUTHORITY.get(st.format, 1)
    doomed = []
    for account in {t.account for t in st.txs}:
        mine = [t for t in st.txs if t.account == account]
        first, last = st.period or (min(t.booked_on for t in mine), max(t.booked_on for t in mine))
        weaker = [f for f, r in importer.AUTHORITY.items() if r < rank]
        rows = con.execute(
            f"SELECT id, value_on, booked_on, amount_cents FROM transactions WHERE account = ?"
            f" AND source_format IN ({','.join('?' * len(weaker))})", (account, *weaker)).fetchall() if weaker else []
        rows = [r for r in rows if first <= _op_day(r[1], r[2]) <= last]
        used: set[int] = set()
        missing = []
        for tx_id, value_on, booked_on, cents in sorted(rows, key=lambda r: _op_day(r[1], r[2])):
            day = _op_day(value_on, booked_on)
            match = min((k for k, t in enumerate(mine) if k not in used and t.amount_cents == cents
                         and -1 <= (t.booked_on - day).days <= 3), key=lambda k: abs((mine[k].booked_on - day).days),
                        default=None)
            if match is None:
                missing.append((day, cents))
            else:
                used.add(match)
                doomed.append(tx_id)
        if missing:
            sample = ", ".join(f"{d:%d/%m/%Y} {c / 100:.2f} €" for d, c in missing[:3])
            raise importer.ImportFormatError(
                f"{len(missing)} {account} movements already in the ledger between {first:%d/%m/%Y} and {last:%d/%m/%Y} "
                f"are not in this file ({sample}{', …' if len(missing) > 3 else ''}). Nothing was imported: is the "
                "export complete?")
    if doomed:
        q = ",".join("?" * len(doomed))
        if con.execute(f"SELECT count(*) FROM bill_payments WHERE tx_id IN ({q})", doomed).fetchone()[0]:
            raise importer.ImportFormatError("This file would replace movements that settled a bill; not imported.")
        con.execute(f"DELETE FROM links WHERE out_tx IN ({q}) OR in_tx IN ({q})", doomed + doomed)  # relinked below
        con.execute(f"DELETE FROM transactions WHERE id IN ({q})", doomed)
    return len(doomed)


def _drop_weaker(con: sqlite3.Connection, st: importer.Statement) -> tuple[list[Transaction], int]:
    """The statement's rows minus those on days a stronger format's import already covers for the same account."""
    rank = importer.AUTHORITY.get(st.format, 1)
    stronger = [(a, date.fromisoformat(f), date.fromisoformat(t)) for a, fmt, f, t in con.execute(
        "SELECT account, format, period_from, period_to FROM imports") if importer.AUTHORITY.get(fmt, 0) > rank]
    keep = [t for t in st.txs if not any(a == t.account and f <= _op_day(t.value_on and t.value_on.isoformat(),
                                                                          t.booked_on.isoformat()) <= to
                                          for a, f, to in stronger)]
    return keep, len(st.txs) - len(keep)
