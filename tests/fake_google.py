"""A tiny in-memory Google (token endpoint, Calendar events, Gmail metadata) behind httpx.MockTransport."""

import json
from datetime import datetime, timedelta
from urllib.parse import unquote

import httpx


class FakeGoogle:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.mail: dict[str, dict] = {}
        self.refreshes = 0
        self.requests: list[httpx.Request] = []
        self.reject_next_token = False  # simulate an access token revoked server-side
        self._n = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_series(self, title: str, start: datetime, weeks: int, tz: str = "+01:00") -> str:
        """A weekly series as Google lists it with singleEvents=true: one instance per week."""
        self._n += 1
        base = f"series{self._n}"
        for i in range(weeks):
            s = start + timedelta(weeks=i)
            iid = f"{base}_{s:%Y%m%dT%H%M%S}Z"
            self.events[iid] = {"id": iid, "status": "confirmed", "summary": title, "recurringEventId": base,
                                "start": {"dateTime": s.isoformat() + tz},
                                "end": {"dateTime": (s + timedelta(hours=1)).isoformat() + tz}}
        return base

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = unquote(request.url.path)
        if request.url.host == "oauth2.googleapis.com":
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": f"at{self.refreshes}", "expires_in": 3600})
        if self.reject_next_token:
            self.reject_next_token = False
            return httpx.Response(401, json={"error": "invalid token"})
        if "/calendar/v3/calendars/primary/events" in path:
            return self._calendar(request, path)
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": i} for i in self.mail]})
        if "/messages/" in path:
            return httpx.Response(200, json=self.mail[path.rsplit("/", 1)[1]])
        return httpx.Response(404)

    def _calendar(self, request: httpx.Request, path: str) -> httpx.Response:
        event_id = path.split("/events", 1)[1].strip("/")
        if request.method == "GET":
            lo, hi = request.url.params["timeMin"], request.url.params["timeMax"]

            def start(e):
                return e["start"].get("dateTime", e["start"].get("date"))[:16]

            items = [e for e in self.events.values() if lo[:16] <= start(e) < hi[:16]]
            return httpx.Response(200, json={"items": sorted(items, key=lambda e: str(e["start"]))})
        if request.method == "POST":
            body = json.loads(request.content)
            self._n += 1
            event = {"id": f"ev{self._n}", "status": "confirmed", **body}
            self.events[event["id"]] = event
            return httpx.Response(200, json=event)
        if request.method == "PATCH":
            self.events[event_id].update(json.loads(request.content))
            return httpx.Response(200, json=self.events[event_id])
        if request.method == "DELETE":
            self.events.pop(event_id)
            return httpx.Response(204)
        return httpx.Response(405)


def token_file(tmp_path):
    path = tmp_path / "google_token.json"
    path.write_text(json.dumps({"refresh_token": "rt", "client_id": "cid", "client_secret": "cs",
                                "token_uri": "https://oauth2.googleapis.com/token"}))
    return path
