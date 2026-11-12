"""Single-shot code tasks: one prompt, the last ```python block of the answer run against hidden checks."""

from evals.tasks.base import Task

# ---- 2026-10 tasks ------------------------------------------------------------------------------------------------
SLUGIFY = ("Write a Python function `slugify(text: str) -> str`: lowercase the text, strip accents (é → e), replace every "
           "run of characters that are not ASCII letters or digits with a single '-', and strip '-' from both ends. "
           "Return an empty string if nothing is left.")
SLUGIFY_T = '''
check("basic", lambda: slugify("Hello, World!") == "hello-world")
check("accents", lambda: slugify("  Crème  Brûlée  ") == "creme-brulee")
check("runs", lambda: slugify("a--b__c  d") == "a-b-c-d")
check("digits", lambda: slugify("Route 66!") == "route-66")
check("empty", lambda: slugify("") == "")
check("only symbols", lambda: slugify("---!!!") == "")
check("ends", lambda: slugify("-x-") == "x")
'''

DURATION = ("Write a Python function `parse_duration(s: str) -> int` returning seconds. Format: any of the units d, h, m, s "
            "(days, hours, minutes, seconds), each as a whole number followed by its letter, each at most once and in "
            "that order (d before h before m before s), no spaces inside: '1h30m' = 5400, '2d' = 172800, "
            "'1d2h3m4s' = 93784. A bare whole number ('90') is seconds. Leading and trailing whitespace is ignored. "
            "Raise ValueError for anything else: empty string, unknown unit, wrong order, repeated unit, a unit with no "
            "number, decimals, negative numbers, inner spaces.")
DURATION_T = '''
check("hm", lambda: parse_duration("1h30m") == 5400)
check("s", lambda: parse_duration("45s") == 45)
check("d", lambda: parse_duration("2d") == 172800)
check("all", lambda: parse_duration("1d2h3m4s") == 93784)
check("bare", lambda: parse_duration("90") == 90)
check("strip", lambda: parse_duration(" 90 ") == 90 and parse_duration("\\t5m\\n") == 300)
check("int", lambda: type(parse_duration("1m")) is int)
for bad in ["", "5x", "h", "1h2x", "m5", "1s1h", "1h1h", "1.5h", "-5s", "1h 30m", "  ", "1h30"]:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, parse_duration, bad))
'''

TTL = ("Write a Python class `TTLCache` with `__init__(self, capacity: int, ttl: float, clock=time.monotonic)` (import time). "
       "`put(key, value)` stores the value, which expires at clock()+ttl, and makes the key the most recently used; "
       "if the cache is then over capacity, first drop expired entries, and if it is still over, evict the least recently "
       "used key. `get(key, default=None)` returns the value if present and not expired (an entry is expired when "
       "clock() >= its expiry), marks the key most recently used without extending its expiry; an expired entry is "
       "removed and `default` returned. `__contains__` is true for a present, non-expired key and does not change "
       "recency. `__len__` is the number of non-expired entries.")
TTL_T = '''
def mk(cap=2, ttl=10):
    t = [0.0]
    return TTLCache(cap, ttl, clock=lambda: t[0]), t
def lru():
    c, t = mk(); c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)
    return c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
def expiry():
    c, t = mk(); c.put("a", 1); t[0] = 9; ok = c.get("a") == 1; t[0] = 10
    return ok and c.get("a") is None and c.get("a", "d") == "d"
def refresh_put():
    c, t = mk(); c.put("a", 1); t[0] = 8; c.put("a", 2); t[0] = 15; ok = c.get("a") == 2; t[0] = 18
    return ok and c.get("a") is None
def evict_expired_first():
    c, t = mk(); c.put("a", 1); t[0] = 5; c.put("b", 2); t[0] = 11; c.put("c", 3)
    return c.get("b") == 2 and c.get("c") == 3
def length():
    c, t = mk(3); c.put("a", 1); t[0] = 5; c.put("b", 2); t[0] = 11
    return len(c) == 1
def get_no_extend():
    c, t = mk(); c.put("a", 1); t[0] = 5; c.get("a"); t[0] = 10
    return c.get("a") is None
def contains_no_recency():
    c, t = mk(); c.put("a", 1); c.put("b", 2); in_a = "a" in c; c.put("c", 3)
    return in_a and "a" not in c and "b" in c and "c" in c
def cap1():
    c, t = mk(1); c.put("a", 1); c.put("b", 2)
    return c.get("a") is None and c.get("b") == 2 and len(c) == 1
def default_value():
    c, t = mk(); return c.get("zz", 42) == 42 and c.get("zz") is None
def overwrite_recency():
    c, t = mk(); c.put("a", 1); c.put("b", 2); c.put("a", 3); c.put("c", 4)
    return c.get("b") is None and c.get("a") == 3
for f in (lru, expiry, refresh_put, evict_expired_first, length, get_no_extend, contains_no_recency, cap1,
          default_value, overwrite_recency):
    check(f.__name__, f)
'''

TOPO = ("Write a Python function `schedule(tasks: dict[str, list[str]]) -> list[str]`. `tasks` maps a task name to the names "
        "it depends on, which must come before it. Return an order that respects every dependency and, among all valid "
        "orders, is the lexicographically smallest (at every step take the alphabetically first task that is ready). "
        "Raise ValueError if there is a cycle (including a task depending on itself), KeyError if a dependency is not a "
        "key of `tasks`. A dependency listed twice counts once. Do not modify the argument.")
TOPO_T = '''
import copy
check("chain", lambda: schedule({"a": [], "b": ["a"], "c": ["a"]}) == ["a", "b", "c"])
check("reverse", lambda: schedule({"b": [], "a": ["b"]}) == ["b", "a"])
check("independent", lambda: schedule({"c": [], "b": [], "a": []}) == ["a", "b", "c"])
check("lexicographic", lambda: schedule({"d": [], "c": ["d"], "a": [], "b": ["a"]}) == ["a", "b", "d", "c"])
check("diamond", lambda: schedule({"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}) == ["a", "b", "c", "d"])
check("empty", lambda: schedule({}) == [])
check("dup dep", lambda: schedule({"a": [], "b": ["a", "a"]}) == ["a", "b"])
check("cycle", lambda: raises(ValueError, schedule, {"a": ["b"], "b": ["a"]}))
check("self cycle", lambda: raises(ValueError, schedule, {"a": ["a"]}))
check("partial cycle", lambda: raises(ValueError, schedule, {"x": [], "a": ["b"], "b": ["c"], "c": ["a"]}))
check("unknown", lambda: raises(KeyError, schedule, {"a": ["zzz"]}))
def pure():
    t = {"b": ["a"], "a": []}; before = copy.deepcopy(t); schedule(t); return t == before
check("pure", pure)
def big():
    t = {f"n{i:03d}": [f"n{j:03d}" for j in range(max(0, i - 3), i)] for i in range(300)}
    return schedule(t) == sorted(t)
check("big", big)
'''

EXPR = ("Write a Python function `evaluate(expr: str, variables: dict | None = None)` that evaluates an arithmetic "
        "expression. Do NOT use eval, exec, compile or the ast module; write a tokenizer and parser. Supported: numbers "
        "(integers stay int, '2.5' is float), variables from the dict, operators + - * / ** with the usual precedence, "
        "** right-associative and binding tighter than a unary minus on its left (-2**2 is -4) but allowing a unary minus "
        "in its exponent (2**-1 is 0.5), unary + and -, parentheses, and the functions min(...) and max(...) (one or more "
        "arguments) and abs(x) (exactly one), whose arguments are full expressions. '/' is true division. Whitespace is "
        "ignored. Errors: division by zero raises ZeroDivisionError, an unknown variable or function raises NameError, "
        "any malformed input (empty, dangling operator, unbalanced parentheses, two numbers in a row, '1.2.3', wrong "
        "argument count, empty call) raises SyntaxError.")
EXPR_T = '''
E = evaluate
check("precedence", lambda: E("1+2*3") == 7)
check("parens", lambda: E("(1+2)*3") == 9)
check("pow right assoc", lambda: E("2**3**2") == 512)
check("neg pow", lambda: E("-2**2") == -4)
check("pow neg exp", lambda: E("2**-1") == 0.5)
check("unary", lambda: E("2*-3") == -6 and E("--3") == 3 and E("+4") == 4)
check("div", lambda: E("10/4") == 2.5)
check("float", lambda: E("1.5*2") == 3.0 and E(".5+.5") == 1.0)
check("int type", lambda: type(E("2+3")) is int)
check("vars", lambda: E("x*y+1", {"x": 3, "y": 4}) == 13)
check("max", lambda: E("max(1, 2+3, 4)") == 5)
check("min abs", lambda: E("min(2,abs(-7))") == 2)
check("nested", lambda: E("abs(-(2+1))*max(1,2)") == 6)
check("spaces", lambda: E("  1 +   2 ") == 3)
check("div zero", lambda: raises(ZeroDivisionError, E, "1/0") and raises(ZeroDivisionError, E, "1/(2-2)"))
check("name", lambda: raises(NameError, E, "z+1") and raises(NameError, E, "foo(1)"))
for bad in ["", "1+", "(1", "1)", "1 2", "()", "1.2.3", "max()", "abs(1,2)", "*3", "2**", "1+*2", "min(1,)"]:
    check("syntax " + repr(bad), lambda bad=bad: raises(SyntaxError, E, bad))
'''
EXPR_FORBIDDEN = r"\b(eval|exec|compile)\s*\(|\bimport ast\b|\bfrom ast\b|__import__"

JSON = ("Write a Python function `parse(s: str)` that parses JSON text exactly as json.loads would, without importing "
        "json (or any parser library): objects → dict, arrays → list, strings → str, numbers without fraction or exponent "
        "→ int, others → float, true/false/null → True/False/None. Support all string escapes including \\uXXXX and "
        "surrogate pairs (a high and a low surrogate escape together make one character). Whitespace is space, tab, "
        "newline, carriage return. A repeated object key keeps the last value. Raise ValueError for invalid JSON: "
        "trailing commas, single quotes, leading zeros like 01, NaN/Infinity, raw control characters inside strings, "
        "bad escapes, text after the value, empty input, unterminated anything, a number like '1.' or '.5' or '1e'.")
JSON_T = '''
import json
def same(s):
    return repr(parse(s)) == repr(json.loads(s))
GOOD = ['0', '-0', '12', '-7', '1.5', '-1.5E-3', '1e2', '1E+2', '0.0', '123456789012345678901234567890', 'true', 'false',
        'null', '""', '"abc"', '"\\\\u00e9\\\\n\\\\t\\\\/\\\\\\\\\\\\""', '"\\\\ud83d\\\\ude00"', '"a\\\\u0041b"', '[]', '{}', ' [1 , 2 ] ',
        '[[],[[]],{}]', '{"a":1,"a":2}', '{"a":{"b":[1,2,{"c":null}]},"d":"x"}', '\\n\\t{ "k" : [ true , false , null ] }\\r\\n',
        '[1.0, 10, 1e0, -0.0, 0.1e1]', '"héllo wörld ✓"', '{"": ""}']
for g in GOOD:
    check("good " + g[:30], lambda g=g: same(g))
BAD = ['', ' ', '[1,]', '{"a":1,}', "{'a':1}", '01', '-01', 'NaN', 'Infinity', '[1] x', '"a\\nb"', '"\\\\x"', '[1 2]',
       '{"a" 1}', 'tru', '"abc', '-', '1.', '.5', '1e', '[', '{"a":}', '{"a":1', '[1,,2]', '{1:2}',
       '+1', '0x10', '[1]]', 'nul']
for b in BAD:
    check("bad " + repr(b)[:30], lambda b=b: raises(ValueError, parse, b))
check("deep", lambda: same("[" * 200 + "]" * 200))
'''
JSON_FORBIDDEN = r"\bimport json\b|\bfrom json\b|__import__|\bimport (simplejson|ujson|orjson|ast)\b|\beval\s*\("


CRON = ("Write a Python function `next_run(expr: str, after: datetime) -> datetime` (import datetime) for a 5-field cron "
        "expression: minute (0-59), hour (0-23), day-of-month (1-31), month (1-12), day-of-week (0-7, both 0 and 7 are "
        "Sunday, 1 is Monday). A field is `*`, a number, a range `a-b`, a step `*/n` or `a-b/n` (n >= 1), or a comma "
        "list of numbers, ranges and steps. Return the earliest datetime strictly after `after` (naive datetimes; "
        "seconds and microseconds zero) whose fields all match. If both day-of-month and day-of-week are restricted "
        "(neither is a plain `*`), a day matches when EITHER of them matches; if one of them is a plain `*`, only the "
        "other restricts. Day 31 never matches a 30-day month and Feb 29 only exists in leap years (Gregorian rules: "
        "2100 is not a leap year). It must return within about a second even when the next match is years away. Raise "
        "ValueError for a wrong field count, a value out of range, a step of 0, a reversed range, or any other malformed "
        "field.")
CRON_T = r"""
from datetime import datetime as D
import time
N = next_run
check("every 15", lambda: N("*/15 * * * *", D(2026, 11, 4, 9, 7, 30)) == D(2026, 11, 4, 9, 15))
check("strictly after", lambda: N("*/15 * * * *", D(2026, 11, 4, 9, 15)) == D(2026, 11, 4, 9, 30))
check("next day", lambda: N("0 9 * * *", D(2026, 11, 4, 9, 0)) == D(2026, 11, 5, 9, 0))
check("list+range", lambda: N("5,35 8-10 * * *", D(2026, 11, 4, 9, 40)) == D(2026, 11, 4, 10, 5))
check("range step", lambda: N("10-50/20 * * * *", D(2026, 11, 4, 9, 31)) == D(2026, 11, 4, 9, 50)
      and N("10-50/20 * * * *", D(2026, 11, 4, 9, 50)) == D(2026, 11, 4, 10, 10))
check("weekdays", lambda: N("0 9 * * 1-5", D(2026, 11, 6, 9, 0)) == D(2026, 11, 9, 9, 0))
check("sunday 7", lambda: N("0 12 * * 7", D(2026, 11, 4)) == D(2026, 11, 8, 12, 0)
      and N("0 12 * * 0", D(2026, 11, 4)) == D(2026, 11, 8, 12, 0))
check("new year", lambda: N("0 0 1 1 *", D(2026, 11, 4)) == D(2027, 1, 1))
check("year end", lambda: N("59 23 31 12 *", D(2026, 12, 31, 23, 59)) == D(2027, 12, 31, 23, 59))
check("day 31 skips november", lambda: N("0 0 31 * *", D(2026, 11, 4)) == D(2026, 12, 31))
check("dom or dow", lambda: N("0 0 13 * 5", D(2026, 11, 4)) == D(2026, 11, 6)
      and N("0 0 13 * 5", D(2026, 11, 6)) == D(2026, 11, 13) and N("0 0 13 * 5", D(2026, 11, 13)) == D(2026, 11, 20))
check("dom only when dow star", lambda: N("0 0 15 * *", D(2026, 11, 4)) == D(2026, 11, 15))
check("dow only when dom star", lambda: N("30 6 * * 2", D(2026, 11, 4)) == D(2026, 11, 10, 6, 30))
check("leap day", lambda: N("30 2 29 2 *", D(2026, 11, 4)) == D(2028, 2, 29, 2, 30))
def far():
    t = time.time(); r = N("0 0 29 2 *", D(2099, 3, 1))
    return r == D(2104, 2, 29) and time.time() - t < 3
check("century not leap, fast", far)
check("seconds dropped", lambda: N("* * * * *", D(2026, 11, 4, 9, 7, 59, 999)) == D(2026, 11, 4, 9, 8))
for bad in ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "* * 32 * *", "* * * 13 *", "* * * 0 *",
            "* * * * 8", "*/0 * * * *", "5-1 * * * *", "a * * * *", "1,,2 * * * *", "*/ * * * *", "-5 * * * *"]:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, N, bad, D(2026, 1, 1)))
"""

REGEX = ("Write a Python function `fullmatch(pattern: str, text: str) -> bool` that says whether the WHOLE text matches a "
         "regular expression, without using the re module (or any regex library). Syntax: literal characters; `.` (any "
         "single character); `*`, `+`, `?` after an atom (an atom is a literal, `.`, a group or a class); `|` "
         "alternation; `( )` groups, which may be empty on one side of `|` like `(a|)`; character classes `[abc]`, "
         "ranges `[a-z]`, negation `[^ab]`; a backslash makes the next character literal (`\\.`, `\\*`). There are no "
         "anchors, no `{n}`, no lazy or possessive forms. The empty pattern matches only the empty text. The pattern "
         "must be validated before matching and ValueError raised for: unbalanced parentheses or brackets, a quantifier "
         "with nothing to repeat (`*a`, `(+)`, `|*`), stacked quantifiers (`a**`, `a+*`, `a?+`), an empty class `[]`, a "
         "reversed range `[b-a]`, a trailing backslash. It must not blow up on nasty patterns or long texts: "
         "`(a+)+b` against 30 letters a has to answer quickly, and a 5000-character text must not hit the recursion limit.")
REGEX_T = r"""
import re
PATTERNS = ['abc', 'a.c', 'a*', 'a+', 'a?', 'ab*c', '(ab)+', '(a|b)*c', 'a|b|c', '(a|)b', '[abc]+', '[a-c]*d', '[^ab]+',
            'x[0-9]+y', '\\.', '\\*a', '(ab|a)(bc|c)', '((a))', '(a*)*', '.*', '', 'a.*b.*c', '(a|ab)(c|bcd)(d*)', '[a.]+',
            '(a+|b)*', 'a?a?a?aaa', '(ab|cd)*e?', 'a(b|c)*d', '[a-cx-z]+', '(|a)+', 'a|', '(a|b)(c|d)(e|f)?']
TEXTS = ['', 'a', 'b', 'c', 'd', 'ab', 'abc', 'abbc', 'aac', 'abab', 'ababc', 'abcd', 'xx', 'x12y', 'x1y', '.', '*a', 'a.c',
         'a-', 'cd', 'abcbd', 'bcd', 'aaaa', 'aaa', 'aaaaaa', 'ba', 'bb', 'abcabc', 'a.', 'a*', 'abcde', 'cdabe', 'acd', 'xyz', 'ace', 'bdf']
for pat in PATTERNS:
    check("match " + repr(pat), lambda pat=pat: all(bool(fullmatch(pat, t)) == (re.fullmatch(pat, t) is not None) for t in TEXTS))
for bad in ['(', ')', '(a', 'a)', '*a', '+', '?a', 'a**', 'a+*', 'a?+', '[a', '[]', '[b-a]', 'a\\', '|*', '(*a)', '(+)', 'a||*']:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, fullmatch, bad, "a"))
import time
def nasty():
    t = time.time(); r = fullmatch('(a+)+b', 'a' * 30) is False and fullmatch('(a*)*b', 'a' * 40) is False
    return r and time.time() - t < 3
check("catastrophic backtracking", nasty)
check("long text", lambda: fullmatch('(a|b)*', 'ab' * 2500) is True and fullmatch('.*c', 'x' * 5000 + 'c') is True)
"""


OLD = [
    Task("slugify", "easy", "slugify with accent stripping", SLUGIFY, SLUGIFY_T),
    Task("parse_duration", "easy", "strict duration parser, 12 invalid inputs", DURATION, DURATION_T),
    Task("ttl_cache", "medium", "LRU + TTL cache with an injected clock", TTL, TTL_T),
    Task("topo_schedule", "medium", "lexicographically smallest topological order, error cases", TOPO, TOPO_T),
    Task("expr_eval", "hard", "expression parser, no eval, precedence + errors", EXPR, EXPR_T, EXPR_FORBIDDEN),
    Task("json_parse", "hard", "JSON parser from scratch checked against json.loads", JSON, JSON_T, JSON_FORBIDDEN),
    Task("cron_next", "hard", "cron next-run calculator: dom/dow rule, leap years, century, 16 invalid inputs", CRON, CRON_T),
    Task("regex_engine", "hard", "regex matcher without re, checked against re.fullmatch; no catastrophic backtracking", REGEX, REGEX_T,
         r"\bimport re\b|\bfrom re\b|__import__|\bimport (regex|sre_\w+)\b"),
]


# ---- 2026-11 tasks: the small functions the bot's own code is made of (amounts, IBANs, slots, dates, recurrence) ------
AMOUNT = ("Write a Python function `parse_amount(s: str) -> int` returning euro cents from an amount written the Italian "
          "way. Grammar, with optional spaces between the parts and around the whole: an optional sign (+ or -), an "
          "optional currency mark ('€' or 'EUR') either before or after the number but not both, and the number: an "
          "integer part either plain digits ('1234') or groups with '.' as thousands separator ('1.234', '1.000.000': "
          "1-3 digits, then groups of exactly three), and optionally ',' followed by one or two decimals. Examples: "
          "'1.234,56' → 123456, '-12,5' → -1250, '€ 3' → 300, '3 €' → 300, '+0,99 EUR' → 99, '-€5' → -500. Raise "
          "ValueError for anything else, e.g. '', '1,234.56', '12,345', '1.23', '€', '--5', '5-', '1 234', ',5', '5,'.")
AMOUNT_T = '''
check("thousands", lambda: parse_amount("1.234,56") == 123456)
check("one decimal", lambda: parse_amount("-12,5") == -1250)
check("euro before", lambda: parse_amount("€ 3") == 300)
check("euro after", lambda: parse_amount("3 €") == 300)
check("EUR and plus", lambda: parse_amount("+0,99 EUR") == 99)
check("plain digits", lambda: parse_amount("1234,00") == 123400)
check("spaces", lambda: parse_amount("  7 ") == 700)
check("millions", lambda: parse_amount("1.000.000") == 100000000)
check("sign before euro", lambda: parse_amount("-€5") == -500)
check("int", lambda: type(parse_amount("1,5")) is int and parse_amount("1,5") == 150)
for bad in ["", "1,234.56", "12,345", "1.23", "€", "--5", "5-", "1 234", "abc", "1.2345,00", "€5€", ",5", "5,", "1..000",
            "EUR 5 €", "12.34.567"]:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, parse_amount, bad))
'''
AMOUNT_REF = r'''import re
def parse_amount(s):
    m = re.fullmatch(r"\s*([+-])?\s*(€|EUR)?\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?\s*(€|EUR)?\s*", s)
    if not m or (m.group(2) and m.group(5)):
        raise ValueError(s)
    v = int(m.group(3).replace(".", "")) * 100 + int((m.group(4) or "0").ljust(2, "0"))
    return -v if m.group(1) == "-" else v
'''

IBAN = ("Write two Python functions. `iban_valid(s: str) -> bool`: spaces are ignored and letters may be lowercase. "
        "Valid when it is two letters (the country), two digits, then letters and digits only; total length 15 to 34; "
        "an Italian ('IT') IBAN is exactly 27 characters; and the ISO 13616 check holds: move the first four "
        "characters to the end, replace each letter with two digits (A=10 ... Z=35), and the resulting number mod 97 "
        "is 1. `format_iban(s: str) -> str`: the IBAN uppercase, in groups of four separated by single spaces (the "
        "last group may be shorter); raise ValueError if it is not valid.")
IBAN_T = '''
check("valid IT", lambda: iban_valid("IT60X054281110100000012345" "6"))
check("spaces and lowercase", lambda: iban_valid("  it60 x054 2811 1010 0000 0123 456 "))
check("valid GB", lambda: iban_valid("GB82 WEST 1234 5698 7654 32"))
check("valid DE", lambda: iban_valid("DE89370400440532013000"))
check("bad checksum", lambda: not iban_valid("IT60X054281110100000012345" "7"))
check("swapped digits", lambda: not iban_valid("GB82 WEST 1234 5698 7654 23"))
check("IT wrong length", lambda: not iban_valid("IT60X054281110100000012345"))
check("bad characters", lambda: not iban_valid("IT60X05428111010000001234-6"))
check("no country", lambda: not iban_valid("1260X0542811101000000123456"))
check("too short", lambda: not iban_valid("GB82WEST12"))
check("empty", lambda: not iban_valid(""))
check("format", lambda: format_iban("it60x0542811101000000123456") == "IT60 X054 2811 1010 0000 0123 456")
check("format GB", lambda: format_iban("GB82WEST12345698765432") == "GB82 WEST 1234 5698 7654 32")
check("format invalid", lambda: raises(ValueError, format_iban, "IT60X054281110100000012345" "7"))
'''
IBAN_REF = '''def _clean(s):
    return s.replace(" ", "").upper()
def iban_valid(s):
    s = _clean(s)
    if not (15 <= len(s) <= 34) or not (s[:2].isascii() and s[:2].isalpha() and s[2:4].isdigit()):
        return False
    if not (s.isascii() and s.isalnum()) or (s[:2] == "IT" and len(s) != 27):
        return False
    n = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(n) % 97 == 1
def format_iban(s):
    if not iban_valid(s):
        raise ValueError(s)
    s = _clean(s)
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))
'''

SPLIT = ("Write a Python function `split(total_cents: int, weights: list[int]) -> list[int]` that splits a bill among "
         "people in proportion to their weights (e.g. how many nights each stayed). The shares are whole cents and add "
         "up exactly to the total: each person first gets the floor of total*weight/sum(weights), then the cents left "
         "over go one each to the people with the largest remainders, ties to the earlier person. Raise ValueError for "
         "a negative total, an empty list, a negative weight, or weights that are all zero.")
SPLIT_T = '''
check("thirds", lambda: split(1000, [1, 1, 1]) == [334, 333, 333])
check("proportional", lambda: split(100, [1, 2]) == [33, 67])
check("ties to the first", lambda: split(7, [2, 2, 2, 2]) == [2, 2, 2, 1])
check("zero weight", lambda: split(5, [0, 3]) == [0, 5])
check("zero total", lambda: split(0, [1, 2]) == [0, 0])
check("one person", lambda: split(1234, [5]) == [1234])
check("sum exact", lambda: sum(split(99999, [3, 7, 11, 13])) == 99999)
check("largest remainder", lambda: split(10, [1, 2, 3]) == [2, 3, 5] and split(10, [1, 1, 4]) == [2, 2, 6])
for args in [(-1, [1]), (10, []), (10, [1, -1]), (10, [0, 0])]:
    check("bad " + repr(args), lambda args=args: raises(ValueError, split, *args))
'''
SPLIT_REF = '''def split(total_cents, weights):
    if total_cents < 0 or not weights or any(w < 0 for w in weights) or sum(weights) == 0:
        raise ValueError("bad input")
    W = sum(weights)
    base = [total_cents * w // W for w in weights]
    order = sorted(range(len(weights)), key=lambda i: (-(total_cents * weights[i] % W), i))
    for i in order[:total_cents - sum(base)]:
        base[i] += 1
    return base
'''

SLOTS = ("Write a Python function `free_slots(busy, day_start: str, day_end: str, min_minutes: int = 15) -> "
         "list[tuple[str, str]]`. `busy` is a list of (start, end) pairs of 'HH:MM' strings (24 h, two digits each), in "
         "any order, possibly overlapping or touching, possibly reaching outside the window. Return the free gaps inside "
         "[day_start, day_end] as ('HH:MM', 'HH:MM') pairs in order, keeping only gaps of at least min_minutes (and never "
         "empty ones). Raise ValueError for a time not in 'HH:MM' form or out of range (00:00-23:59), a busy pair whose "
         "start is not before its end, or day_start not before day_end.")
SLOTS_T = '''
F = free_slots
check("one meeting", lambda: F([("10:00", "11:00")], "09:00", "12:00") == [("09:00", "10:00"), ("11:00", "12:00")])
check("unsorted overlapping", lambda: F([("13:00", "14:00"), ("10:00", "11:30"), ("11:00", "12:00")], "09:00", "18:00")
      == [("09:00", "10:00"), ("12:00", "13:00"), ("14:00", "18:00")])
check("too short dropped", lambda: F([("09:10", "10:00")], "09:00", "11:00") == [("10:00", "11:00")])
check("exactly min kept", lambda: F([("09:15", "10:00")], "09:00", "10:00") == [("09:00", "09:15")])
check("custom min", lambda: F([("10:00", "11:00")], "09:00", "12:00", 61) == [])
check("outside window", lambda: F([("07:00", "09:30"), ("17:00", "22:00")], "09:00", "18:00") == [("09:30", "17:00")])
check("fully busy", lambda: F([("08:00", "19:00")], "09:00", "18:00") == [])
check("touching", lambda: F([("10:00", "11:00"), ("11:00", "12:00")], "10:00", "13:00") == [("12:00", "13:00")])
check("nothing busy", lambda: F([], "08:30", "21:00") == [("08:30", "21:00")])
check("after the window", lambda: F([("19:00", "20:00")], "09:00", "18:00") == [("09:00", "18:00")])
check("zero min no empty", lambda: F([("10:00", "11:00"), ("11:00", "12:00")], "10:00", "12:00", 0) == [])
check("contained", lambda: F([("09:00", "17:00"), ("10:00", "11:00")], "09:00", "18:00") == [("17:00", "18:00")])
for bad in [([("11:00", "10:00")], "09:00", "18:00"), ([("9:00", "10:00")], "09:00", "18:00"),
            ([("10:60", "11:00")], "09:00", "18:00"), ([], "18:00", "09:00"), ([("10:00", "10:00")], "09:00", "18:00"),
            ([], "24:00", "25:00")]:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, F, *bad))
'''
SLOTS_REF = r'''import re
def _m(t):
    if not isinstance(t, str) or not re.fullmatch(r"\d\d:\d\d", t):
        raise ValueError(t)
    h, m = int(t[:2]), int(t[3:])
    if h > 23 or m > 59:
        raise ValueError(t)
    return h * 60 + m
def _t(m):
    return f"{m // 60:02d}:{m % 60:02d}"
def free_slots(busy, day_start, day_end, min_minutes=15):
    lo, hi = _m(day_start), _m(day_end)
    if lo >= hi:
        raise ValueError("window")
    spans = []
    for a, b in busy:
        a, b = _m(a), _m(b)
        if a >= b:
            raise ValueError("busy")
        spans.append((a, b))
    out, cur = [], lo
    for a, b in sorted(spans):
        if a > cur:
            out.append((cur, min(a, hi)))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return [(_t(a), _t(b)) for a, b in out if b > a and b - a >= min_minutes]
'''

RECUR = ("Write a Python function `recurring(tx: list[tuple[str, str, float]]) -> list[tuple[str, float, str]]` that "
         "finds monthly charges in bank movements. Each movement is (date 'YYYY-MM-DD', payee, amount); negative amounts "
         "are charges, the others are ignored. Payees are the same when equal after stripping spaces at both ends and "
         "ignoring case. A payee is recurring when it has at least 3 charges, every gap between its consecutive charges "
         "(by date) is 26 to 35 days inclusive, and every charge is within 10% of the median charge (|a - median| <= "
         "0.10 * |median|). For each, return (payee as written in its latest charge, stripped; the median charge as a "
         "positive number rounded to 2 decimals; the next expected date, 'YYYY-MM-DD' = latest charge date + the mean "
         "gap rounded to the nearest whole day), sorted by next date, then payee. The input may be in any order.")
RECUR_T = '''
tx = [("2026-07-15", "Netflix", -12.99), ("2026-08-15", "Netflix", -12.99), ("2026-09-15", "Netflix", -12.99),
      ("2026-10-15", "Netflix", -12.99), ("2026-09-20", "Netflix", 12.99),
      ("2026-08-01", "FitLife", -45.0), ("2026-10-02", "FitLife", -49.0), ("2026-09-01", "FitLife", -45.0),
      ("2026-08-03", "Spotify", -9.99), ("2026-09-03", "Spotify", -9.99), ("2026-10-03", "Spotify", -10.99),
      ("2026-09-28", "Enel", -64.3), ("2026-10-28", "Enel", -71.8),
      ("2026-07-07", "ILIAD", -9.99), ("2026-08-07", " Iliad", -9.99), ("2026-09-07", "Iliad", -9.99), ("2026-10-07", "iliad ", -9.99),
      ("2026-08-01", "GTT", -39.0), ("2026-09-01", "GTT", -39.0), ("2026-11-01", "GTT", -39.0)]
tx += [(f"2026-10-{d:02d}", "Esselunga", -50.0) for d in (1, 8, 15, 22, 29)]
R = recurring(tx)
check("exact result", lambda: R == [("FitLife", 45.0, "2026-11-02"), ("iliad", 9.99, "2026-11-07"), ("Netflix", 12.99, "2026-11-15")])
check("refund ignored", lambda: any(r[0] == "Netflix" for r in R))
check("price jump excluded", lambda: not any(r[0].lower() == "spotify" for r in R))
check("two charges excluded", lambda: not any(r[0] == "Enel" for r in R))
check("gap too long excluded", lambda: not any(r[0] == "GTT" for r in R))
check("weekly excluded", lambda: not any(r[0] == "Esselunga" for r in R))
check("empty", lambda: recurring([]) == [])
check("unsorted same", lambda: recurring(list(reversed(tx))) == R)
check("even median", lambda: recurring([("2026-01-01", "A", -10.0), ("2026-02-01", "A", -10.5), ("2026-03-01", "A", -10.5),
                                        ("2026-04-01", "A", -10.0)]) == [("A", 10.25, "2026-05-01")])
check("same day twice", lambda: recurring([("2026-01-01", "B", -5.0), ("2026-01-01", "B", -5.0), ("2026-02-01", "B", -5.0)]) == [])
'''
RECUR_REF = '''import statistics
from datetime import date, timedelta
def recurring(tx):
    groups = {}
    for d, p, a in tx:
        if a < 0:
            groups.setdefault(p.strip().lower(), []).append((date.fromisoformat(d), p.strip(), a))
    out = []
    for rows in groups.values():
        rows.sort(key=lambda r: r[0])
        if len(rows) < 3:
            continue
        gaps = [(b[0] - a[0]).days for a, b in zip(rows, rows[1:])]
        if not all(26 <= g <= 35 for g in gaps):
            continue
        med = statistics.median(r[2] for r in rows)
        if any(abs(r[2] - med) > 0.10 * abs(med) + 1e-9 for r in rows):
            continue
        nxt = rows[-1][0] + timedelta(days=round(sum(gaps) / len(gaps)))
        out.append((rows[-1][1], round(abs(med), 2), nxt.isoformat()))
    return sorted(out, key=lambda r: (r[2], r[0]))
'''

WHEN = ("Write a Python function `parse_when(text: str, today: date) -> date` (from datetime import date) for the dates "
        "people type in Italian. Case-insensitive; accents optional (lunedì = lunedi); extra spaces ignored. Forms:\n"
        "- 'oggi', 'domani', 'dopodomani';\n"
        "- 'tra N giorni' / 'fra N giorni' ('tra 1 giorno' too), 'tra N settimane' ('tra una settimana' = 7 days); N is "
        "digits;\n"
        "- a weekday name (lunedì martedì mercoledì giovedì venerdì sabato domenica): the next such day strictly after "
        "today;\n"
        "- a weekday name followed by 'prossimo' or 'prossima': that weekday in the next calendar week (weeks run Monday "
        "to Sunday), e.g. with today Tuesday 2026-11-10, 'giovedì prossimo' is 2026-11-19 and 'lunedì prossimo' "
        "2026-11-16;\n"
        "- a day and an Italian month name, optionally after 'il' ('il 5 dicembre', '5 dicembre'), optionally followed "
        "by a 4-digit year: without a year, the first such date on or after today;\n"
        "- 'D/M' or 'DD/MM' (day first), optionally '/YYYY': without a year, the first such date on or after today.\n"
        "Raise ValueError for anything else, including dates that do not exist ('31 novembre', '30/2').")
WHEN_T = '''
from datetime import date
T = date(2026, 11, 10)  # a Tuesday
P = lambda s, t=T: parse_when(s, t)
check("oggi", lambda: P("oggi") == T)
check("domani", lambda: P(" Domani ") == date(2026, 11, 11))
check("dopodomani", lambda: P("dopodomani") == date(2026, 11, 12))
check("tra giorni", lambda: P("tra 3 giorni") == date(2026, 11, 13) and P("fra 1 giorno") == date(2026, 11, 11))
check("tra settimane", lambda: P("tra 2 settimane") == date(2026, 11, 24) and P("tra una settimana") == date(2026, 11, 17))
check("weekday", lambda: P("giovedì") == date(2026, 11, 12) and P("giovedi") == date(2026, 11, 12))
check("same weekday is next week", lambda: P("martedì") == date(2026, 11, 17))
check("monday", lambda: P("lunedì") == date(2026, 11, 16))
check("prossimo", lambda: P("giovedì prossimo") == date(2026, 11, 19) and P("lunedì prossimo") == date(2026, 11, 16))
check("domenica prossima", lambda: P("domenica prossima") == date(2026, 11, 22) and P("domenica") == date(2026, 11, 15))
check("il 5 dicembre", lambda: P("il 5 dicembre") == date(2026, 12, 5) and P("5 Dicembre") == date(2026, 12, 5))
check("month passed: next year", lambda: P("3 marzo") == date(2027, 3, 3))
check("today's date", lambda: P("10 novembre") == T and P("10/11") == T)
check("with year", lambda: P("1 gennaio 2028") == date(2028, 1, 1) and P("5/12/2027") == date(2027, 12, 5))
check("slash", lambda: P("5/12") == date(2026, 12, 5) and P("01/02") == date(2027, 2, 1))
check("year end", lambda: P("domani", date(2026, 12, 31)) == date(2027, 1, 1) and P("3/1", date(2026, 12, 30)) == date(2027, 1, 3))
check("29 feb next leap", lambda: P("29/2", date(2027, 3, 1)) == date(2028, 2, 29))
for bad in ["", "ieri", "31 novembre", "30/2", "tra giorni", "lunedì scorso", "5 dicembr", "32/1", "tra -1 giorni", "13/13"]:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, P, bad))
'''
WHEN_REF = r'''import re, unicodedata
from datetime import date, timedelta
DAYS = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
MONTHS = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre",
          "novembre", "dicembre"]
def _norm(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))
    return " ".join(s.split())
def _next_on_or_after(d, m, today):
    for y in range(today.year, today.year + 9):
        try:
            c = date(y, m, d)
        except ValueError:
            if not (m == 2 and d == 29):
                raise
            continue
        if c >= today:
            return c
    raise ValueError("no such date")
def _ymd(y, m, d):
    return date(y, m, d)
def parse_when(text, today):
    s = _norm(text)
    fixed = {"oggi": 0, "domani": 1, "dopodomani": 2}
    if s in fixed:
        return today + timedelta(days=fixed[s])
    m = re.fullmatch(r"(?:tra|fra) (\d+|una|un) (giorn[oi]|settiman[ae])", s)
    if m:
        n = 1 if m.group(1) in ("una", "un") else int(m.group(1))
        return today + timedelta(days=n * (7 if m.group(2).startswith("settiman") else 1))
    m = re.fullmatch(r"(\w+)(?: (prossim[oa]))?", s)
    if m and m.group(1) in DAYS:
        wd = DAYS.index(m.group(1))
        if m.group(2):
            monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
            return monday + timedelta(days=wd)
        return today + timedelta(days=(wd - today.weekday() - 1) % 7 + 1)
    m = re.fullmatch(r"(?:il )?(\d{1,2}) (\w+)(?: (\d{4}))?", s)
    if m and m.group(2) in MONTHS:
        d, mo = int(m.group(1)), MONTHS.index(m.group(2)) + 1
        return _ymd(int(m.group(3)), mo, d) if m.group(3) else _next_on_or_after(d, mo, today)
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return _ymd(int(m.group(3)), mo, d) if m.group(3) else _next_on_or_after(d, mo, today)
    raise ValueError(text)
'''

RRULE = ("Write a Python function `expand(start: datetime, freq: str, *, interval: int = 1, count: int | None = None, "
         "until: datetime | None = None, byday: list[str] | None = None, exdates=()) -> list[datetime]` that lists the "
         "occurrences of a repeating event, like an iCalendar RRULE (from datetime import datetime, timedelta). Rules:\n"
         "- freq is 'daily', 'weekly', 'monthly' or 'yearly'; every `interval` days / weeks / months / years from start.\n"
         "- Exactly one of count and until must be given. `until` is inclusive. `count` is the number of occurrences "
         "generated before exdates are removed (as in iCalendar: an excluded date still uses up the count).\n"
         "- Every occurrence has start's time of day. start itself is an occurrence only if it matches the rule.\n"
         "- weekly: the days are those in `byday` (codes 'MO','TU','WE','TH','FR','SA','SU'), or start's weekday if "
         "byday is None; weeks run Monday to Sunday, every `interval` weeks counting from start's week; nothing before "
         "start.\n"
         "- monthly: start's day of the month; months without that day are skipped (no clamping). yearly: start's month "
         "and day; Feb 29 only in leap years.\n"
         "- exdates: datetimes to leave out.\n"
         "Raise ValueError for an unknown freq, interval < 1, neither or both of count/until, count < 0, byday with a "
         "freq other than weekly, or an unknown day code. Return the occurrences in order.")
RRULE_T = '''
from datetime import datetime as D
X = expand
check("daily count", lambda: X(D(2026, 1, 30, 9), "daily", count=3) == [D(2026, 1, 30, 9), D(2026, 1, 31, 9), D(2026, 2, 1, 9)])
check("interval until inclusive", lambda: X(D(2026, 1, 1, 9), "daily", interval=2, until=D(2026, 1, 7, 9))
      == [D(2026, 1, d, 9) for d in (1, 3, 5, 7)])
check("until earlier time", lambda: X(D(2026, 1, 1, 9), "daily", interval=2, until=D(2026, 1, 7, 8))
      == [D(2026, 1, d, 9) for d in (1, 3, 5)])
check("weekly byday", lambda: X(D(2026, 11, 4, 10), "weekly", count=5, byday=["MO", "WE", "FR"])
      == [D(2026, 11, d, 10) for d in (4, 6, 9, 11, 13)])
check("biweekly from mid-week", lambda: X(D(2026, 11, 4, 10), "weekly", interval=2, count=4, byday=["TU", "TH"])
      == [D(2026, 11, 5, 10), D(2026, 11, 17, 10), D(2026, 11, 19, 10), D(2026, 12, 1, 10)])
check("start not matching", lambda: X(D(2026, 11, 4, 8), "weekly", count=2, byday=["MO"]) == [D(2026, 11, 9, 8), D(2026, 11, 16, 8)])
check("weekly plain until", lambda: X(D(2026, 11, 12, 18), "weekly", until=D(2026, 12, 1)) == [D(2026, 11, d, 18) for d in (12, 19, 26)])
check("monthly 31st skips", lambda: X(D(2026, 1, 31, 12), "monthly", count=4)
      == [D(2026, 1, 31, 12), D(2026, 3, 31, 12), D(2026, 5, 31, 12), D(2026, 7, 31, 12)])
check("quarterly over year end", lambda: X(D(2026, 11, 15), "monthly", interval=3, count=3)
      == [D(2026, 11, 15), D(2027, 2, 15), D(2027, 5, 15)])
check("yearly feb 29", lambda: X(D(2024, 2, 29, 7), "yearly", count=3) == [D(2024, 2, 29, 7), D(2028, 2, 29, 7), D(2032, 2, 29, 7)])
check("exdate uses count", lambda: X(D(2026, 1, 1, 9), "daily", count=5, exdates=[D(2026, 1, 3, 9)])
      == [D(2026, 1, d, 9) for d in (1, 2, 4, 5)])
check("exdate with until", lambda: X(D(2026, 1, 1, 9), "daily", until=D(2026, 1, 4, 9), exdates=[D(2026, 1, 1, 9)])
      == [D(2026, 1, d, 9) for d in (2, 3, 4)])
check("until before start", lambda: X(D(2026, 5, 1), "daily", until=D(2026, 4, 1)) == [])
check("count zero", lambda: X(D(2026, 5, 1), "daily", count=0) == [])
check("byday sunday end of week", lambda: X(D(2026, 11, 9, 20), "weekly", count=3, byday=["SU", "MO"])
      == [D(2026, 11, 9, 20), D(2026, 11, 15, 20), D(2026, 11, 16, 20)])
check("long until fast", lambda: len(X(D(2000, 1, 1), "daily", until=D(2030, 1, 1))) == 10959)
for bad in [dict(freq="hourly", count=1), dict(freq="daily"), dict(freq="daily", count=1, until=D(2027, 1, 1)),
            dict(freq="monthly", count=2, byday=["MO"]), dict(freq="weekly", count=2, byday=["XX"]),
            dict(freq="daily", count=2, interval=0), dict(freq="daily", count=-1)]:
    check("bad " + repr(bad), lambda bad=bad: raises(ValueError, lambda: X(D(2026, 1, 1), **bad)))
'''
RRULE_REF = '''import calendar
from datetime import datetime, timedelta
CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
def expand(start, freq, *, interval=1, count=None, until=None, byday=None, exdates=()):
    if freq not in ("daily", "weekly", "monthly", "yearly") or interval < 1:
        raise ValueError("freq/interval")
    if (count is None) == (until is None) or (count is not None and count < 0):
        raise ValueError("count/until")
    if byday is not None and (freq != "weekly" or any(d not in CODES for d in byday)):
        raise ValueError("byday")
    def gen():
        k = 0
        if freq == "daily":
            while True:
                yield start + timedelta(days=k * interval); k += 1
        elif freq == "weekly":
            days = sorted({CODES.index(d) for d in byday}) if byday is not None else [start.weekday()]
            monday = start - timedelta(days=start.weekday())
            while True:
                for wd in days:
                    t = monday + timedelta(days=7 * k * interval + wd)
                    if t >= start:
                        yield t
                k += 1
        else:
            step = interval if freq == "monthly" else 12 * interval
            while True:
                m0 = start.month - 1 + k * step
                y, m = start.year + m0 // 12, m0 % 12 + 1
                if start.day <= calendar.monthrange(y, m)[1]:
                    yield start.replace(year=y, month=m)
                k += 1
                if y > 9000:
                    return
    out = []
    for t in gen():
        if count is not None and len(out) >= count:
            break
        if until is not None and t > until:
            break
        out.append(t)
    ex = set(exdates)
    return [t for t in out if t not in ex]
'''

ICS = ("Write a Python function `parse_ics(text: str) -> list[dict]` that reads the events of an iCalendar file. "
       "Rules:\n"
       "- Lines end with CRLF or LF. A line starting with a space or a tab continues the previous one: remove the line "
       "break and that single whitespace character (unfolding), before anything else.\n"
       "- A content line is NAME, then optional ;PARAM=VALUE parts, then ':' and the value. Names and parameter names "
       "are case-insensitive. A parameter value may be in double quotes, and may then contain ':' ';' or ','.\n"
       "- The text must start with BEGIN:VCALENDAR and end with END:VCALENDAR; every BEGIN has its matching END.\n"
       "- Return one dict per VEVENT directly inside the VCALENDAR, in file order, with keys: uid, summary, location "
       "(str, or None when absent), start, end, all_day. Properties of components nested inside a VEVENT (a VALARM "
       "has its own SUMMARY, DESCRIPTION...) belong to them, not to the event.\n"
       "- Text values (UID, SUMMARY, LOCATION) are unescaped: \\n or \\N is a newline, \\, \\; and \\\\ are "
       "',' ';' and '\\'.\n"
       "- DTSTART / DTEND: 'YYYYMMDD' (or with the parameter VALUE=DATE) is a date and makes all_day True; "
       "'YYYYMMDDTHHMMSS' is a naive datetime (a TZID parameter is ignored); with a trailing 'Z' it is a datetime "
       "with tzinfo=timezone.utc.\n"
       "- If there is no DTEND, a DURATION like 'PT1H30M', 'P1D', 'P2W', 'P1DT12H' or 'PT45S' gives end = start + "
       "duration; with neither, end is None.\n"
       "- Raise ValueError for a VEVENT without UID or DTSTART, mismatched BEGIN/END, or a missing VCALENDAR wrapper. "
       "Ignore every other property and component.")
ICS_T = r'''
from datetime import date, datetime, timedelta, timezone
CAL = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Test//IT\r\n"
       "BEGIN:VEVENT\r\nUID:e1@x\r\nDTSTART;TZID=Europe/Rome:20261112T153000\r\nDTEND;TZID=Europe/Rome:20261112T163000\r\n"
       "SUMMARY:Dentista\\, controllo\r\n  annuale\r\n"
       'LOCATION;ALTREP="http://maps.example/a;b,c":Studio Bassi\\, via Roma 12\r\n'
       "BEGIN:VALARM\r\nACTION:DISPLAY\r\nSUMMARY:Promemoria\r\nTRIGGER:-PT30M\r\nEND:VALARM\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nuid:e2@x\r\ndtstart;value=date:20261224\r\nsummary:Vigilia\\nfamiglia\r\nEND:VEVENT\r\n"
       "BEGIN:VEVENT\r\nUID:e3@x\r\nDTSTART:20261201T080000Z\r\nDURATION:PT1H30M\r\nSUMMARY:Volo \; LH1234\r\n"
       "END:VEVENT\r\nBEGIN:VTODO\r\nUID:t1\r\nSUMMARY:not an event\r\nEND:VTODO\r\nEND:VCALENDAR\r\n")
E = parse_ics(CAL)
check("three events", lambda: [e["uid"] for e in E] == ["e1@x", "e2@x", "e3@x"])
check("unfold + unescape", lambda: E[0]["summary"] == "Dentista, controllo annuale")
check("valarm ignored", lambda: E[0]["summary"] != "Promemoria")
check("quoted param", lambda: E[0]["location"] == "Studio Bassi, via Roma 12")
check("tzid naive", lambda: E[0]["start"] == datetime(2026, 11, 12, 15, 30) and E[0]["end"] == datetime(2026, 11, 12, 16, 30)
      and E[0]["all_day"] is False)
check("date + lowercase", lambda: E[1]["start"] == date(2026, 12, 24) and type(E[1]["start"]) is date and E[1]["all_day"] is True)
check("newline escape", lambda: E[1]["summary"] == "Vigilia\nfamiglia")
check("no end, no location", lambda: E[1]["end"] is None and E[1]["location"] is None)
check("utc", lambda: E[2]["start"] == datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc) and E[2]["start"].tzinfo is not None)
check("duration", lambda: E[2]["end"] == datetime(2026, 12, 1, 9, 30, tzinfo=timezone.utc))
check("semicolon escape", lambda: E[2]["summary"] == "Volo ; LH1234")
LF = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nDTSTART:20260101\nDURATION:P2W\nSUMMARY:back\\\\slash\nEND:VEVENT\nEND:VCALENDAR\n"
check("LF + weeks + backslash", lambda: parse_ics(LF)[0]["end"] == date(2026, 1, 15) and parse_ics(LF)[0]["summary"] == "back\\slash")
TAB = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nDTSTART:20260101T100000\nDURATION:P1DT12H\nSUMMARY:a\n\tb\nEND:VEVENT\nEND:VCALENDAR"
check("tab fold + day-hour duration", lambda: parse_ics(TAB)[0]["summary"] == "ab" and parse_ics(TAB)[0]["end"] == datetime(2026, 1, 2, 22))
check("empty calendar", lambda: parse_ics("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n") == [])
for bad in ["", "BEGIN:VEVENT\nUID:a\nDTSTART:20260101\nEND:VEVENT",
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20260101\nEND:VEVENT\nEND:VCALENDAR",
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nEND:VEVENT\nEND:VCALENDAR",
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nDTSTART:20260101\nEND:VCALENDAR",
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:a\nDTSTART:20260101\nEND:VTODO\nEND:VCALENDAR"]:
    check("bad " + repr(bad[:30]), lambda bad=bad: raises(ValueError, parse_ics, bad))
'''
ICS_REF = r'''import re
from datetime import date, datetime, timedelta, timezone
def _split(line):
    name_end = re.match(r"[^;:]*", line).end()
    name, i, params, q = line[:name_end].upper(), name_end, {}, False
    while i < len(line) and line[i] == ";":
        j, buf = i + 1, ""
        while j < len(line):
            c = line[j]
            if c == '"':
                q = not q
            elif not q and c in ";:":
                break
            buf += c
            j += 1
        k, _, v = buf.partition("=")
        params[k.upper()] = v.strip('"')
        i = j
    if i >= len(line) or line[i] != ":":
        raise ValueError("bad line " + line)
    return name, params, line[i + 1:]
def _text(v):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v)
def _when(v, params):
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", v):
        return date(int(v[:4]), int(v[4:6]), int(v[6:8])), True
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", v)
    if not m:
        raise ValueError(v)
    d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return (d.replace(tzinfo=timezone.utc) if m.group(3) else d), False
def _dur(v):
    m = re.fullmatch(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v)
    if not m or v in ("P", "PT"):
        raise ValueError(v)
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
def parse_ics(text):
    text = re.sub(r"\r?\n[ \t]", "", text)
    lines = [l for l in re.split(r"\r?\n", text) if l != ""]
    if not lines or lines[0].upper() != "BEGIN:VCALENDAR" or lines[-1].upper() != "END:VCALENDAR":
        raise ValueError("not a calendar")
    stack, events, cur = [], [], None
    for line in lines:
        name, params, value = _split(line)
        if name == "BEGIN":
            stack.append(value.upper())
            if stack == ["VCALENDAR", "VEVENT"]:
                cur = {}
            continue
        if name == "END":
            if not stack or stack[-1] != value.upper():
                raise ValueError("mismatched END")
            if stack == ["VCALENDAR", "VEVENT"]:
                if "UID" not in cur or "DTSTART" not in cur:
                    raise ValueError("event without UID or DTSTART")
                start, all_day = cur["DTSTART"]
                end = cur["DTEND"][0] if "DTEND" in cur else (start + cur["DURATION"] if "DURATION" in cur else None)
                events.append({"uid": cur["UID"], "summary": cur.get("SUMMARY"), "location": cur.get("LOCATION"),
                               "start": start, "end": end, "all_day": all_day})
                cur = None
            stack.pop()
            continue
        if stack == ["VCALENDAR", "VEVENT"]:
            if name in ("UID", "SUMMARY", "LOCATION"):
                cur[name] = _text(value)
            elif name in ("DTSTART", "DTEND"):
                cur[name] = _when(value, params)
            elif name == "DURATION":
                cur[name] = _dur(value)
    if stack:
        raise ValueError("unclosed")
    return events
'''

NEW = [
    Task("parse_amount", "easy", "Italian amount → cents, strict grammar, 16 invalid inputs", AMOUNT, AMOUNT_T,
         tags=("code", "money"), reference=AMOUNT_REF),
    Task("iban_check", "easy", "IBAN mod-97 validation and grouping", IBAN, IBAN_T, tags=("code", "money"), reference=IBAN_REF),
    Task("split_bill", "easy", "proportional split in whole cents, largest remainder", SPLIT, SPLIT_T, tags=("code", "money"),
         reference=SPLIT_REF),
    Task("free_slots", "medium", "free gaps between overlapping busy intervals in a window", SLOTS, SLOTS_T,
         tags=("code", "life"), reference=SLOTS_REF),
    Task("recurring_detect", "medium", "find monthly charges: gaps, median tolerance, next date", RECUR, RECUR_T,
         tags=("code", "money"), reference=RECUR_REF),
    Task("italian_dates", "medium", "'giovedì prossimo', 'tra 2 settimane', '5/12' → date", WHEN, WHEN_T, tags=("code", "life"),
         reference=WHEN_REF),
    Task("rrule_expand", "hard", "RRULE expansion: byday weeks, skipped months, leap years, count before exdates", RRULE,
         RRULE_T, tags=("code", "life"), reference=RRULE_REF),
    Task("ics_parse", "hard", "iCalendar parser: unfolding, quoted params, escapes, nested VALARM, durations", ICS, ICS_T,
         tags=("code", "life"), reference=ICS_REF),
]
