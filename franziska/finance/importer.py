"""Bank statement parsers: Intesa Sanpaolo's 'Lista Operazione' and 'Lista Movimenti' .xlsx, Revolut's statement.

Each parser returns a Statement: the movements, the balances the bank states, and the checks that prove the file is
complete. A file whose own balances don't add up is refused (ImportFormatError), never imported in part.

Intesa 'Lista Movimenti' (as of the May 2026 export): a metadata block with 'Saldo contabile iniziale al: <date>
<amount>' and '... finale al:', then a header row starting with 'Data contabile', then one row per movement: Data
contabile, Data valuta, Descrizione, Accrediti, Addebiti (already negative), Descrizione estesa. Check: opening
balance + movements = closing balance, to the cent.

Intesa 'Lista Operazione' (the authoritative Intesa format since 2026-10-28; the web's Entrate/Uscite export): a
metadata block ('I movimenti selezionati sono: <n>', 'Data inizio/fine periodo'), then a header row Data, Operazione,
Dettagli, Conto o carta, Contabilizzazione, Categoria, Valuta, Importo (the table may start in column B). `Data` is
the operation day, which is Lista Movimenti's 'Data valuta' (checked on 472 movements, Oct 2025 – May 2026); the
booking day is not given. No balances, so the check is the stated count = the rows read. Pending rows (NON
CONTABILIZZATO) are skipped until a later export shows them booked. Beware: the page exports only what it has
loaded, so a file can state a long period and hold a few weeks; its coverage starts at its first movement.

Revolut (en-us, Feb–Oct 2026): Type, Product, Started Date, Completed Date, Description, Amount, Fee, Currency,
State, Balance. Product 'Current' is the main account, 'Deposit' the Instant Access Savings. Balance = previous
Balance + Amount − Fee on every completed row of a product (checked, in Completed Date order); only COMPLETED rows
count (a REVERTED card payment never moved money). Non-EUR rows are skipped and counted.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl

from franziska.finance import mapped, merchants
from franziska.finance.ledger import Transaction

INTESA = "intesa_movimenti"
INTESA_OPS = "intesa_operazioni"
REVOLUT = "revolut"
# Which format wins where two cover the same days of an account (service.import_file replaces the weaker rows).
AUTHORITY = {INTESA_OPS: 2, INTESA: 1, REVOLUT: 1}  # a mapped format (mapped.py): 1
HEADER = "Data contabile"
OPS_HEADER = ["Data", "Operazione", "Dettagli", "Conto o carta", "Contabilizzazione", "Categoria", "Valuta", "Importo"]
REVOLUT_HEADER = ["Type", "Product", "Started Date", "Completed Date", "Description", "Amount", "Fee", "Currency",
                  "State", "Balance"]
REVOLUT_ACCOUNTS = {"Current": "revolut", "Deposit": "revolut_savings"}
REVOLUT_KINDS = {"Card Payment": "card", "Rev Payment": "card", "Card Refund": "refund", "Interest": "interest",
                 "Deposit": "topup"}  # Transfer: transfer_in / transfer_out by sign
# Any other type is also counted by its sign, and the import says so (a new type may need a kind of its own).
REVOLUT_BY_SIGN = {"Transfer"}


class ImportFormatError(ValueError):
    pass


@dataclass
class Statement:
    format: str
    txs: list[Transaction]
    balances: list[tuple[str, date, int]] = field(default_factory=list)  # (account, day, cents) as the bank states
    checks: list[str] = field(default_factory=list)  # what was verified, one line each
    skipped: dict[str, int] = field(default_factory=dict)  # reason → rows
    period: tuple[date, date] | None = None  # the days the file covers for its accounts (first movement → stated end)


def _cents(value) -> int:
    return round(float(value) * 100) if value not in (None, "") else 0


def detect(path: Path) -> str | None:
    """INTESA, REVOLUT, a mapped bank's 'mapped:<account>' (mapped.py), or None when the file isn't a statement we
    know. Raises for a known but unusable export."""
    fmt = _builtin(path)
    if fmt is None and (profile := mapped.detect(path)):
        return profile.format
    return fmt


def _builtin(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
            first = next(csv.reader(f), [])
        return REVOLUT if first[:len(REVOLUT_HEADER)] == REVOLUT_HEADER else None
    if suffix != ".xlsx":
        return None
    wb = openpyxl.load_workbook(path, data_only=True)
    if _ops_header(list(wb.active.iter_rows(max_row=60, values_only=True))):
        return INTESA_OPS
    for row in wb.active.iter_rows(max_row=60, values_only=True):
        if row and row[0] == HEADER:
            return INTESA
        if row and [str(c) for c in row[:len(REVOLUT_HEADER)]] == REVOLUT_HEADER:
            return REVOLUT
    return None


def read(path: Path) -> Statement:
    fmt = detect(path)
    if fmt == INTESA_OPS:
        return read_operazioni(path)
    if fmt == INTESA:
        return read_movimenti(path)
    if fmt == REVOLUT:
        return read_revolut(path)
    if fmt and fmt.startswith(mapped.PREFIX):
        return read_mapped(path, next(p for p in mapped.profiles() if p.format == fmt))
    raise ImportFormatError(f"{path.name}: not a statement I know (Intesa .xlsx, Revolut .csv, or a bank whose format "
                            "was saved: send its export to the bot once).")


def read_mapped(path: Path, profile: mapped.Profile) -> Statement:
    try:
        got = mapped.read(path, profile)
    except mapped.Unusable as e:
        raise ImportFormatError(f"{path.name}: read as {profile.account}, but {e}.") from None
    return Statement(profile.format, got.txs, got.balances, got.checks, got.skipped)


def _ops_header(rows: list[tuple]) -> tuple[int, int] | None:
    """(row, first column) of the Lista Operazione header, or None."""
    for i, r in enumerate(rows):
        cells = [str(c).strip() if c is not None else "" for c in r]
        for j in range(len(cells)):
            if cells[j:j + len(OPS_HEADER)] == OPS_HEADER:
                return i, j
    return None


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value).strip().replace("/", "."), "%d.%m.%Y").date()


def _ops_kind(category: str, details: str) -> str | None:
    if category.startswith("Bonifici in uscita"):
        return "transfer_out"
    if category.startswith("Bonifici in entrata") or "A VOSTRO FAVORE" in details.upper():
        return "transfer_in"
    if re.search(r"MEDIANTE LA CARTA|CARTA N\.?\s*\d", details, re.IGNORECASE):
        return "card"
    return None


def read_operazioni(path: Path) -> Statement:
    rows = list(openpyxl.load_workbook(path, data_only=True).active.iter_rows(values_only=True))
    h, j = _ops_header(rows)
    meta = {}
    for r in rows[:h]:
        cells = [c for c in r if c not in (None, "")]
        for k, c in enumerate(cells[:-1]):
            if isinstance(c, str) and c.strip().endswith(":"):
                meta.setdefault(c.strip(), cells[k + 1])
    st = Statement(INTESA_OPS, [])
    read = 0
    for r in rows[h + 1:]:
        if len(r) <= j or r[j] in (None, ""):
            continue
        on, op, details, where, status, category, currency, amount = r[j:j + 8]
        read += 1
        if status != "CONTABILIZZATO":
            st.skipped["pending (not booked yet)"] = st.skipped.get("pending (not booked yet)", 0) + 1
            continue
        if currency != "EUR":
            st.skipped[f"in {currency}"] = st.skipped.get(f"in {currency}", 0) + 1
            continue
        account = "intesa" if str(where).startswith("Conto") else "intesa_card"
        op, details, category = (op or "").strip(), (details or "").strip(), (category or "").strip()
        st.txs.append(Transaction(
            account=account, booked_on=_day(on), value_on=_day(on), amount_cents=_cents(amount), description=op,
            details=details, source=path.name, kind=_ops_kind(category, details), bank_category=category or None,
            source_format=INTESA_OPS, merchant=merchants.of(op, details, INTESA_OPS, None)))
    stated = meta.get("I movimenti selezionati sono:")
    if stated is None or int(stated) != read:
        raise ImportFormatError(f"{path.name}: the file says {stated} movements but holds {read}; export it again.")
    if not st.txs:
        raise ImportFormatError(f"{path.name}: no booked EUR movements")
    end = _day(meta["Data fine periodo:"]) if "Data fine periodo:" in meta else max(t.booked_on for t in st.txs)
    st.period = (min(t.booked_on for t in st.txs), end)
    st.checks.append(f"intesa: {read} movements, as many as the file states ✅ (covers {st.period[0]:%d/%m/%Y} – "
                     f"{end:%d/%m/%Y})")
    return st


def read_movimenti(path: Path) -> Statement:
    rows = list(openpyxl.load_workbook(path, data_only=True).active.iter_rows(values_only=True))
    try:
        h = next(i for i, r in enumerate(rows) if r and r[0] == HEADER)
    except StopIteration:
        raise ImportFormatError(f"{path.name}: no '{HEADER}' header row; is this a Lista Movimenti export?") from None
    stated = {}  # 'iniziale' / 'finale' → (day, cents)
    for r in rows[:h]:
        label = str(r[2] or "") if r and len(r) > 4 else ""
        if m := re.match(r"Saldo contabile (iniziale|finale) al:", label):
            stated[m.group(1)] = (datetime.strptime(str(r[3]), "%d.%m.%Y").date(), _cents(r[4]))
    txs = []
    for booked, value_on, desc, credit, debit, details, *_ in rows[h + 1:]:
        if not isinstance(booked, datetime):
            continue
        txs.append(Transaction(
            account="intesa", booked_on=booked.date(),
            value_on=value_on.date() if isinstance(value_on, datetime) else None,
            amount_cents=_cents(credit) + _cents(debit), description=(desc or "").strip(),
            details=(details or "").strip(), source=path.name, source_format=INTESA))
    if not txs:
        raise ImportFormatError(f"{path.name}: header found but no dated movements")
    st = Statement(INTESA, txs)
    if {"iniziale", "finale"} <= stated.keys():
        (d0, opening), (d1, closing) = stated["iniziale"], stated["finale"]
        total = sum(t.amount_cents for t in txs)
        if opening + total != closing:
            raise ImportFormatError(
                f"{path.name}: opening balance + movements ≠ closing balance (off by {(opening + total - closing) / 100:.2f} €): "
                "the file is incomplete; export it again.")
        st.balances = [("intesa", d0, opening), ("intesa", d1, closing)]
        st.period = (d0, d1)
        st.checks.append(f"intesa: opening {d0:%d/%m/%Y} + {len(txs)} movements = closing {d1:%d/%m/%Y} ✅")
    else:
        st.checks.append("intesa: no opening/closing balance in the file, completeness not checked ⚠️")
    return st


def _revolut_rows(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    rows = list(openpyxl.load_workbook(path, data_only=True).active.iter_rows(values_only=True))
    h = next(i for i, r in enumerate(rows) if r and [str(c) for c in r[:len(REVOLUT_HEADER)]] == REVOLUT_HEADER)
    return [dict(zip(REVOLUT_HEADER, ("" if c is None else str(c) for c in r))) for r in rows[h + 1:] if r and r[0]]


def read_revolut(path: Path) -> Statement:
    st = Statement(REVOLUT, [])
    last: dict[str, int] = {}
    breaks: dict[str, int] = {}
    counted: dict[str, int] = {}
    unknown: dict[str, int] = {}
    rows = sorted(_revolut_rows(path), key=lambda r: r["Completed Date"] or r["Started Date"])
    for r in rows:
        if r["State"] != "COMPLETED":
            st.skipped[r["State"].lower()] = st.skipped.get(r["State"].lower(), 0) + 1
            continue
        if r["Currency"] != "EUR":
            st.skipped[f"in {r['Currency']}"] = st.skipped.get(f"in {r['Currency']}", 0) + 1
            continue
        account = REVOLUT_ACCOUNTS.get(r["Product"])
        if account is None:
            raise ImportFormatError(f"{path.name}: unknown Revolut product {r['Product']!r} (known: Current, Deposit)")
        amount, fee, balance = _cents(r["Amount"]), _cents(r["Fee"]), _cents(r["Balance"])
        net = amount - fee  # what the movement did to the balance
        if account in last and last[account] + net != balance:
            breaks[account] = breaks.get(account, 0) + 1
        last[account] = balance
        counted[account] = counted.get(account, 0) + 1
        started = datetime.fromisoformat(r["Started Date"])
        completed = datetime.fromisoformat(r["Completed Date"]) if r["Completed Date"] else started
        desc = r["Description"].strip()
        kind = REVOLUT_KINDS.get(r["Type"]) or ("transfer_in" if amount > 0 else "transfer_out")
        if r["Type"] not in REVOLUT_KINDS and r["Type"] not in REVOLUT_BY_SIGN:
            unknown[r["Type"]] = unknown.get(r["Type"], 0) + 1
        st.txs.append(Transaction(
            account=account, booked_on=completed.date(), value_on=None, amount_cents=net, description=desc,
            details=r["Type"], source=path.name, occurred_at=started, kind=kind, fee_cents=fee,
            balance_after_cents=balance, source_format=REVOLUT,
            merchant=merchants.of(desc, r["Type"], REVOLUT, kind)))
        st.balances = [b for b in st.balances if not (b[0] == account and b[1] == completed.date())]
        st.balances.append((account, completed.date(), balance))
    if not st.txs:
        raise ImportFormatError(f"{path.name}: no completed EUR movements")
    if breaks:
        raise ImportFormatError(
            f"{path.name}: the running balance doesn't add up ({', '.join(f'{a}: {n} breaks' for a, n in breaks.items())}):"
            " rows are missing; export the statement again.")
    st.checks += [f"{a}: running balance consistent over {n} movements ✅" for a, n in counted.items()]
    if unknown:
        st.checks.append(f"revolut: ⚠️ {sum(unknown.values())} movements of a type I don't know "
                         f"({', '.join(f'{t} ×{n}' for t, n in unknown.items())}), counted as transfers by their sign")
    return st
