"""Coding-agent tasks: a tool loop in a scratch workspace (list, read, write, run python), judged on the files."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys

from evals.tasks.base import ENV, HARNESS, Task
from evals.tasks.worlds import Workspace

CODER = "You are a coding agent working in a project directory. Use the tools; when done, reply with one sentence."


INVENTORY = '''class Inventory:
    def __init__(self):
        self.items = {}  # name -> {"qty": int, "price": float}

    def add(self, name, qty, price):
        """Add stock. Adding an existing item increases its quantity and sets the new price."""
        if name in self.items:
            self.items[name]["qty"] = qty
            self.items[name]["price"] = price
        else:
            self.items[name] = {"qty": qty, "price": price}

    def remove(self, name, qty):
        item = self.items[name]
        if qty > item["qty"]:
            raise ValueError("not enough stock")
        item["qty"] -= qty
        if item["qty"] == 0:
            del self.items[name]

    def low_stock(self, threshold):
        """Names of items with fewer than `threshold` units, alphabetical."""
        return sorted(n for n, i in self.items.items() if i["qty"] <= threshold)

    def total_value(self):
        return round(sum(i["qty"] * i["price"] for i in self.items.values()), 2)
'''
REPORT = '''def format_report(inv):
    """One line per item, alphabetical by name: name padded to 8, quantity right-aligned in 4, 'x', price with 2 decimals."""
    lines = []
    for name, item in inv.items.items():
        lines.append(f"{name:<8}{item['qty']:>4} x {item['price']:.1f}")
    return "\\n".join(lines)
'''
INVENTORY_TESTS = '''import sys
from inventory import Inventory
from report import format_report


def make():
    inv = Inventory()
    inv.add("nut", 5, 0.1)
    inv.add("bolt", 10, 0.25)
    inv.add("gear", 2, 12.5)
    return inv


def test_add_existing_adds_quantity():
    inv = make(); inv.add("bolt", 5, 0.3)
    assert inv.items["bolt"] == {"qty": 15, "price": 0.3}, inv.items["bolt"]


def test_remove():
    inv = make(); inv.remove("bolt", 4)
    assert inv.items["bolt"]["qty"] == 6


def test_remove_all_deletes():
    inv = make(); inv.remove("gear", 2)
    assert "gear" not in inv.items


def test_remove_too_many():
    inv = make()
    try:
        inv.remove("nut", 6)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_low_stock_is_strictly_below():
    assert make().low_stock(5) == ["gear"], make().low_stock(5)
    assert make().low_stock(6) == ["gear", "nut"], make().low_stock(6)


def test_total_value():
    assert make().total_value() == 28.0, make().total_value()


def test_report():
    expected = "bolt      10 x 0.25\\ngear       2 x 12.50\\nnut        5 x 0.10"
    assert format_report(make()) == expected, repr(format_report(make()))


failed = 0
for name, fn in sorted(globals().items()):
    if name.startswith("test_"):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
print("all tests passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
'''

WC_SPEC = ("Create `wc.py` in the workspace: a command-line script that takes one or more file paths as arguments. For each "
           "file print one line `<lines> <words> <chars> <path>` (lines = number of newline characters, words = "
           "whitespace-separated words, chars = characters), separated by single spaces. If more than one file was given, "
           "end with a line `<lines> <words> <chars> total` summing the files that could be read. A file that cannot be "
           "read gets a message on stderr, is skipped, and makes the script exit with code 1 after all files were "
           "processed; otherwise exit 0. Run it yourself on a few test files you create to check it works.")


def judge_inventory(ws: Workspace, final: str, life) -> tuple[float, str]:
    unchanged = hashlib.sha256((ws.dir / "test_inventory.py").read_bytes()).hexdigest() == hashlib.sha256(INVENTORY_TESTS.encode()).hexdigest()
    p = ws.run_script("test_inventory.py")
    out = (p.stdout + p.stderr).strip().replace("\n", " | ")[-250:]
    if not unchanged:
        return 0.0, "edited the test file"
    return (1.0 if p.returncode == 0 else 0.0), out


def judge_wc(ws: Workspace, final: str, life) -> tuple[float, str]:
    (ws.dir / "chk_a.txt").write_text("hello world\nfoo\n")
    (ws.dir / "chk_b.txt").write_text("")
    (ws.dir / "chk_c.txt").write_text("one two three\n  four\n\nfive six")
    if not (ws.dir / "wc.py").exists():
        return 0.0, "no wc.py"
    norm = lambda s: [" ".join(l.split()) for l in s.strip().splitlines()]
    results = []
    try:
        one = ws.run_script("wc.py", "chk_a.txt")
        results.append(("single", one.returncode == 0 and norm(one.stdout) == ["2 3 16 chk_a.txt"]))
        many = ws.run_script("wc.py", "chk_a.txt", "chk_b.txt", "chk_c.txt")
        results.append(("total", many.returncode == 0 and norm(many.stdout) == [
            "2 3 16 chk_a.txt", "0 0 0 chk_b.txt", "3 6 30 chk_c.txt", "5 9 46 total"]))
        miss = ws.run_script("wc.py", "chk_a.txt", "nope.txt", "chk_b.txt")
        results.append(("missing", miss.returncode == 1 and bool(miss.stderr.strip())
                        and norm(miss.stdout) == ["2 3 16 chk_a.txt", "0 0 0 chk_b.txt", "2 3 16 total"]))
    except subprocess.TimeoutExpired:
        return 0.0, "wc.py timed out"
    return sum(ok for _, ok in results) / 3, "failed: " + ", ".join(n for n, ok in results if not ok) if not all(ok for _, ok in results) else ""


TASKLIB = """from dataclasses import dataclass
from datetime import date


@dataclass
class Task:
    id: int
    title: str
    due: date | None = None
    done: bool = False


class TaskList:
    def __init__(self):
        self.tasks: dict[int, Task] = {}
        self._next = 1

    def add(self, title, due=None):
        t = Task(self._next, title, due)
        self.tasks[t.id] = t
        self._next += 1
        return t.id

    def complete(self, task_id):
        self.tasks[task_id].done = True

    def due_on(self, day):
        return sorted(t.title for t in self.tasks.values() if not t.done and t.due == day)

    def overdue(self, today):
        return sorted(t.title for t in self.tasks.values() if not t.done and t.due and t.due < today)
"""
SUMMARY = """def summary(tl, today):
    # One line for the daily digest: how many tasks are open and how many of them are overdue.
    open_ = [t for t in tl.tasks.values() if not t.done]
    return f"{len(open_)} open, {len(tl.overdue(today))} overdue"
"""
TASK_TESTS = """import sys
from datetime import date
from tasks import TaskList
from summary import summary


def test_plain_task():
    tl = TaskList(); i = tl.add("a", date(2026, 1, 5)); tl.complete(i)
    assert tl.tasks[i].done and tl.due_on(date(2026, 1, 5)) == []


def test_due_and_overdue():
    tl = TaskList(); tl.add("a", date(2026, 1, 5)); tl.add("b", date(2026, 1, 1)); tl.add("c")
    assert tl.due_on(date(2026, 1, 5)) == ["a"] and tl.overdue(date(2026, 1, 5)) == ["b"]


def test_summary_plain():
    tl = TaskList(); tl.add("a", date(2026, 1, 1)); i = tl.add("b"); tl.complete(i)
    assert summary(tl, date(2026, 1, 5)) == "1 open, 1 overdue", summary(tl, date(2026, 1, 5))


def test_daily():
    tl = TaskList(); i = tl.add("a", date(2026, 3, 1), repeat="daily"); tl.complete(i)
    assert tl.tasks[i].due == date(2026, 3, 2) and not tl.tasks[i].done


def test_weekly():
    tl = TaskList(); i = tl.add("a", date(2026, 3, 1), repeat="weekly"); tl.complete(i)
    assert tl.tasks[i].due == date(2026, 3, 8)


def test_monthly_simple():
    tl = TaskList(); i = tl.add("a", date(2026, 1, 15), repeat="monthly"); tl.complete(i)
    assert tl.tasks[i].due == date(2026, 2, 15)


def test_completions():
    tl = TaskList(); i = tl.add("a", date(2026, 3, 1), repeat="daily"); tl.complete(i); tl.complete(i)
    assert tl.tasks[i].completions == [date(2026, 3, 1), date(2026, 3, 2)], tl.tasks[i].completions


def test_bad_repeat():
    try:
        TaskList().add("a", date(2026, 1, 1), repeat="yearly")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


failed = 0
for name, fn in sorted(globals().items()):
    if name.startswith("test_"):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
print("all tests passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
"""
HIDDEN = r"""
from datetime import date
from _h import check, raises, done
from tasks import TaskList
from summary import summary
def cyc(rep, due, n):
    tl = TaskList(); i = tl.add("x", due, repeat=rep); out = []
    for _ in range(n):
        tl.complete(i); out.append(tl.tasks[i].due)
    return out
check("daily year end", lambda: cyc("daily", date(2026, 12, 31), 2) == [date(2027, 1, 1), date(2027, 1, 2)])
check("weekly month end", lambda: cyc("weekly", date(2026, 2, 25), 1) == [date(2026, 3, 4)])
check("monthly keeps anchor day", lambda: cyc("monthly", date(2026, 1, 31), 3) == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)])
check("monthly leap february", lambda: cyc("monthly", date(2028, 1, 31), 2) == [date(2028, 2, 29), date(2028, 3, 31)])
check("monthly december", lambda: cyc("monthly", date(2026, 12, 15), 1) == [date(2027, 1, 15)])
def completions():
    tl = TaskList(); i = tl.add("x", date(2026, 1, 31), repeat="monthly"); tl.complete(i); tl.complete(i)
    return tl.tasks[i].completions == [date(2026, 1, 31), date(2026, 2, 28)]
check("completions", completions)
def stays_open():
    tl = TaskList(); i = tl.add("x", date(2026, 5, 1), repeat="weekly"); tl.complete(i)
    return not tl.tasks[i].done and tl.due_on(date(2026, 5, 8)) == ["x"] and tl.due_on(date(2026, 5, 1)) == []
check("stays open and moves", stays_open)
def plain_untouched():
    tl = TaskList(); i = tl.add("x", date(2026, 5, 1)); tl.complete(i)
    return tl.tasks[i].done and tl.tasks[i].due == date(2026, 5, 1)
check("plain task still finishes", plain_untouched)
check("bad repeat", lambda: raises(ValueError, TaskList().add, "x", date(2026, 1, 1), repeat="yearly"))
check("repeat needs due", lambda: raises(ValueError, TaskList().add, "x", None, repeat="daily"))
check("repeat none ok", lambda: TaskList().add("x", date(2026, 1, 1), repeat=None) == 1)
def digest():
    tl = TaskList(); i = tl.add("x", date(2026, 11, 1), repeat="daily"); tl.complete(i)
    return summary(tl, date(2026, 11, 4)) == "1 open, 1 overdue"
check("summary counts recurring once", digest)
done()
"""


def judge_recurrence(ws: Workspace, final: str, life) -> tuple[float, str]:
    if hashlib.sha256((ws.dir / "test_tasks.py").read_bytes()).hexdigest() != hashlib.sha256(TASK_TESTS.encode()).hexdigest():
        return 0.0, "edited the test file"
    vis = ws.run_script("test_tasks.py")
    (ws.dir / "_h.py").write_text(HARNESS)
    (ws.dir / "hidden_check.py").write_text(HIDDEN)
    try:
        hid = ws.run_script("hidden_check.py")
    except subprocess.TimeoutExpired:
        return 0.0, "hidden checks timed out"
    m = re.search(r"@@RESULT@@(.*)", hid.stdout)
    frac = 0.0
    failed = (hid.stderr.strip().splitlines() or ["no output"])[-1][:150]
    if m:
        r = json.loads(m.group(1))
        frac, failed = r["passed"] / r["total"], "; ".join(r["failed"])[:250]
    vis_out = (vis.stdout + vis.stderr).strip().replace("\n", " | ")[-150:]
    why = ("" if vis.returncode == 0 else f"visible tests: {vis_out}. ") + (f"hidden: {failed}" if frac < 1 else "")
    return 0.4 * (vis.returncode == 0) + 0.6 * frac, why


OLD = [
    Task("fix_inventory", "agent", "two buggy modules, failing tests, fix the code (3+ bugs)",
         "The tests in test_inventory.py fail. Fix the bugs in the code (inventory.py, report.py), not in the tests, "
         "until `python test_inventory.py` passes. The docstrings describe the intended behaviour.",
         files={"inventory.py": INVENTORY, "report.py": REPORT, "test_inventory.py": INVENTORY_TESTS},
         judge=judge_inventory, system="You are a coding agent working in a project directory. Use the tools; when done, reply with one sentence."),
    Task("build_wc", "agent", "write a CLI from a spec, test it yourself", WC_SPEC, judge=judge_wc,
         system="You are a coding agent working in a project directory. Use the tools; when done, reply with one sentence."),
    Task("add_recurrence", "hard", "add a feature across 2 modules in an existing codebase, with hidden edge cases",
         "Add recurring tasks to this task list library. `TaskList.add(title, due=None, repeat=None)` takes repeat = None, "
         "'daily', 'weekly' or 'monthly'; anything else, or a repeat without a due date, raises ValueError. Completing a "
         "recurring task does not finish it: its due date moves to the next occurrence (+1 day, +7 days, or the same day "
         "of the next month) and it stays open. For monthly, the day of the month is the one the task was created with: if "
         "the next month is shorter use its last day, and the month after goes back to the original day (Jan 31 -> Feb 28 "
         "-> Mar 31). Each task keeps `completions`, the list of due dates it was completed at. Everything else must behave "
         "as before (summary.py too). Do not edit test_tasks.py: make `python test_tasks.py` pass and think about the "
         "cases it does not cover.",
         files={"tasks.py": TASKLIB, "summary.py": SUMMARY, "test_tasks.py": TASK_TESTS}, judge=judge_recurrence,
         system="You are a coding agent working in a project directory. Use the tools; when done, reply with one sentence."),
]


# ---- 2026-11 tasks: the coding jobs the bot's sandbox gets (franziska/code.py, run_python) ---------------------------
BANKCSV = '''import csv
import io
from datetime import datetime


def parse_amount(s):
    """'1.234,56' -> 1234.56, '-12,50' -> -12.5, '+5' -> 5.0 (Italian format: '.' thousands, ',' decimals)."""
    return float(s.replace(",", "."))


def parse(text):
    """The movements of a bank export: ';'-separated, a header row naming the columns Data, Descrizione and Importo
    (in any order, any case, other columns ignored), dates as dd/mm/yyyy, fields may be quoted (a quoted description
    can contain ';'). Exports saved by Excel may start with a BOM and use Windows line endings; blank lines are skipped
    and spaces around fields don't count.
    -> [{"day": date, "description": str, "amount": float}] in file order."""
    rows = []
    lines = text.strip().split("\\n")
    for line in lines[1:]:
        day, desc, amount = line.split(";")
        rows.append({"day": datetime.strptime(day, "%m/%d/%Y").date(), "description": desc.strip(),
                     "amount": parse_amount(amount)})
    return rows


def total(rows):
    """Sum of the amounts, rounded to cents."""
    return sum(r["amount"] for r in rows)
'''
BANKCSV_TESTS = '''import sys
from datetime import date
from bankcsv import parse, parse_amount, total

BASIC = "Data;Descrizione;Importo\\n03/10/2026;Esselunga;-86,40\\n27/10/2026;Stipendio;2.350,00\\n"


def test_amounts():
    assert parse_amount("-12,50") == -12.5
    assert parse_amount("1.234,56") == 1234.56, parse_amount("1.234,56")


def test_day_first():
    assert parse(BASIC)[0]["day"] == date(2026, 10, 3), parse(BASIC)[0]["day"]


def test_basic():
    rows = parse(BASIC)
    assert [r["amount"] for r in rows] == [-86.4, 2350.0], rows
    assert rows[1]["description"] == "Stipendio"


def test_column_order():
    rows = parse("Importo;Data;Descrizione\\n-9,99;07/10/2026;Iliad\\n")
    assert rows == [{"day": date(2026, 10, 7), "description": "Iliad", "amount": -9.99}], rows


def test_quoted_semicolon():
    rows = parse('Data;Descrizione;Importo\\n09/10/2026;"Farmacia; via Roma";-18,70\\n')
    assert rows[0]["description"] == "Farmacia; via Roma", rows


def test_blank_lines():
    assert len(parse("Data;Descrizione;Importo\\n\\n01/10/2026;GTT;-39,00\\n\\n")) == 1


def test_total_rounded():
    rows = parse("Data;Descrizione;Importo\\n01/10/2026;a;0,10\\n02/10/2026;b;0,20\\n")
    assert total(rows) == 0.3, total(rows)


failed = 0
for name, fn in sorted(globals().items()):
    if name.startswith("test_"):
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
print("all tests passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
'''
BANKCSV_HIDDEN = r"""
from datetime import date
from _h import check, raises, done
from bankcsv import parse, parse_amount, total
check("crlf", lambda: [r["amount"] for r in parse("Data;Descrizione;Importo\r\n01/10/2026;GTT;-39,00\r\n02/10/2026;Q8;-60,00\r\n")] == [-39.0, -60.0])
check("bom", lambda: parse("﻿Data;Descrizione;Importo\n01/10/2026;GTT;-39,00\n")[0]["day"] == date(2026, 10, 1))
check("header case + extra column", lambda: parse("DATA;Valuta;descrizione;IMPORTO\n05/10/2026;06/10/2026;Bar;-2,50\n")
      == [{"day": date(2026, 10, 5), "description": "Bar", "amount": -2.5}])
check("spaces around fields", lambda: parse("Data ; Descrizione ; Importo\n 05/10/2026 ; Bar Roma ; -2,50 \n")[0]
      == {"day": date(2026, 10, 5), "description": "Bar Roma", "amount": -2.5})
check("plus and thousands", lambda: parse_amount("+1.000,00") == 1000.0 and parse_amount("5") == 5.0)
check("total rounding", lambda: total([{"amount": 0.1}] * 3) == 0.3)
check("quoted amount", lambda: parse('Data;Descrizione;Importo\n05/10/2026;Bar;"-1.002,50"\n')[0]["amount"] == -1002.5)
done()
"""


def judge_bankcsv(ws: Workspace, final: str, life) -> tuple[float, str]:
    if (ws.dir / "test_bankcsv.py").read_text() != BANKCSV_TESTS:
        return 0.0, "edited the test file"
    vis = ws.run_script("test_bankcsv.py")
    (ws.dir / "_h.py").write_text(HARNESS)
    (ws.dir / "hidden_check.py").write_text(BANKCSV_HIDDEN)
    try:
        hid = ws.run_script("hidden_check.py")
    except subprocess.TimeoutExpired:
        return 0.0, "hidden checks timed out"
    m = re.search(r"@@RESULT@@(.*)", hid.stdout)
    r = json.loads(m.group(1)) if m else {"passed": 0, "total": 1, "failed": [hid.stderr.strip()[-150:]]}
    frac = r["passed"] / r["total"]
    vis_out = (vis.stdout + vis.stderr).strip().replace("\n", " | ")[-150:]
    why = ("" if vis.returncode == 0 else f"visible tests: {vis_out}. ") + (f"hidden: {'; '.join(r['failed'])[:250]}" if frac < 1 else "")
    return 0.5 * (vis.returncode == 0) + 0.5 * frac, why


BANKCSV_FIXED = '''import csv
import io
from datetime import datetime


def parse_amount(s):
    return float(s.strip().replace(".", "").replace(",", "."))


def parse(text):
    text = text.lstrip("\\ufeff")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=";")
    rows, cols = [], None
    for fields in reader:
        fields = [f.strip() for f in fields]
        if not any(fields):
            continue
        if cols is None:
            cols = {name.lower(): i for i, name in enumerate(fields)}
            continue
        rows.append({"day": datetime.strptime(fields[cols["data"]], "%d/%m/%Y").date(),
                     "description": fields[cols["descrizione"]], "amount": parse_amount(fields[cols["importo"]])})
    return rows


def total(rows):
    return round(sum(r["amount"] for r in rows), 2)
'''


def _spese() -> str:
    """60 expenses over 2026, fixed: the data_report workspace."""
    import random
    rnd, cats = random.Random(7), ["spesa", "ristoranti", "trasporti", "casa", "svago", "salute"]
    rows = ["data,categoria,importo"]
    for _ in range(60):
        m, d = rnd.randint(1, 10), rnd.randint(1, 28)
        rows.append(f"2026-{m:02d}-{d:02d},{rnd.choice(cats)},{rnd.randint(300, 15000) / 100:.2f}")
    return "\n".join(rows) + "\n"


SPESE = _spese()


def _spese_facts() -> tuple[dict[str, float], tuple[str, float]]:
    cats: dict[str, float] = {}
    months: dict[str, float] = {}
    for line in SPESE.strip().splitlines()[1:]:
        d, c, a = line.split(",")
        cats[c] = round(cats.get(c, 0) + float(a), 2)
        months[d[:7]] = round(months.get(d[:7], 0) + float(a), 2)
    top = max(months.items(), key=lambda kv: kv[1])
    return dict(sorted(cats.items(), key=lambda kv: -kv[1])), top


MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
        "novembre", "dicembre"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]


def judge_report(ws: Workspace, final: str, life) -> tuple[float, str]:
    from evals.tasks.home import amount_in, said
    f = ws.dir / "report.md"
    if not f.exists():
        return 0.0, "no report.md"
    text = f.read_text()
    cats, (month, mtotal) = _spese_facts()
    lines = text.lower().splitlines()
    where = {c: next((i for i, l in enumerate(lines) if c in l and amount_in(l, v)), None) for c, v in cats.items()}
    m = int(month[5:])
    checks = {
        "every category total": all(i is not None for i in where.values()),
        "highest first": all(i is not None for i in where.values()) and list(where.values()) == sorted(where.values()),
        "top month and its total": said(text, MESI[m - 1], MONTHS[m - 1], month, f"{m:02d}/2026") and amount_in(text, mtotal),
        "data untouched": (ws.dir / "spese.csv").read_text() == SPESE,
    }
    return sum(checks.values()) / len(checks), ("failed: " + ", ".join(k for k, v in checks.items() if not v)) if not all(checks.values()) else ""


def _ref_report(ws: Workspace) -> str:
    cats, (month, mtotal) = _spese_facts()
    (ws.dir / "report.md").write_text("# Spese 2026\n\n| categoria | totale |\n|---|---|\n"
                                      + "".join(f"| {c} | {v:.2f} € |\n" for c, v in cats.items())
                                      + f"\nMese con più spesa: {MESI[int(month[5:]) - 1]} ({month}), {mtotal:.2f} €.\n")
    return "Fatto: report.md."


TODO = '''"""A tiny to-do list on the command line. The data lives in todo.json in the current directory."""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

DB = Path("todo.json")


def load():
    return json.loads(DB.read_text()) if DB.exists() else {"next": 1, "items": []}


def save(data):
    DB.write_text(json.dumps(data, indent=1))


def line(it):
    mark = "x" if it["done"] else " "
    due = f" (due {it['due']})" if it.get("due") else ""
    return f"[{mark}] {it['id']}: {it['title']}{due}"


def main(argv=None):
    p = argparse.ArgumentParser(description="to-do list")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("title")
    a.add_argument("--due", help="YYYY-MM-DD")
    sub.add_parser("list")
    d = sub.add_parser("done")
    d.add_argument("id", type=int)
    args = p.parse_args(argv)
    data = load()
    if args.cmd == "add":
        if args.due:
            date.fromisoformat(args.due)
        it = {"id": data["next"], "title": args.title, "due": args.due, "done": False}
        data["next"] += 1
        data["items"].append(it)
        save(data)
        print(f"added {it['id']}")
    elif args.cmd == "list":
        for it in data["items"]:
            print(line(it))
    elif args.cmd == "done":
        for it in data["items"]:
            if it["id"] == args.id:
                it["done"] = True
                save(data)
                print(f"done {args.id}")
                return 0
        print(f"no item {args.id}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''
# written by an older version: item 2 has no "due" key at all
TODO_JSON = json.dumps({"next": 6, "items": [
    {"id": 1, "title": "Pagare il bollo", "due": "2026-11-30", "done": False},
    {"id": 2, "title": "Chiamare la banca", "done": False},
    {"id": 3, "title": "Report per Laura", "due": "2026-11-09", "done": False},
    {"id": 4, "title": "Rinnovare la carta", "due": "2026-10-01", "done": True},
    {"id": 5, "title": "Prenotare il dentista", "due": "2026-11-02", "done": False}]}, indent=1)
TODO_SPEC = ("Add to todo.py: (1) `list --open` shows only the items not done, in the same format as `list`; (2) a new "
             "command `overdue [--today YYYY-MM-DD]` that prints the open items whose due date is before today (default: "
             "the real date today), sorted by due date and then id, in the same line format as `list`, and exits with code "
             "1 if it printed any, 0 if none (so scripts can use it); (3) `add` with a --due that is not a valid "
             "YYYY-MM-DD date must print an error on stderr and exit with code 2 without saving anything (now it "
             "crashes). Everything else must keep working, including todo.json files written by older versions like the "
             "one here. Try it yourself; leave todo.json as it is when you are done.")


def judge_todo(ws: Workspace, final: str, life) -> tuple[float, str]:
    import shutil
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp(prefix="bench-todo-"))
    try:
        if not (ws.dir / "todo.py").exists():
            return 0.0, "no todo.py"
        shutil.copy(ws.dir / "todo.py", d / "todo.py")
        (d / "todo.json").write_text(TODO_JSON)
        run = lambda *a: subprocess.run([sys.executable, "todo.py", *a], cwd=d, capture_output=True, text=True, timeout=30, env=ENV)
        out = lambda p: [l.rstrip() for l in p.stdout.strip().splitlines()]
        checks: dict[str, bool] = {}
        lst = run("list")
        checks["list unchanged"] = lst.returncode == 0 and out(lst) == [
            "[ ] 1: Pagare il bollo (due 2026-11-30)", "[ ] 2: Chiamare la banca", "[ ] 3: Report per Laura (due 2026-11-09)",
            "[x] 4: Rinnovare la carta (due 2026-10-01)", "[ ] 5: Prenotare il dentista (due 2026-11-02)"]
        op = run("list", "--open")
        checks["list --open"] = op.returncode == 0 and out(op) == [x for x in out(lst) if not x.startswith("[x]")]
        ov = run("overdue", "--today", "2026-11-10")
        checks["overdue sorted, exit 1"] = ov.returncode == 1 and out(ov) == [
            "[ ] 5: Prenotare il dentista (due 2026-11-02)", "[ ] 3: Report per Laura (due 2026-11-09)"]
        none = run("overdue", "--today", "2026-11-01")
        checks["none overdue, exit 0"] = none.returncode == 0 and out(none) == []
        before = (d / "todo.json").read_text()
        bad = run("add", "x", "--due", "2026-13-01")
        checks["bad date: exit 2, stderr, nothing saved"] = bad.returncode == 2 and bool(bad.stderr.strip()) \
            and (d / "todo.json").read_text() == before and "Traceback" not in bad.stderr
        ok = run("add", "Spesa", "--due", "2026-11-20")
        dn = run("done", "6")
        checks["add and done still work"] = ok.returncode == 0 and dn.returncode == 0 and \
            "[x] 6: Spesa (due 2026-11-20)" in out(run("list"))
        same = run("overdue", "--today", "2026-11-09")
        checks["strictly before today"] = same.returncode == 1 and out(same) == ["[ ] 5: Prenotare il dentista (due 2026-11-02)"]
    except subprocess.TimeoutExpired:
        return 0.0, "todo.py timed out"
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return sum(checks.values()) / len(checks), ("failed: " + ", ".join(k for k, v in checks.items() if not v)) if not all(checks.values()) else ""


TODO_FIXED = TODO.replace('''    sub.add_parser("list")''', '''    ls = sub.add_parser("list")
    ls.add_argument("--open", action="store_true")
    ov = sub.add_parser("overdue")
    ov.add_argument("--today")''').replace('''        if args.due:
            date.fromisoformat(args.due)''', '''        if args.due:
            try:
                date.fromisoformat(args.due)
            except ValueError:
                print(f"not a date: {args.due}", file=sys.stderr)
                return 2''').replace('''        for it in data["items"]:
            print(line(it))''', '''        for it in data["items"]:
            if not (args.open and it["done"]):
                print(line(it))
    elif args.cmd == "overdue":
        today = (args.today or date.today().isoformat())
        late = sorted((it for it in data["items"] if not it["done"] and it.get("due") and it["due"] < today),
                      key=lambda it: (it["due"], it["id"]))
        for it in late:
            print(line(it))
        return 1 if late else 0''')


def _write(**files):
    def run(ws: Workspace) -> str:
        for name, text in files.items():
            (ws.dir / name).write_text(text)
        return "Done."
    return run


NEW = [
    Task("fix_bankcsv", "agent", "fix a bank-export parser: Italian amounts, day-first dates, csv quoting, BOM/CRLF (hidden)",
         "The tests in test_bankcsv.py fail. Fix bankcsv.py (not the tests) until `python test_bankcsv.py` passes; its "
         "docstrings say what real exports look like, so make it handle those too.",
         files={"bankcsv.py": BANKCSV, "test_bankcsv.py": BANKCSV_TESTS}, judge=judge_bankcsv, system=CODER,
         tags=("workspace", "money"), reference=_write(**{"bankcsv.py": BANKCSV_FIXED})),
    Task("data_report", "agent", "a user's data question: compute from a CSV with a script, write a markdown report",
         "In spese.csv ci sono le mie spese del 2026. Fammi un file report.md con il totale per categoria, dalla più "
         "alta alla più bassa, e il mese in cui ho speso di più con il suo totale. Fai i conti con uno script Python, "
         "non a mano.", files={"spese.csv": SPESE}, judge=judge_report, system=CODER, tags=("workspace", "money", "it"),
         reference=_ref_report),
    Task("todo_cli_feature", "hard", "extend a CLI: new command, flag, error handling, old data files (hidden checks)",
         TODO_SPEC, files={"todo.py": TODO, "todo.json": TODO_JSON}, judge=judge_todo, system=CODER,
         tags=("workspace", "code"), reference=_write(**{"todo.py": TODO_FIXED})),
]
