"""Checks text against fingerprints of your own data, read from the live stores: run by .githooks/pre-commit on the
staged added lines, so a real figure or name copied into code, docs or an eval report is caught before it is
committed. The denylist catches identifiers you list by hand; this catches what you would not think to list.

Fingerprints (nothing leaves this machine; they are built in memory on each run):
- amounts with cents from the ledger: movements of 100 € or more, balances, monthly spending and income, monthly
  totals per payee and per spending group (100 € or more), portfolio values and costs; every tracked bill's amount;
- the ISINs you hold or held, and the payees in the 'people & gifts' group (people, not shops).

    python3 -I .githooks/leakscan.py < some.txt        # exit 1 and one line per hit if anything matches
    python3 -I .githooks/leakscan.py --tree            # every tracked file in the repo (slow: about a minute)

Standard library only. No data dir (a fresh clone, CI): nothing to check, exit 0.
"""

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("FRANZISKA_DATA_DIR", Path.home() / ".local/share/franziska")).expanduser()
# A payee left as the bank's transfer wording ('SWIFT transfer') names nobody.
GENERIC = {"SWIFT", "SEPA", "TRANSFER", "PAYMENT", "BONIFICO", "GIROCONTO", "TO", "FROM"}


def _amount(cents: int, floor_cents: int, whole: bool = False) -> str | None:
    """A regex for a euro amount in any common spelling (1234.56, 1.234,56, 1'234.56, 1 234,56)."""
    c = abs(cents)
    if c < floor_cents or (c % 100 == 0 and not whole):
        return None
    euros, rest = divmod(c, 100)
    s = str(euros)
    groups = [s[max(0, i - 3):i] for i in range(len(s), 0, -3)][::-1]
    body = r"[.,' ]?".join(groups)
    if rest:
        return rf"(?<![\d.,]){body}[.,]{rest:02d}(?!\d)"
    # A whole amount only next to a currency mark: '410 €', '410,00 euro', '€ 410' (a bare 300 is everywhere)
    return rf"(?<![\d.,]){body}(?:[.,]00)?\s?(?:€|euro\b|eur\b)|(?:€|\beur)\s?{body}(?![\d.,]*\d)"


def fingerprints() -> dict[str, str]:
    out: dict[str, str] = {}

    def amounts(rows, label, floor_cents, whole=False):
        for (c,) in rows:
            if c is not None and (rx := _amount(c, floor_cents, whole)):
                out.setdefault(rx, label)

    ledger = DATA / "ledger.sqlite"
    if not ledger.exists():
        return out
    con = sqlite3.connect(f"file:{ledger}?mode=ro", uri=True)

    def q(sql):
        try:
            return con.execute(sql).fetchall()
        except sqlite3.OperationalError:  # an older ledger without that table
            return []

    amounts(q("select amount_cents from transactions"), "a movement", 10000)
    amounts(q("select cents from balances"), "a balance", 1000)
    amounts(q("select sum(amount_cents) from transactions where amount_cents < 0 group by substr(booked_on, 1, 7)"),
            "a month's spending", 1000)
    amounts(q("select sum(amount_cents) from transactions where amount_cents > 0 group by substr(booked_on, 1, 7)"),
            "a month's income", 1000)
    amounts(q("select sum(amount_cents) from transactions where payee is not null "
              "group by substr(booked_on, 1, 7), payee"), "a payee's monthly total", 10000)
    amounts(q("select sum(amount_cents) from transactions group by substr(booked_on, 1, 7), category"),
            "a spending group's monthly total", 10000)
    amounts(q("select amount_cents from bills"), "a bill", 1000, whole=True)
    for col in ("local_cents", "eur_cents"):
        amounts(q(f"select {col} from inv_snapshots"), "a portfolio value", 1000)
    for col in ("cost_cents", "realized_cents"):
        amounts(q(f"select {col} from inv_positions"), "a position's cost or gain", 1000)
    for (isin,) in q("select isin from inv_instruments"):
        out.setdefault(re.escape(isin), "an ISIN you hold or held")
    for (name,) in q("select payee from payees where category = 'people & gifts'"):
        name = re.sub(r"^PAYPAL \*", "", (name or "").strip())
        if len(name) >= 6 and not set(name.upper().split()) <= GENERIC:
            out.setdefault(r"\b" + re.escape(name) + r"\b", "a person you paid or were paid by")
    return out


def scan(lines, fps) -> list[str]:
    compiled = [(re.compile(rx, re.IGNORECASE), label) for rx, label in fps.items()]
    hits = []
    for where, line in lines:
        for rx, label in compiled:
            if m := rx.search(line):
                hits.append(f"{where}: {label}: {m.group(0)!r}  | {line.strip()[:120]}")
    return hits


def main() -> int:
    fps = fingerprints()
    if not fps:
        return 0
    if sys.argv[1:] == ["--tree"]:
        files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
        skip = re.compile(r"(\.lock$|package-lock\.json$|/vendor/|\.(svg|png|gguf|ico)$)")
        lines = []
        for name in files:
            if skip.search(name):
                continue
            try:
                lines += [(f"{name}:{i}", t) for i, t in enumerate(Path(name).read_text(errors="ignore").splitlines(), 1)]
            except (IsADirectoryError, FileNotFoundError):
                pass
    else:
        lines = [(f"line {i}", t) for i, t in enumerate(sys.stdin.read().splitlines(), 1)]
    hits = scan(lines, fps)
    for h in hits:
        print(h)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
