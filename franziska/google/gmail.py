"""Gmail: search messages or conversations, read headers and snippet (triage) or the full text, and download
attachments one by one when asked. Nothing is stored here. The one thing sent is mail to the account itself
(`send_to_self`: the market report), never to anyone else.

Workflows work per conversation: one `thread_headers` call gives every subject in a thread and one `thread` call
every body, instead of one request per message."""

from __future__ import annotations

import base64
import html
from email.message import EmailMessage
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from franziska.google.auth import GoogleClient

API = "https://gmail.googleapis.com/gmail/v1/users/me"
# Promotions/social/forums are Gmail's own tabs: skipped before anything reaches the triage model.
SKIP = "-category:promotions -category:social -category:forums"
DEFAULT_QUERY = f"newer_than:2d {SKIP}"


@dataclass(frozen=True)
class MailHeader:
    id: str
    thread_id: str
    sender: str
    subject: str
    received_at: str  # naive local ISO
    snippet: str
    address: str = ""  # the sender's e-mail address, lowercase; `sender` is the display name when there is one


@dataclass(frozen=True)
class Attachment:
    """An attachment's metadata; `Gmail.download` fetches the bytes (one request each)."""

    message_id: str
    attachment_id: str
    filename: str
    mime: str
    size: int


@dataclass(frozen=True)
class MailContent:
    header: MailHeader
    text: str  # the plain-text part, or the HTML part as text
    attachments: list[Attachment] = field(default_factory=list)


class _Text(HTMLParser):
    """HTML mail to text: block tags become line breaks, scripts and styles are dropped."""

    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(ln.split()) for ln in "".join(self.parts).splitlines())
        return "\n".join(ln for ln in lines if ln)


def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _parts(part: dict):
    yield part
    for p in part.get("parts", []):
        yield from _parts(p)


class Gmail:
    def __init__(self, client: GoogleClient, timezone: str = "Europe/Rome"):
        self.client, self.tz = client, ZoneInfo(timezone)

    def recent_ids(self, query: str = DEFAULT_QUERY, limit: int = 50) -> list[str]:
        """Ids of the newest messages matching a Gmail search, up to `limit`."""
        return self._search("messages", query, limit)

    def thread_ids(self, query: str, limit: int = 100) -> list[str]:
        """Ids of the newest conversations with a message matching a Gmail search, up to `limit`."""
        return self._search("threads", query, limit)

    def _search(self, kind: str, query: str, limit: int) -> list[str]:
        ids, page = [], None
        while len(ids) < limit:  # paged: Gmail returns 500 at most per request
            params = {"q": query, "maxResults": str(min(limit - len(ids), 500))} | ({"pageToken": page} if page else {})
            body = self.client.request("GET", f"{API}/{kind}", params=params).json()
            ids += [m["id"] for m in body.get(kind, [])]
            if not (page := body.get("nextPageToken")):
                break
        return ids

    def thread_headers(self, thread_id: str) -> list[MailHeader]:
        """Sender, subject, date and snippet of every message in a conversation, oldest first: one request."""
        t = self.client.request("GET", f"{API}/threads/{thread_id}", params=[
            ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
            ("metadataHeaders", "Date")]).json()
        return [self._header(m) for m in t.get("messages", [])]

    def thread(self, thread_id: str) -> list[MailContent]:
        """Every message of a conversation with its text and attachment metadata, oldest first: one request."""
        t = self.client.request("GET", f"{API}/threads/{thread_id}", params={"format": "full"}).json()
        return [self._content(m) for m in t.get("messages", [])]

    def me(self) -> str:
        """The signed-in account's address, lowercase (asked once)."""
        if not getattr(self, "_me", ""):
            self._me = self.client.request("GET", f"{API}/profile").json()["emailAddress"].lower()
        return self._me

    def send_to_self(self, subject: str, body: str, files: list[Path] = ()) -> str:
        """Mail to the signed-in account's own address, with attachments. Returns the message id."""
        me = self.me()
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = me, me, subject
        msg.set_content(body)
        for f in files:
            msg.add_attachment(Path(f).read_bytes(), maintype="application",
                               subtype="pdf" if f.suffix == ".pdf" else "octet-stream", filename=f.name)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self.client.request("POST", f"{API}/messages/send", json={"raw": raw}).json()["id"]

    def download(self, a: Attachment) -> bytes:
        body = self.client.request("GET", f"{API}/messages/{a.message_id}/attachments/{a.attachment_id}").json()
        return _b64(body["data"])

    def header(self, message_id: str) -> MailHeader:
        m = self.client.request("GET", f"{API}/messages/{message_id}", params=[
            ("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"),
            ("metadataHeaders", "Date")]).json()
        return self._header(m)

    def content(self, message_id: str) -> MailContent:
        """One message's text and attachment metadata."""
        return self._content(self.client.request("GET", f"{API}/messages/{message_id}", params={"format": "full"}).json())

    def _content(self, m: dict) -> MailContent:
        plain, rich, attachments = [], [], []
        for p in _parts(m.get("payload", {})):
            body, mime = p.get("body", {}), p.get("mimeType", "")
            if p.get("filename"):
                if body.get("attachmentId"):
                    attachments.append(Attachment(m["id"], body["attachmentId"], p["filename"], mime,
                                                  body.get("size", 0)))
            elif body.get("data") and mime == "text/plain":
                plain.append(_b64(body["data"]).decode("utf-8", "replace"))
            elif body.get("data") and mime == "text/html":
                parser = _Text()
                parser.feed(_b64(body["data"]).decode("utf-8", "replace"))
                rich.append(parser.text())
        return MailContent(self._header(m), "\n".join(plain or rich).strip(), attachments)

    def _header(self, m: dict) -> MailHeader:
        h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
        name, addr = parseaddr(h.get("from", ""))
        try:
            received = parsedate_to_datetime(h["date"]).astimezone(self.tz)
        except (KeyError, TypeError, ValueError):
            received = datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, self.tz)
        return MailHeader(m["id"], m.get("threadId", ""), name or addr, h.get("subject", "(no subject)"),
                          received.replace(tzinfo=None).isoformat(timespec="minutes"),
                          html.unescape(m.get("snippet", "")), addr.lower())
