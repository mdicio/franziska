"""A stand-in for `gog mcp` over a JSON mailbox, for evals: same tool names and input schemas (gog_tools.json, a
snapshot of gog v0.47.1's `tools/list`), same result envelope, no Google account.

    FAKE_GOG_STATE=/tmp/box.json python -m evals.fake_gog --account x --gmail-no-send mcp --allow-tool gmail_search

The state file holds `messages`, `drafts` and `labels`; every write is saved back to it, so the judge reads the
mailbox after a run. gog's own flags are accepted and ignored except --allow-tool (which tools are listed).
Gmail search syntax is covered well enough for an agent: field operators, quoted phrases, OR, {a b}, -negation,
dates, in:/is:/has:/label:/category:. Words match whole words, accents and case ignored, as Gmail does.

A minimal MCP server (newline-delimited JSON-RPC on stdio) rather than the SDK, so the schemas are gog's verbatim.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

TOOLS = json.loads((Path(__file__).parent / "gog_tools.json").read_text())
SYSTEM_LABELS = ["INBOX", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "TRASH", "SPAM", "CATEGORY_PERSONAL",
                 "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"]
WRITES = {t["name"] for t in TOOLS} - {"gmail_search", "gmail_get_message", "gmail_get_thread", "gmail_list_drafts",
                                       "gmail_get_draft", "gmail_list_labels"}


class GogError(Exception):
    pass


def norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", norm(text)))


def has_phrase(text: str, phrase: str) -> bool:
    toks = re.findall(r"[a-z0-9]+", norm(phrase))
    return bool(toks) and re.search(r"\b" + r"\W+".join(map(re.escape, toks)) + r"\b", norm(text)) is not None


class Box:
    def __init__(self, path: Path):
        self.path = path
        self.state = json.loads(path.read_text())
        self.state.setdefault("drafts", [])
        self.state.setdefault("labels", [])

    def save(self) -> None:
        self.path.write_text(json.dumps(self.state, indent=1, ensure_ascii=False))

    @property
    def messages(self) -> list[dict]:
        return self.state["messages"]

    def message(self, mid: str) -> dict:
        for m in self.messages:
            if m["id"] == mid:
                return m
        raise GogError(f"message not found: {mid}")

    def label_id(self, name_or_id: str, create: bool = False) -> str:
        if name_or_id.upper() in SYSTEM_LABELS:
            return name_or_id.upper()
        for lab in self.state["labels"]:
            if name_or_id in (lab["id"], lab["name"]) or norm(name_or_id) == norm(lab["name"]):
                return lab["id"]
        if create:
            return self.create_label(name_or_id)["id"]
        raise GogError(f"label not found: {name_or_id}")

    def create_label(self, name: str) -> dict:
        if any(norm(lab["name"]) == norm(name) for lab in self.state["labels"]):
            raise GogError(f"label already exists: {name}")
        lab = {"id": f"Label_{len(self.state['labels']) + 1}", "name": name, "type": "user"}
        self.state["labels"].append(lab)
        return lab

    def label_name(self, lid: str) -> str:
        return next((lab["name"] for lab in self.state["labels"] if lab["id"] == lid), lid)


# ---- search ---------------------------------------------------------------------------------------------------------
TOKEN = re.compile(r'-?\{[^}]*\}|-?[a-zA-Z_]+:"[^"]*"|-?[a-zA-Z_]+:\([^)]*\)|-?"[^"]*"|\(|\)|\S+')


def _date(value: str) -> datetime:
    return datetime.strptime(value.replace("-", "/"), "%Y/%m/%d").replace(tzinfo=UTC)


def _ago(value: str) -> timedelta:
    n, unit = int(value[:-1]), value[-1].lower()
    return timedelta(days=n * {"d": 1, "m": 30, "y": 365}.get(unit, 1)) if unit in "dmy" else timedelta(days=int(value))


def _term(box: Box, m: dict, term: str, now: datetime) -> bool:
    if term.startswith("{") and term.endswith("}"):
        return any(_term(box, m, t, now) for t in TOKEN.findall(term[1:-1]))
    if term.startswith('"'):
        return has_phrase(_text(m), term.strip('"'))
    field, sep, value = term.partition(":")
    if not sep or not value:
        return bool(words(term)) and words(term) <= words(_text(m))
    value = value.strip('"')
    if value.startswith("(") and value.endswith(")"):
        return all(_term(box, m, f"{field}:{v}", now) for v in value[1:-1].split())
    field, labels, date = field.lower(), set(m["labels"]), datetime.fromisoformat(m["date"])
    if field == "from":
        return has_phrase(m["from"], value) or norm(value) in norm(m["from"])
    if field in ("to", "cc", "bcc", "deliveredto"):
        return norm(value) in norm(m.get("to", ""))
    if field == "subject":
        return has_phrase(m["subject"], value) or words(value) <= words(m["subject"])
    if field == "label":
        try:
            return box.label_id(value) in labels
        except GogError:
            return value.upper().replace("-", "_") in labels
    if field == "category":
        return f"CATEGORY_{value.upper()}" in labels
    if field == "is":
        return {"unread": "UNREAD" in labels, "read": "UNREAD" not in labels, "starred": "STARRED" in labels,
                "important": "IMPORTANT" in labels}.get(value.lower(), False)
    if field == "in":
        v = value.lower()
        return v == "anywhere" or v.upper() in labels or (v == "sent" and "SENT" in labels)
    if field == "has":
        return value.lower() == "attachment" and bool(m.get("attachments"))
    if field == "filename":
        return any(norm(value) in norm(a) for a in m.get("attachments", []))
    if field in ("newer_than", "older_than"):
        newer = date >= now - _ago(value)
        return newer if field == "newer_than" else not newer
    if field in ("after", "before"):
        try:
            d = _date(value)
        except ValueError:
            return False
        return date >= d if field == "after" else date < d
    if field == "list":
        return norm(value) in norm(m.get("headers", {}).get("List-Id", ""))
    return words(term) <= words(_text(m))  # unknown operator: as words


def _text(m: dict) -> str:
    return " ".join([m["from"], m.get("to", ""), m["subject"], m["body"], " ".join(m.get("attachments", []))])


def matches(box: Box, m: dict, query: str, now: datetime) -> bool:
    tokens = TOKEN.findall(query)
    if not any(t.lower().startswith(("in:anywhere", "in:trash", "in:spam", "label:trash", "label:spam"))
               for t in tokens) and {"TRASH", "SPAM"} & set(m["labels"]):
        return False
    groups, current = [], []  # OR binds tighter than the implicit AND, as in Gmail
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in ("(", ")"):
            i += 1
            continue
        if t == "OR" and current:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            current[-1] = current[-1] + [nxt]
            i += 2
            continue
        current.append([t])
        i += 1
    groups = current
    for alternatives in groups:
        ok = False
        for t in alternatives:
            neg = t.startswith("-") and len(t) > 1
            hit = _term(box, m, t[1:] if neg else t, now)
            ok = ok or (not hit if neg else hit)
        if not ok:
            return False
    return True


def summary(box: Box, m: dict, body: bool = False, recipients: bool = False, headers: list | None = None) -> dict:
    out = {"id": m["id"], "threadId": m["threadId"], "date": m["date"], "from": m["from"], "subject": m["subject"],
           "snippet": m["body"][:160].replace("\n", " "), "labels": [box.label_name(x) for x in m["labels"]]}
    if m.get("attachments"):
        out["attachments"] = m["attachments"]
    if recipients:
        out["to"] = m.get("to", "")
    if body:
        out["body"] = m["body"]
    if headers:
        out["headers"] = {h: m.get("headers", {}).get(h, "") for h in headers}
    return out


# ---- tools ----------------------------------------------------------------------------------------------------------
def call(box: Box, name: str, a: dict, now: datetime) -> dict:
    if name == "gmail_search":
        hits = sorted((m for m in box.messages if matches(box, m, a["query"], now)), key=lambda m: m["date"],
                      reverse=True)
        start = int(a.get("page") or 0)
        page = hits[start:start + int(a.get("max", 10))]
        out = {"messages": [summary(box, m, a.get("include_body", False), a.get("include_recipients", False),
                                    a.get("headers")) for m in page], "resultSizeEstimate": len(hits)}
        if start + len(page) < len(hits):
            out["nextPageToken"] = str(start + len(page))
        return out
    if name == "gmail_get_message":
        m = box.message(a["message_id"])
        return {**summary(box, m, body=True, recipients=True), "headers": m.get("headers", {})}
    if name == "gmail_get_thread":
        msgs = [m for m in box.messages if m["threadId"] == a["thread_id"]]
        if not msgs:
            raise GogError(f"thread not found: {a['thread_id']}")
        return {"id": a["thread_id"], "messages": [summary(box, m, body=a.get("full", False), recipients=True)
                                                    for m in sorted(msgs, key=lambda m: m["date"])]}
    if name == "gmail_list_drafts":
        return {"drafts": [{"id": d["id"], "messageId": d["messageId"], "threadId": d["threadId"]}
                           for d in box.state["drafts"]]}
    if name == "gmail_get_draft":
        d = next((d for d in box.state["drafts"] if d["id"] == a["draft_id"]), None)
        if not d:
            raise GogError(f"draft not found: {a['draft_id']}")
        return d
    if name == "gmail_list_labels":
        return {"labels": [{"id": x, "name": x, "type": "system"} for x in SYSTEM_LABELS] + box.state["labels"]}
    if name in ("gmail_create_draft", "gmail_update_draft"):
        if not (a.get("body") or a.get("body_html")):
            raise GogError("body or body_html is required")
        thread = a.get("thread_id")
        to = a.get("to")
        if a.get("reply_to_message_id"):
            target = box.message(a["reply_to_message_id"])
            thread = target["threadId"]
            to = to or target["from"]
        if name == "gmail_update_draft":
            d = next((d for d in box.state["drafts"] if d["id"] == a["draft_id"]), None)
            if not d:
                raise GogError(f"draft not found: {a['draft_id']}")
            d.update({"subject": a["subject"], "body": a.get("body") or a.get("body_html"),
                      "to": d["to"] if to is None else to, "cc": a.get("cc", ""), "bcc": a.get("bcc", "")})
        else:
            n = len(box.state["drafts"]) + 1
            d = {"id": f"r-{n:04d}", "messageId": f"d{n:04d}", "threadId": thread or f"t-draft-{n}",
                 "to": to or "", "cc": a.get("cc", ""), "bcc": a.get("bcc", ""), "subject": a["subject"],
                 "body": a.get("body") or a.get("body_html"), "replyTo": a.get("reply_to_message_id", "")}
            box.state["drafts"].append(d)
        box.save()
        return {"draftId": d["id"], "message": {"id": d["messageId"], "threadId": d["threadId"]}}
    if name == "gmail_create_label":
        lab = box.create_label(a["name"])
        box.save()
        return lab
    if name in ("gmail_modify_messages", "gmail_modify_thread", "gmail_mark_read", "gmail_mark_unread",
                "gmail_archive_messages", "gmail_trash_messages", "gmail_restore_messages"):
        if name == "gmail_modify_thread":
            ids = [m["id"] for m in box.messages if m["threadId"] == a["thread_id"]]
        else:
            ids = list(a["message_ids"])
        add, remove = {
            "gmail_mark_read": ([], ["UNREAD"]), "gmail_mark_unread": (["UNREAD"], []),
            "gmail_archive_messages": ([], ["INBOX"]), "gmail_trash_messages": (["TRASH"], ["INBOX"]),
            "gmail_restore_messages": ([], ["TRASH"]),
        }.get(name, (a.get("add_labels", []), a.get("remove_labels", [])))
        add_ids = [box.label_id(x) for x in add]
        remove_ids = [box.label_id(x) for x in remove]
        for mid in ids:
            m = box.message(mid)
            m["labels"] = [x for x in m["labels"] if x not in remove_ids] + [x for x in add_ids if x not in m["labels"]]
        box.save()
        return {"modified": len(ids), "ids": ids}
    raise GogError(f"unknown tool {name}")


def main() -> None:
    allowed = None
    if "--allow-tool" in sys.argv:
        allowed = set(sys.argv[sys.argv.index("--allow-tool") + 1].split(","))
    tools = [t for t in TOOLS if allowed is None or t["name"] in allowed]
    box_path = Path(os.environ["FAKE_GOG_STATE"])
    now = datetime.now(UTC)
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        if "id" not in req:
            continue  # notifications
        method, rid = req.get("method"), req["id"]
        if method == "initialize":
            result = {"protocolVersion": req["params"].get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-gog", "version": "0.47.1"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            name, args = req["params"]["name"], req["params"].get("arguments") or {}
            env = {"tool": name, "service": "gmail", "risk": "write" if name in WRITES else "read"}
            try:
                if name not in {t["name"] for t in tools}:
                    raise GogError(f"tool not enabled: {name}")
                out = call(Box(box_path), name, args, now)
                env |= {"exit_code": 0, "stdout": out, "stderr": ""}
                result = {"content": [{"type": "text", "text": json.dumps(env, ensure_ascii=False)}],
                          "structuredContent": env, "isError": False}
            except (GogError, KeyError, ValueError) as e:
                env |= {"exit_code": 1, "stderr": f"{type(e).__name__}: {e}"}
                result = {"content": [{"type": "text", "text": json.dumps(env, ensure_ascii=False)}],
                          "structuredContent": env, "isError": True}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                         "error": {"code": -32601, "message": f"no method {method}"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
