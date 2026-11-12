"""OAuth for the user's own Google Cloud project: a one-time sign-in, then refresh tokens forever.

Sign in once, from the web app's Setup page (web/onboarding.py: upload the app's JSON, one consent in the browser;
`sign_in_url` and `finish` below), or in a terminal (opens a browser; on a headless box see deploy/BOX.md for the SSH
tunnel):

    uv run --with google-auth-oauthlib==1.5.0 python -m franziska.google.auth path/to/client_secret.json

That writes `~/.local/share/franziska/google_token.json` (mode 600): the refresh token plus the client id/secret, in google-auth's
"authorized user" format. At runtime only this module and httpx are used: no Google SDK in the bot.

Scopes are the narrowest that do the job: calendar events read/write (not calendar settings or sharing) and
Gmail read-only plus send (only the market report, to your own address; no deleting). Keep the OAuth app "In production" (unverified is fine for one user):
in "Testing" status Google expires refresh tokens after 7 days.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",  # only the market report to the user's own address
]
# The mail assistant's, through gog (franziska/mail/agent.py GOG_TOOLS): Gmail drafts, labels, archive and trash (never
# a permanent delete), Drive search, Docs and Sheets, calendar reads. Asked in the same consent when gog is installed,
# so one refresh token serves both (Setup page: `gog auth import`).
MAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.readonly",
]
GOG_SERVICES = "gmail,drive,docs,sheets,calendar"  # recorded on gog's token (informational)
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
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


def read_client(data: bytes) -> dict:
    """The OAuth client in the JSON Google Cloud lets you download (Credentials → OAuth client ID → Desktop app):
    its `installed` part. ValueError, saying what to fix, for anything else."""
    try:
        info = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("That isn't the JSON file Google Cloud downloads for an OAuth client.") from None
    if not isinstance(info, dict):
        raise ValueError("That isn't the JSON file Google Cloud downloads for an OAuth client.")
    if "web" in info:
        raise ValueError("This client is a 'Web application'. Create one of type 'Desktop app' and download that.")
    client = info.get("installed")
    if not isinstance(client, dict) or not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("No OAuth client in this file: download it from Credentials → OAuth client ID → Desktop app.")
    return client


def sign_in_url(client: dict, redirect_uri: str, scopes: list[str]) -> tuple[str, str, str]:
    """(the consent page's address, its state, the PKCE verifier) for a Desktop client: Google sends the browser
    back to `redirect_uri` (a loopback address, any port and path) with the code."""
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTH_URI}?{urlencode(query)}", state, verifier


def finish(client: dict, code: str, redirect_uri: str, verifier: str, token_path: Path,
           transport: httpx.BaseTransport | None = None) -> dict:
    """Trade the code for tokens and save them as `token_path` (the format GoogleClient reads, mode 600). Returns
    Google's answer: `access_token`, `refresh_token`, `scope` (what the user granted)."""
    with httpx.Client(timeout=20, transport=transport) as http:
        r = http.post(client.get("token_uri", TOKEN_URI), data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier,
            "client_id": client["client_id"], "client_secret": client["client_secret"]})
    if r.status_code != 200:
        raise ValueError(f"Google didn't accept the sign-in ({r.status_code} {r.text[:200]}). Sign in again.")
    body = r.json()
    if not body.get("refresh_token"):
        raise ValueError("Google sent no refresh token. Remove Franziska's access at myaccount.google.com/permissions, "
                         "then sign in again.")
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.touch(mode=0o600)
    os.chmod(token_path, 0o600)
    token_path.write_text(json.dumps({
        "token": body["access_token"], "refresh_token": body["refresh_token"],
        "token_uri": client.get("token_uri", TOKEN_URI), "client_id": client["client_id"],
        "client_secret": client["client_secret"], "scopes": body.get("scope", "").split(),
        "expiry": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + float(body.get("expires_in", 3600))))}))
    return body


def account(access_token: str, transport: httpx.BaseTransport | None = None) -> str:
    """The signed-in Gmail address (gog keys its token by it), from Gmail's profile: no extra scope."""
    with httpx.Client(timeout=20, transport=transport) as http:
        r = http.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                     headers={"Authorization": f"Bearer {access_token}"})
    r.raise_for_status()
    return r.json()["emailAddress"]


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
