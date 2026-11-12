"""Any bank's export, mapped once: code finds the columns, AnyJev says what each one holds, code checks, you tap.

The built-in parsers (importer.py) know a few banks exactly. For any other .csv or .xlsx statement (Core.offer_mapping):
1. One AnyJev yes/no on the file's first rows: is this an account statement?
2. `layout`, in code: the header row (the last text-only row before the first row with a date and an amount) and each
   column's kind from its values: date, money or text.
3. `propose`, AnyJev decisions (logprobs, no generated text): what each money column holds (one signed amount,
   money out, money in, the balance, something else), which text column names the counterparty (a yes/no per column),
   and which word of the file name or its title rows names the bank (a yes/no per word). The first date column is
   the movement's date (a value date comes after it).
4. `read`, in code, on every row: every dated row has an amount, dates parse one way for the whole file, and with a
   balance column the running balance chains in file order or reversed. A mapping that fails is never offered.
5. The reply shows what was read (columns, period, totals, first rows) and a button. The tap saves a `Profile` in
   <data_dir>/banks/<account>.json and imports the file.

From then on a file whose header row matches a saved profile is that bank's format: detected and read in code, no
model, like the built-in ones (importer.detect). Profiles are plain JSON, so one can be corrected by hand or shared.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from franziska.finance import merchants
from franziska.finance.ledger import Transaction

DIR: Path | None = None  # <data_dir>/banks: set by Money.open; None (CLIs on a bare ledger): no mapped formats
PREFIX = "mapped:"  # Statement.format and Transaction.source_format of a mapped file: 'mapped:<account>'
SUFFIXES = (".csv", ".xlsx", ".txt", ".tsv")
PREVIEW_ROWS = 15
SAMPLES = 4  # values shown per column in a question

ROLES = {
    "amount": "a signed amount: one amount per movement, negative when money goes out",
    "money_out": "money out: the amount that left the account (money in is in another column)",
    "money_in": "money in: the amount that came into the account (money out is in another column)",
    "balance": "the balance: what was on the account after the movement",
    "other": "something else: a code, a count, a rate, or an amount in another currency",
}
# The question texts never change: AnyJev computes a question's content-free prior once and caches it, which costs
# 14-19 s the first time on gemma-4-26B and 1-4 s per call after (2026-11-11). What changes, the column or the word,
# goes in the state.
ROLE_Q = "What does this column of the statement hold?"
COUNTERPARTY_Q = ("Does this column name the counterparty of each movement (who was paid, or who paid: a payee, a "
                  "merchant, a full description), rather than only the kind of movement or a code?")
BANK_Q = "Is this word the name of the bank, card or payment service the statement comes from?"
YES_NO = ["yes", "no"]
# Words in file names and title rows that never name a bank.
GENERIC = {"statement", "statements", "export", "transactions", "transaction", "account", "accounts", "movimenti",
           "lista", "estratto", "conto", "umsaetze", "umsätze", "kontoauszug", "report", "bank", "banca", "data",
           "csv", "xlsx", "the", "and", "del", "della", "von", "der", "des", "operazioni", "list", "history"}


class Unusable(ValueError):
    """The mapping doesn't read the file: why, for the user."""


@dataclass
class Profile:
    account: str
    signature: list[str]  # the header row's non-empty cells, normalised (_norm): how a later file is recognised
    mapping: dict  # role → column name: date, amount | money_out / money_in, description, balance, currency
    day_first: bool  # 03/04/2026 is 3 April
    decimal: str  # ',' or '.'
    year_first: bool = False  # 2026-04-03: the order question doesn't arise

    @property
    def format(self) -> str:
        return PREFIX + self.account

    def save(self) -> Path:
        if DIR is None:
            raise Unusable("no data directory to keep the bank's format in")
        DIR.mkdir(parents=True, exist_ok=True)
        path = DIR / f"{self.account}.json"
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False) + "\n")
        return path


def profiles() -> list[Profile]:
    if DIR is None or not DIR.is_dir():
        return []
    out = []
    for p in sorted(DIR.glob("*.json")):
        try:
            out.append(Profile(**json.loads(p.read_text())))
        except (ValueError, TypeError):
            continue  # a hand edit gone wrong: that bank is unknown again, the others still work
    return out


# ---- the file as a table ----------------------------------------------------------------------------------------
def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds").removesuffix(" 00:00:00")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def table(path: Path) -> list[list[str]]:
    """Every row of the file's first sheet (or the CSV) as strings; [] when it isn't a table we can read."""
    if path.suffix.lower() == ".xlsx":
        import openpyxl

        try:
            ws = openpyxl.load_workbook(path, data_only=True, read_only=True).active
            return [[_cell(c) for c in r] for r in ws.iter_rows(values_only=True)]
        except Exception:  # not a workbook after all
            return []
    raw = path.read_bytes()[:20_000_000]
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return []
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        return []
    return [[c.strip() for c in r] for r in csv.reader(io.StringIO(text), dialect)]


def preview(rows: list[list[str]], name: str = "") -> str:
    """The file's name and its first non-empty rows, numbered, for the yes/no."""
    out = [f"File: {name}"] if name else []
    for i, r in enumerate(rows):
        if any(r):
            out.append(f"[{i}] " + " | ".join(r)[:400])
        if len(out) >= PREVIEW_ROWS:
            break
    return "\n".join(out)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _header(rows: list[list[str]], signature: list[str]) -> int | None:
    for i, r in enumerate(rows[:60]):
        if [_norm(c) for c in r if c.strip()] == signature:
            return i
    return None


def detect(path: Path) -> Profile | None:
    """The saved profile whose header row this file has, or None."""
    if path.suffix.lower() not in SUFFIXES or not (saved := profiles()):
        return None
    rows = table(path)
    return next((p for p in saved if _header(rows, p.signature) is not None), None)


# ---- values ------------------------------------------------------------------------------------------------------
_DATE = re.compile(r"^(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})(?:[ T].*)?$")


def _date_parts(s: str) -> tuple[int, int, int] | None:
    m = _DATE.match(s.strip())
    return tuple(map(int, m.groups())) if m else None


def _day_first(values: list[str]) -> bool:
    """d/m or m/d from the whole column: a second field above 12 means month first; otherwise day first."""
    return not any(p[1] > 12 for v in values if (p := _date_parts(v)) and p[0] <= 31)


def _to_date(s: str, day_first: bool) -> date | None:
    p = _date_parts(s)
    if not p:
        return None
    a, b, c = p
    try:
        if a > 31:  # 2026-04-03
            return date(a, b, c)
        y = c + 2000 if c < 100 else c
        return date(y, b, a) if day_first else date(y, a, b)
    except ValueError:
        return None


_MONEY_JUNK = re.compile(r"[€$£\s +]|EUR|USD|GBP|CHF", re.IGNORECASE)
_MONEY = re.compile(r"^\(?-?\d[\d.,']*\)?-?$")


def _is_money(s: str) -> bool:
    return bool(_MONEY.match(_MONEY_JUNK.sub("", s))) and _date_parts(s) is None


def _decimal(values: list[str]) -> str:
    """',' or '.': whichever separator ends values with one or two digits more often (1.234,56 → ',')."""
    votes = {",": 0, ".": 0}
    for v in values:
        if m := re.search(r"([.,])\d{1,2}$", _MONEY_JUNK.sub("", v)):
            votes[m.group(1)] += 1
    return "," if votes[","] > votes["."] else "."


def _to_cents(s: str, decimal: str) -> int | None:
    v = _MONEY_JUNK.sub("", s).replace("'", "")
    if not v:
        return None
    neg = v.startswith("(") and v.endswith(")") or v.endswith("-")
    v = v.strip("()").rstrip("-")
    thousands = "." if decimal == "," else ","
    v = v.replace(thousands, "").replace(decimal, ".")
    try:
        cents = round(float(v) * 100)
    except ValueError:
        return None
    return -abs(cents) if neg else cents


# ---- finding the columns -----------------------------------------------------------------------------------------
@dataclass
class Column:
    name: str
    index: int
    kind: str  # 'date' | 'money' | 'text'
    values: list[str]  # the non-empty ones, in file order

    def shown(self) -> str:
        return f"'{self.name}' (e.g. {', '.join(repr(v[:30]) for v in self.values[:SAMPLES])})"


def layout(rows: list[list[str]]) -> tuple[int, list[Column]] | None:
    """(header row, its columns with their kinds), or None when no row looks like a header over dated amounts."""
    def kind(v: str) -> str:
        return "date" if _date_parts(v) else "money" if _is_money(v) else "text"

    for h, r in enumerate(rows[:60]):
        cells = [c for c in r if c.strip()]
        if len(cells) < 3 or any(kind(c) != "text" for c in cells):
            continue
        body = [b for b in rows[h + 1:] if any(b)]
        if not body or not ({"date", "money"} <= {kind(c) for c in body[0] if c.strip()}):
            continue
        cols = []
        for j, name in enumerate(r):
            values = [b[j] for b in body[:300] if j < len(b) and b[j].strip()]
            if not name.strip() or not values:
                continue
            kinds = [kind(v) for v in values]
            k = max(("date", "money", "text"), key=kinds.count)
            cols.append(Column(name, j, k if kinds.count(k) >= 0.8 * len(kinds) else "text", values))
        return h, cols
    return None


def _bank_words(rows: list[list[str]], h: int, name: str) -> list[str]:
    """Candidate bank names: the words of the file name and of the rows above the header, minus the generic ones."""
    text = " ".join([Path(name).stem] + [c for r in rows[:h] for c in r])
    words = re.findall(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9]{1,20}|\b[0-9]{1,2}[A-Za-z]{1,10}\b", text)
    seen: dict[str, None] = {}
    for w in words:
        if w.lower() not in GENERIC and not w.isdigit():
            seen.setdefault(w.lower())
    return list(seen)[:12]


def propose(rows: list[list[str]], name: str, choose: Callable[[str, str, list[str]], dict[str, float] | None]
            ) -> Profile:
    """A Profile for this file from AnyJev choices (`choose` is Router.choose); Unusable when the columns aren't a
    statement's. Every choice is checked again by `read` on the whole file."""
    found = layout(rows)
    if found is None:
        raise Unusable("no header row above dated amounts")
    h, cols = found
    head = preview(rows[:h + 4], name)  # the file's name, its header and three rows: short prefills

    def ask(about: str, question: str, options: list[str]) -> dict[str, float]:
        probs = choose(f"{head}\n\n{about}", question, options)
        if probs is None:
            raise Unusable("the local model isn't available")
        return probs

    def yes(about: str, question: str) -> float:
        return ask(about, question, YES_NO).get("yes", 0.0)

    dates = [c for c in cols if c.kind == "date"]
    texts = [c for c in cols if c.kind == "text"]
    money = [c for c in cols if c.kind == "money"]
    if not dates or not money:
        raise Unusable("no date column or no amount column")
    mapping: dict[str, str] = {}
    mapping["date"] = dates[0].name  # the booking or transaction date comes first; a value date after it
    roles: dict[str, list[Column]] = {}
    by_text = {v: k for k, v in ROLES.items()}
    for c in money:
        probs = ask(f"The column: {c.shown()}", ROLE_Q, list(by_text))
        roles.setdefault(by_text[max(probs, key=probs.get)], []).append(c)
    if roles.get("amount"):
        mapping["amount"] = roles["amount"][0].name
    elif roles.get("money_out") or roles.get("money_in"):
        for k in ("money_out", "money_in"):
            if roles.get(k):
                mapping[k] = roles[k][0].name
    else:
        raise Unusable("no column holds the movements' amounts")
    if roles.get("balance"):
        mapping["balance"] = roles["balance"][0].name
    codes = [c for c in texts if all(re.fullmatch(r"[A-Z]{3}", v) for v in c.values)]
    if codes:
        mapping["currency"] = codes[0].name
    # A counterparty varies from row to row: a column with one value throughout ('Main', the account's own IBAN)
    # isn't one, and asking about it only makes the choice longer.
    texts = [c for c in texts if c not in codes and (len(set(c.values)) > 1 or len(c.values) == 1)]
    if not texts:
        raise Unusable("no text column names the counterparty")
    mapping["description"] = (texts[0] if len(texts) == 1 else
                              max(texts, key=lambda c: yes(f"The column: {c.shown()}", COUNTERPARTY_Q))).name
    scored = {w: yes(f"The word: {w}", BANK_Q) for w in _bank_words(rows, h, name)}
    bank = max(scored, key=scored.get) if scored and max(scored.values()) >= 0.5 else ""
    account = re.sub(r"[^a-z0-9_]+", "_", str(bank).lower()).strip("_")[:24] or "bank"
    taken = {p.account for p in profiles()}
    if account in taken:  # another bank's format already has this name: keep both
        account = next(f"{account}{n}" for n in range(2, 99) if f"{account}{n}" not in taken)
    date_values = next(c.values for c in dates if c.name == mapping["date"])
    amounts = [v for c in money for v in c.values]
    return Profile(account, [_norm(c) for c in rows[h] if c.strip()], mapping, _day_first(date_values),
                   _decimal(amounts), year_first=all((d := _date_parts(v)) and d[0] > 31 for v in date_values))


# ---- reading ------------------------------------------------------------------------------------------------------
@dataclass
class Read:
    """A file read with a profile: what importer.Statement needs, and what the offer shows."""

    txs: list[Transaction]
    balances: list[tuple[str, date, int]]
    checks: list[str]
    skipped: dict[str, int]


def read(path: Path, p: Profile, rows: list[list[str]] | None = None) -> Read:
    """Every movement in the file, with the checks; Unusable when the mapping doesn't read it whole."""
    rows = table(path) if rows is None else rows
    h = _header(rows, p.signature)
    if h is None:
        raise Unusable("its header row isn't the one this bank's format was saved with")
    col = {_norm(c): j for j, c in enumerate(rows[h])}
    m = p.mapping

    def get(r, key):
        name = m.get(key)
        j = col.get(_norm(name)) if name else None
        return r[j] if j is not None and j < len(r) else ""

    txs, bals, skipped, bad = [], [], {}, []
    for i, r in enumerate(rows[h + 1:], h + 1):
        if not any(r):
            continue
        day = _to_date(get(r, "date"), p.day_first)
        # Whatever amount columns are mapped add up: a signed amount, or money in and money out.
        parts = [(_to_cents(get(r, k), p.decimal), k) for k in ("amount", "money_in", "money_out") if m.get(k)]
        parts = [(c if k == "amount" else abs(c) if k == "money_in" else -abs(c)) for c, k in parts if c is not None]
        cents = sum(parts) if parts else None
        if day is None:
            skipped["undated"] = skipped.get("undated", 0) + 1  # a footer, a subtotal, a second header
            continue
        if cents is None:
            bad.append(i)
            continue
        ccy = get(r, "currency").upper()
        if ccy and ccy != "EUR":
            skipped[f"in {ccy}"] = skipped.get(f"in {ccy}", 0) + 1
            continue
        desc, details = get(r, "description"), get(r, "details")
        txs.append(Transaction(account=p.account, booked_on=day, value_on=None, amount_cents=cents,
                               description=desc, details=details, source=path.name, source_format=p.format,
                               merchant=merchants.normalise(desc) if desc else None))
        if (b := _to_cents(get(r, "balance"), p.decimal)) is not None:
            bals.append((day, b, cents))
    if bad:
        raise Unusable(f"{len(bad)} dated rows have no amount I can read (first: row {bad[0] + 1})")
    if not txs:
        raise Unusable("no dated movements")
    checks = []
    if m.get("balance"):
        if len(bals) != len(txs):
            raise Unusable("the balance column is empty on some movements")

        def chains(seq):
            return all(prev[1] + cur[2] == cur[1] for prev, cur in zip(seq, seq[1:]))

        order = bals if chains(bals) else bals[::-1] if chains(bals[::-1]) else None
        if order is None:
            raise Unusable("the running balance doesn't add up in either order: rows are missing, or the columns "
                           "aren't what they seem")
        checks.append(f"{p.account}: running balance consistent over {len(txs)} movements ✅")
        last: dict[date, int] = {}
        for day, b, _ in order:
            last[day] = b  # the day's last balance, in the file's own time order
        bals_out = [(p.account, d, b) for d, b in last.items()]
    else:
        checks.append(f"{p.account}: no balance column, completeness not checked ⚠️")
        bals_out = []
    return Read(txs, bals_out, checks, skipped)


def offer(path: Path, p: Profile, got: Read) -> str:
    """What the user reads before the tap: the columns, the period, the totals, the first rows."""
    cols = ", ".join(f"{k} ← '{v}'" for k, v in p.mapping.items() if v)
    days = [t.booked_on for t in got.txs]
    inn = sum(t.amount_cents for t in got.txs if t.amount_cents > 0)
    out = sum(t.amount_cents for t in got.txs if t.amount_cents < 0)
    first = sorted(got.txs, key=lambda t: t.booked_on)[:3]
    order = "." if p.year_first else f"; dates {'day' if p.day_first else 'month'} first."
    return "\n".join([
        f"📄 {path.name} looks like a statement from a bank I don't know yet.",
        f"Read as account '{p.account}': {len(got.txs)} movements, {min(days):%d/%m/%Y} → {max(days):%d/%m/%Y}, "
        f"in {inn / 100:+,.2f} €, out {out / 100:,.2f} €.",
        f"Columns: {cols}; decimal '{p.decimal}'{order}",
        *got.checks,
        *[f"Skipped {n} {why} rows." for why, n in got.skipped.items()],
        "First movements: " + "; ".join(f"{t.booked_on:%d/%m} {t.amount_cents / 100:+.2f} {t.description[:30]}"
                                         for t in first),
        "Tap to import it. Files from this bank are then read the same way, without asking."])
