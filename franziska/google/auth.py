"""OAuth for the user's own Google Cloud project: a one-time sign-in, then refresh tokens forever.

Sign in once (opens a browser; on a headless box see deploy/BOX.md for the SSH tunnel):

    uv run --with google-auth-oauthlib==1.5.0 python -m franziska.google.auth path/to/client_secret.json

That writes `~/.local/share/franziska/google_token.json` (mode 600): the refresh token plus the client id/secret, in google-auth's
"authorized user" format. At runtime only this module and httpx are used: no Google SDK in the bot.

Scopes are the narrowest that do the job: calendar events read/write (not calendar settings or sharing) and
Gmail read-only plus send (only the market report, to your own address; no deleting). Keep the OAuth app "In production" (unverified is fine for one user):
in "Testing" status Google expires refresh tokens after 7 days.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",  # only the market report to the user's own address
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleAuthError(RuntimeError):
    """No token, or Google refused to refresh it (revoked, expired 'Testing' app): sign in again."""


class GoogleClient:
    """httpx client that adds a fresh access token to every request and retries once on 401."""

    def __init__(self, token_path: Path, transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self.token_path = token_path
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._access = ""
        self._expires = 0.0
        self.calls = 0  # API requests made, for logging what a job cost

    @property
    def configured(self) -> bool:
        return self.token_path.exists()

    def _info(self) -> dict:
        if not self.token_path.exists():
            raise GoogleAuthError(f"no Google token at {self.token_path}; run: python -m franziska.google.auth")
        return json.loads(self.token_path.read_text())

    def _refresh(self) -> None:
        info = self._info()
        r = self._http.post(info.get("token_uri", TOKEN_URI), data={
            "grant_type": "refresh_token", "refresh_token": info["refresh_token"],
            "client_id": info["client_id"], "client_secret": info["client_secret"]})
        if r.status_code != 200:
            raise GoogleAuthError(f"Google refused the refresh token ({r.status_code} {r.text[:200]}); sign in again")
        body = r.json()
        self._access = body["access_token"]
        self._expires = time.time() + float(body.get("expires_in", 3600)) - 60

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in (1, 2):
            if not self._access or time.time() >= self._expires:
                self._refresh()
            r = self._http.request(method, url, headers={"Authorization": f"Bearer {self._access}"}, **kwargs)
            self.calls += 1
            if r.status_code == 401 and attempt == 1:
                self._access = ""
                continue
            r.raise_for_status()
            return r
        raise AssertionError("unreachable")


def login(client_secret: Path, token_path: Path, port: int = 0) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow  # only for this one-off command

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=False, access_type="offline", prompt="consent")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)
    print(f"Saved {token_path} with scopes: {', '.join(creds.scopes or SCOPES)}")


if __name__ == "__main__":
    from franziska.config import Settings

    if len(sys.argv) != 2:
        sys.exit(__doc__)
    login(Path(sys.argv[1]), Settings.from_env().google_token)
