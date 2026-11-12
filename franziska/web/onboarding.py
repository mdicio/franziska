"""The Setup page's backend: what this installation has, what each optional piece adds, and how to turn it on, from
the browser. The CLI's `init` and `doctor` do the same in a terminal (franziska/setup.py); this page is for someone
who cloned the repo and ran `uv run python -m franziska.web`.

Franziska works with the language model alone: chat, tasks, notes, money, documents and web search, in the web app.
Every other step is optional and says what it adds. State is read from the saved .env, not from this process's
environment, so a step shows as done as soon as it is saved; the services pick it up when they restart.

Secrets (the Telegram token) are write-only here: the page is told whether one is set, never its value. Nothing runs
with sudo; a step that needs a system package says the command instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from franziska import setup as cli
from franziska.config import ROOT, Settings
from franziska.google import auth as google_auth
from franziska.web import control

TELEGRAM = "https://api.telegram.org"
UNITS = ("franziska-bot.service", "franziska-web.service")  # what "start at login" installs (deploy/)

# The keys this page writes on top of the Settings page's (control.FIELDS). kind: text | secret | ids | time | tz |
# bool | email | path, and the control kinds for the keys it shares.
FIELDS = {
    "FRANZISKA_TIMEZONE": dict(label="Time zone", kind="tz", help="Reminders and dates are in this zone."),
    "FRANZISKA_OWNER_NAME": dict(label="Your name, as your bank writes it", kind="text",
                                 help="Tells transfers between your own accounts from payments to other people."),
    "FRANZISKA_BRIEF_AT": dict(label="Morning brief at", kind="time", help="HH:MM, empty for none.",
                               default=Settings.brief_at),
    "FRANZISKA_EVENING_AT": dict(label="Evening check-in at", kind="time", help="HH:MM, empty for none.",
                                 default=Settings.evening_at),
    "FRANZISKA_TELEGRAM_TOKEN": dict(label="Bot token", kind="secret",
                                     help="From @BotFather. Saved in your .env, never shown again here."),
    "FRANZISKA_ALLOWED_IDS": dict(label="Your Telegram user id", kind="ids",
                                  help="Only these ids are answered. Comma-separated for more than one person."),
    "FRANZISKA_BACKUP_DIR": dict(label="Backup folder", kind="path",
                                 help="On another disk or a mounted drive: the nightly copy of every store."),
    "FRANZISKA_GOOGLE": dict(label="Google Calendar and Gmail", kind="bool", help=""),
    "FRANZISKA_GOG_ACCOUNT": dict(label="Gmail address for the mail assistant", kind="email",
                                  help="Set by the Google sign-in above. Empty turns the mail assistant off."),
    "FRANZISKA_PC": dict(label="The PC assistant", kind="bool",
                         help="Questions about this computer, its files, coding jobs. Each change is a button."),
}


# ---- the saved configuration ---------------------------------------------------------------------------------------
def configured() -> bool:
    return (ROOT / ".env").exists()


def saved() -> Settings:
    """Settings as the saved .env says, on top of the non-Franziska environment."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("FRANZISKA_")}
    return Settings.from_env({**base, **control.read_env()})


def spec(key: str) -> dict:
    if key in FIELDS:
        return {"key": key, **FIELDS[key]}
    if key in control.BY_KEY:
        return control.BY_KEY[key]
    raise ValueError(f"{key} can't be changed here")


def clean(key: str, value) -> str:
    """The .env string for a submitted value; ValueError for anything the field doesn't allow."""
    field = spec(key)
    kind = field["kind"]
    if key in control.BY_KEY:
        return control.clean(field, value)
    if kind == "bool":
        return "1" if value in (True, "1", 1, "true") else "0"
    text = str(value if value is not None else "").strip()
    if any(c in text for c in "\n\r\0"):
        raise ValueError(f"{field['label']}: one line only")
    if not text:
        if kind == "tz":
            raise ValueError("Time zone: required")
        return ""
    if kind == "secret" and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", text):
        raise ValueError("Bot token: it looks like 123456789:AA… (copy it whole from @BotFather)")
    if kind == "ids" and not re.fullmatch(r"-?\d+(\s*,\s*-?\d+)*", text):
        raise ValueError(f"{field['label']}: numbers only, comma-separated")
    if kind == "ids":
        text = ",".join(p.strip() for p in text.split(","))
    if kind == "time" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        raise ValueError(f"{field['label']}: HH:MM, like 07:30")
    if kind == "tz":
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"Time zone: {text!r} isn't one (like Europe/Rome or America/New_York)") from None
    if kind == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValueError(f"{field['label']}: an email address")
    if kind == "path" and not (text.startswith("/") or text.startswith("~")):
        raise ValueError(f"{field['label']}: a full path, like /mnt/backup/franziska")
    return text


def save(values: dict) -> dict:
    """Validate, start the configuration when there is none (setup.write_config), write what changed. An empty
    secret keeps the saved one."""
    env = control.read_env() if configured() else {}
    changes = {}
    for key, raw in values.items():
        if spec(key)["kind"] == "secret" and not str(raw or "").strip():
            continue
        new = clean(key, raw)
        if new != env.get(key, ""):
            changes[key] = new
    started = []
    if not configured():
        # The model as the page showed it (.env.example's own default is llama-server): a first save of another step
        # must not switch a working Ollama to a server that isn't there. The model step's how-to suggests the rest.
        now = saved()
        defaults = {"FRANZISKA_TIMEZONE": cli.local_timezone(), "FRANZISKA_BASE_URL": now.base_url,
                    "FRANZISKA_MODEL": now.model}
        started = cli.write_config({**defaults, **changes})
        return {"changed": sorted(changes), "started": [s.strip() for s in started]}
    if changes:
        control.write_env(changes)
    return {"changed": sorted(changes)}


def field(key: str, env: dict[str, str]) -> dict:
    f = spec(key)
    out = {k: v for k, v in f.items() if k in ("key", "label", "kind", "help", "choices", "default")}
    out["key"] = key
    if f["kind"] == "secret":
        out["set"] = bool(env.get(key))
    else:
        out["value"] = env.get(key, f.get("default", "") if key not in env else "")
    return out


# ---- the steps -----------------------------------------------------------------------------------------------------
def step(sid: str, title: str, adds: str, state: str, detail: str = "", *, fields=(), how=(), commands=(),
         actions=(), required: bool = False, restart=()) -> dict:
    """state: done · todo (needed and missing) · off (optional, not on) · problem (on, but not working)."""
    return {"id": sid, "title": title, "adds": adds, "state": state, "detail": detail, "fields": list(fields),
            "how": list(how), "commands": list(commands), "actions": [{"id": a, "label": label} for a, label in actions],
            "required": required, "restart": list(restart)}


async def _get(url: str) -> httpx.Response | None:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            return await client.get(url)
    except httpx.HTTPError:
        return None


async def _unit(name: str) -> dict:
    return await control.unit_props(name)


async def model_step(s: Settings, env: dict) -> dict:
    fields = [field(k, env) for k in ("FRANZISKA_BASE_URL", "FRANZISKA_MODEL")]
    vram = max((g.total_mb for g in cli.gpus()), default=0)
    t = cli.tier(vram)
    how = [f"For this computer ({f'{vram / 1024:.0f} GB of GPU memory' if vram else 'no NVIDIA GPU found'}): "
           f"{t.label}.", t.note,
           "With llama.cpp, put its URL above (http://localhost:8080/v1); empty uses Ollama with the model name."]
    if s.base_url:
        r = await _get(s.server_url + "/health")
        ok = r is not None and r.status_code == 200
        detail = f"llama-server at {s.server_url}" if ok else f"Nothing answers at {s.server_url}."
    else:
        r = await _get(s.ollama_url + "/api/tags")
        names = {m.get("name", "") for m in r.json().get("models", [])} if r is not None and r.status_code == 200 else set()
        ok = s.model in names or f"{s.model}:latest" in names
        detail = (f"Ollama with {s.model}" if ok else f"Ollama runs, but {s.model} isn't pulled: ollama pull {s.model}"
                  if r is not None else f"No Ollama at {s.ollama_url}.")
    return step("model", "Language model", "Everything: it reads your messages and runs the assistants, on this "
                "machine.", "done" if ok else "todo", detail, fields=fields, how=how, required=True,
                restart=("bot", "web"))


def you_step(env: dict) -> dict:
    keys = ("FRANZISKA_TIMEZONE", "FRANZISKA_OWNER_NAME", "FRANZISKA_BRIEF_AT", "FRANZISKA_EVENING_AT")
    fields = [field(k, env) for k in keys]
    if not configured():
        fields[0]["value"] = cli.local_timezone()
    return step("you", "About you", "Dates in your time zone, the brief and the evening check-in when you want them.",
                "done" if configured() else "todo", "" if configured() else "Save once to start your configuration "
                f"({cli.ENV}).", fields=fields, required=True, restart=("bot", "web"))


async def telegram_step(s: Settings, env: dict) -> dict:
    fields = [field(k, env) for k in ("FRANZISKA_TELEGRAM_TOKEN", "FRANZISKA_ALLOWED_IDS")]
    how = ["In Telegram, open @BotFather, send /newbot, pick a name. It answers with a token: paste it above and save.",
           "Send any message to your new bot, then press “Find my id” and pick yourself.",
           "Then start the bot (Start at login, below). Telegram is how reminders, the brief and pushed emails reach "
           "you: without it they don't."]
    actions = [("telegram_check", "Check the token"), ("telegram_find", "Find my id")]
    if not s.telegram_token:
        return step("telegram", "Telegram", "Talk to it from your phone; reminders with ✅ Done / +1h buttons, the "
                    "morning brief, voice notes.", "off", "Not set up: the web app works on its own.",
                    fields=fields, how=how, actions=actions[:1], restart=("bot",))
    if not s.allowed_ids:
        return step("telegram", "Telegram", "Talk to it from your phone.", "problem",
                    "The token is saved, but nobody is allowed yet: the bot answers no one.", fields=fields, how=how,
                    actions=actions, restart=("bot",))
    bot = await _unit("franziska-bot.service")
    running = bot.get("ActiveState") == "active"
    return step("telegram", "Telegram", "Talk to it from your phone; reminders, the brief, voice notes.",
                "done" if running else "problem",
                f"{len(s.allowed_ids)} allowed user(s)" + ("" if running else ": the bot isn't running (Start at "
                                                           "login, below, or restart it in Settings)"),
                fields=fields, how=how, actions=actions, restart=("bot",))


async def services_step(s: Settings) -> dict:
    props = {u: await _unit(u) for u in UNITS}
    loaded = [u for u, p in props.items() if p.get("LoadState") == "loaded"]
    wanted = [u for u in UNITS if u != "franziska-bot.service" or s.telegram_token]
    missing = [u for u in wanted if u not in loaded]
    how = ["Installs the services in ~/.config/systemd/user for this folder and starts them: they come back after a "
           "reboot or a crash, and the Settings page can restart them.",
           "If you started this page from a terminal, close that one (Ctrl+C) afterwards: the service takes over "
           "within 10 seconds and this page reconnects.",
           "To keep them running while you are logged out: loginctl enable-linger"]
    if not shutil.which("systemctl"):
        return step("services", "Start at login", "Franziska keeps running without a terminal open.", "off",
                    "systemd isn't available here: run the bot and the web app yourself.")
    return step("services", "Start at login", "Franziska keeps running without a terminal open; reminders fire on "
                "time.", "done" if not missing else "off",
                "Installed: " + ", ".join(u.removesuffix(".service") for u in loaded) if not missing else
                "Not installed: " + ", ".join(u.removesuffix(".service") for u in missing),
                how=how, actions=[("install_services", "Install and start")] if missing else ())


def sandbox_step(s: Settings) -> dict:
    from franziska.sandbox import Sandbox

    problem = Sandbox.from_settings(s).problem()
    commands = [] if shutil.which("bwrap") else ["sudo apt install bubblewrap"]
    return step("sandbox", "Code sandbox", "Calculations over your exported data and coding jobs, in a box with no "
                "network, no home folder and no secrets.", "done" if problem is None else "off",
                problem or "run_python and coding jobs are available", commands=commands,
                how=["Creates a separate Python for the sandbox (about a minute, downloads its packages)."],
                actions=[("sandbox_install", "Install the sandbox")] if problem and shutil.which("bwrap") else (),
                restart=("bot", "web"))


async def documents_step(s: Settings, env: dict) -> dict:
    missing = [(t, pkg) for t, pkg in (("pdftotext", "poppler-utils"), ("tesseract", "tesseract-ocr tesseract-ocr-ita"))
               if not shutil.which(t)]
    embed_ok = bool(s.embed_url) and (r := await _get(s.embed_url.rstrip("/") + "/health")) is not None \
        and r.status_code < 500
    state = "done" if not missing and embed_ok else "off"
    detail = "; ".join([f"missing {t}" for t, _ in missing] +
                       (["searched by words only (no embedding server)"] if not embed_ok else []))
    return step("documents", "Reading documents", "PDFs and photos of letters read and kept, searchable by words "
                "and by meaning.", state, detail or "PDF text, OCR and search by meaning are on",
                fields=[field("FRANZISKA_EMBED_URL", env)],
                commands=([f"sudo apt install {' '.join(p for _, p in missing)}"] if missing else []) +
                ([] if embed_ok else ["cp deploy/franziska-embed.service ~/.config/systemd/user/ && "
                                      "systemctl --user daemon-reload && systemctl --user enable --now franziska-embed"]),
                how=["The embedding server runs on the CPU (deploy/franziska-embed.service says which model file); "
                     "then put http://127.0.0.1:8081 above."], restart=("bot", "web"))


async def search_step(s: Settings, env: dict) -> dict:
    ok = bool(s.searxng_url) and (r := await _get(s.searxng_url.rstrip("/") + "/healthz")) is not None \
        and r.status_code < 500
    return step("search", "Better web search", "Your own SearXNG: several search engines at once instead of one.",
                "done" if ok else "problem" if s.searxng_url else "off",
                f"SearXNG at {s.searxng_url}" if ok else f"Nothing answers at {s.searxng_url}" if s.searxng_url
                else "Web search works through DuckDuckGo meanwhile.",
                fields=[field("FRANZISKA_SEARXNG_URL", env)], commands=["cd deploy/searxng && docker compose up -d"],
                how=["Needs Docker. Then put http://127.0.0.1:8888 above."], restart=("bot", "web"))


def voice_step(s: Settings, env: dict) -> dict:
    on = bool(s.voice_model and s.telegram_token)
    return step("voice", "Voice notes", "Telegram voice messages, transcribed on this computer's CPU.",
                "done" if on else "off", f"Whisper {s.voice_model}" if on else
                "Comes with Telegram" if s.voice_model else "Off",
                fields=[field("FRANZISKA_VOICE_MODEL", env)], restart=("bot",))


def backups_step(s: Settings, env: dict) -> dict:
    ok = bool(s.backup_dir) and s.backup_dir.expanduser().is_dir()
    return step("backups", "Backups elsewhere", "A nightly copy of your tasks, money and documents on another disk.",
                "done" if ok else "problem" if s.backup_dir else "off",
                str(s.backup_dir) if ok else f"{s.backup_dir} doesn't exist" if s.backup_dir else
                f"Backups go to the same disk ({s.data_dir / 'backups'}).",
                fields=[field("FRANZISKA_BACKUP_DIR", env)], restart=("bot",))


def google_step(s: Settings, env: dict) -> dict:
    client, signed = google_client(s).exists(), s.google_token.exists()
    how = ["Optional: without it appointments live in Franziska's own calendar, and everything else works.",
           "Google needs an app of your own in Google Cloud (free, about 10 minutes, once): console.cloud.google.com "
           "→ new project → enable the Google Calendar, Gmail, Drive, Docs and Sheets APIs → Google Auth Platform: "
           "External, add your address as a test user, then Publish app (in Testing, Google signs you out every 7 "
           "days) → Clients → Create client → Desktop app → download the JSON.",
           "Upload that file here, then sign in once. With gog installed (the mail assistant, below), the same "
           "sign-in sets it up too."]
    state = "done" if s.google and signed else "problem" if s.google else "off"
    who = google_account(s)
    detail = (f"Signed in{f' as {who}' if who else ''}" + ("" if s.google else ", but turned off above")
              if signed else "Turned on, but not signed in yet" if s.google else "Off")
    actions = [("google_signin", "Sign in with Google" if not signed else "Sign in again")] if client else []
    out = step("google", "Google Calendar and Gmail", "Appointments in your Google Calendar (your phone shows them), "
               "new mail triaged and pushed to the chat, bills found in your inbox.", state, detail,
               fields=[field("FRANZISKA_GOOGLE", env)], how=how, actions=actions, restart=("bot", "web"))
    out["upload"] = {"id": "google_client", "accept": ".json,application/json", "saved": client,
                     "label": "Replace the app's JSON" if client else "Upload the app's JSON"}
    return out


def mail_step(s: Settings, env: dict) -> dict:
    gog = _gog(s)
    state = "off" if not s.gog_account else "done" if gog else "problem"
    how = ([] if gog else ["Install gogcli (github.com/openclaw/gogcli) so `gog` is on the PATH."]) + [
        "Then sign in with Google above: the same sign-in gives gog your Google app and access, with no second "
        "sign-in."]
    return step("mail", "Mail assistant", "Ask about your email: find a message, summarise a thread, draft a reply "
                "(it never sends), Drive, Docs and Sheets.", state,
                (f"For {s.gog_account}" if gog else "gog isn't on the PATH") if s.gog_account else "Off",
                fields=[field("FRANZISKA_GOG_ACCOUNT", env)], how=how, restart=("bot", "web"))


async def overview() -> dict:
    s, env = saved(), control.read_env() if configured() else {}
    steps = [await model_step(s, env), you_step(env), await telegram_step(s, env), await services_step(s),
             sandbox_step(s), await documents_step(s, env), await search_step(s, env), voice_step(s, env),
             backups_step(s, env), google_step(s, env), mail_step(s, env)]
    ready = all(x["state"] == "done" for x in steps if x["required"])
    works = (["Chat, tasks, notes, appointments, money and documents in this web app", "Web search"]
             if ready else [])
    for x, say in (("telegram", "Telegram, reminders and the brief"), ("google", "Google Calendar and Gmail"),
                   ("mail", "The mail assistant"), ("sandbox", "Calculations and coding jobs")):
        if next(st for st in steps if st["id"] == x)["state"] == "done":
            works.append(say)
    return {"configured": configured(), "config": str(cli.ENV), "ready": ready, "works": works, "steps": steps}


# ---- actions -------------------------------------------------------------------------------------------------------
async def telegram_check() -> dict:
    token = saved().telegram_token
    if not token:
        raise ValueError("Save a bot token first.")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{TELEGRAM}/bot{token}/getMe")
    if r.status_code != 200:
        raise ValueError("Telegram doesn't accept this token: copy it again from @BotFather.")
    me = r.json()["result"]
    return {"say": f"The token works: @{me['username']}.", "link": f"https://t.me/{me['username']}"}


async def telegram_find() -> dict:
    """Who wrote to the bot lately (Telegram keeps a day of updates; reading them doesn't consume them)."""
    token = saved().telegram_token
    if not token:
        raise ValueError("Save a bot token first.")
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{TELEGRAM}/bot{token}/getUpdates", params={"limit": 50})
    if r.status_code == 409:  # the bot is polling: it gets the updates, not us
        raise ValueError("The bot is running, so it reads the messages itself: send it /whoami and it answers "
                         "with your id.")
    if r.status_code != 200:
        raise ValueError("Telegram doesn't accept this token: copy it again from @BotFather.")
    people = {}
    for u in r.json().get("result", []):
        sender = (u.get("message") or u.get("edited_message") or {}).get("from")
        if sender and not sender.get("is_bot"):
            name = " ".join(filter(None, (sender.get("first_name"), sender.get("last_name")))) or "?"
            people[sender["id"]] = name + (f" (@{sender['username']})" if sender.get("username") else "")
    if not people:
        raise ValueError("No messages yet: send your bot any message in Telegram, then press this again.")
    return {"people": [{"id": i, "name": n} for i, n in people.items()]}


async def sandbox_install() -> dict:
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("FRANZISKA_")}, **control.read_env()}
    uv = shutil.which("uv") or "uv"
    proc = await asyncio.create_subprocess_exec(uv, "run", "python", "-m", "franziska.sandbox", "bootstrap", cwd=ROOT,
                                                env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 600)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ValueError("The sandbox install took over 10 minutes and was stopped.") from None
    tail = out.decode(errors="replace").strip().splitlines()[-3:]
    if proc.returncode:
        raise ValueError("The sandbox install failed: " + " / ".join(tail))
    return {"say": "The sandbox is installed. Restart the bot and the web app to use it.", "restart": ["bot", "web"]}


def unit_text(name: str) -> str:
    """deploy/<name> for this folder and this uv: the shipped units assume ~/franziska and ~/.local/bin/uv."""
    text = (ROOT / "deploy" / name).read_text()
    home = Path.home()
    here = f"%h/{ROOT.relative_to(home)}" if ROOT.is_relative_to(home) else str(ROOT)
    text = re.sub(r"^WorkingDirectory=.*$", f"WorkingDirectory={here}", text, flags=re.M)
    uv = shutil.which("uv")
    if uv and Path(uv) != home / ".local" / "bin" / "uv":
        text = text.replace("%h/.local/bin/uv", uv)
    return text


async def install_services() -> dict:
    """Write the missing units (an installed one is never overwritten), enable and start them."""
    s = saved()
    if not configured():
        raise ValueError("Save the steps above first: the services read your configuration.")
    control.UNIT_DIR.mkdir(parents=True, exist_ok=True)
    started = []
    for name in UNITS:
        if name == "franziska-bot.service" and not s.telegram_token:
            continue
        path = control.UNIT_DIR / name
        if not path.exists():
            path.write_text(unit_text(name))
        started.append(name)
    await control.systemctl("daemon-reload")
    for name in started:
        code, out = await control.systemctl("enable", "--now", "--no-block", name)
        if code:
            raise ValueError(f"{name}: {out}")
    names = ", ".join(n.removesuffix(".service") for n in started)
    return {"say": f"Started {names}. If you ran this page from a terminal, close it now: the service takes over."}


# ---- Google: one sign-in for Franziska and the mail assistant (gog) -------------------------------------------------
# The OAuth client is the user's own Desktop app (google/auth.py), uploaded here. Google sends the browser back to a
# loopback address: this page's own when it is open on this machine (the code arrives by itself, CALLBACK), else
# 127.0.0.1 on the user's device, which doesn't load; they paste that address into the page (google_finish).
CALLBACK = "/api/setup/google/callback"
SIGN_IN_S = 15 * 60
_pending: dict[str, tuple[str, str, list[str], float]] = {}  # state → (verifier, redirect, scopes, started)


def google_client(s: Settings) -> Path:
    return s.data_dir / "google_client.json"


def google_account(s: Settings) -> str:
    try:
        return json.loads(s.google_token.read_text()).get("account", "")
    except (OSError, ValueError):
        return ""


def _gog(s: Settings) -> list[str] | None:
    """The gog command (FRANZISKA_MAIL_GOG), when it is installed."""
    command = shlex.split(s.mail_gog)
    return command if command and shutil.which(command[0]) else None


def google_upload(data: bytes) -> dict:
    s = saved()
    client = google_auth.read_client(data)
    path = google_client(s)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    path.write_text(json.dumps({"installed": client}))
    return {"say": "Saved. Now sign in with Google."}


def google_signin(origin: str) -> dict:
    """The consent page's address for this page (`origin`: its scheme://host:port), asking for the mail
    assistant's access too when gog is installed."""
    s = saved()
    if not google_client(s).exists():
        raise ValueError("Upload the app's JSON first.")
    client = google_auth.read_client(google_client(s).read_bytes())
    here = urlsplit(origin)
    local = here.scheme == "http" and here.hostname in ("127.0.0.1", "localhost", "::1")
    redirect = f"{origin.rstrip('/')}{CALLBACK}" if local else f"http://127.0.0.1:{s.web_port}{CALLBACK}"
    scopes = google_auth.SCOPES + (google_auth.MAIL_SCOPES if _gog(s) else [])
    url, state, verifier = google_auth.sign_in_url(client, redirect, scopes)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v[3] > SIGN_IN_S]:
        del _pending[k]
    _pending[state] = (verifier, redirect, scopes, now)
    return {"link": url, "paste": not local,
            "say": "Sign in and allow everything Google asks."
                   + ("" if local else " The browser then ends on a page that doesn't load: copy its address from "
                                       "the address bar into the field below.")}


def google_finish(address: str) -> dict:
    """The address Google sent the browser back to (or its query): the tokens saved for Franziska, gog given the
    same app and refresh token (`gog auth import`), Google and the mail assistant turned on."""
    query = parse_qs(urlsplit(address.strip()).query or address.strip().lstrip("?"))
    if error := query.get("error", [""])[0]:
        raise ValueError(f"Google didn't sign you in ({error}). Press Sign in again.")
    code, state = query.get("code", [""])[0], query.get("state", [""])[0]
    if not code or state not in _pending:
        raise ValueError("That isn't the address of this sign-in (or it is over 15 minutes old). Press Sign in again.")
    verifier, redirect, scopes, _ = _pending.pop(state)
    s = saved()
    client = google_auth.read_client(google_client(s).read_bytes())
    body = google_auth.finish(client, code, redirect, verifier, s.google_token)
    granted = set(body.get("scope", "").split())
    who = google_auth.account(body["access_token"])
    info = json.loads(s.google_token.read_text())
    s.google_token.write_text(json.dumps({**info, "account": who}))
    say = [f"Signed in as {who}."]
    if missing := [x.rsplit("/", 1)[-1] for x in google_auth.SCOPES if x not in granted]:
        say.append(f"Not allowed: {', '.join(missing)}, so that part won't work. Sign in again to allow it.")
    changes = {"FRANZISKA_GOOGLE": "1"}
    if gog := _gog(s):
        if lacking := [x.rsplit("/", 1)[-1] for x in google_auth.MAIL_SCOPES if x not in granted]:
            say.append(f"The mail assistant stays off: not allowed {', '.join(lacking)}.")
        else:
            env = {**os.environ, **control.read_env()}  # GOG_KEYRING_* from .env, as the services see them
            # --force: signing in again replaces gog's token for this address (it refuses otherwise).
            for args, given in (([*gog, "auth", "credentials", "set", str(google_client(s)), "--no-input"], None),
                                ([*gog, "auth", "import", "--email", who, "--refresh-token-stdin", "--services",
                                  google_auth.GOG_SERVICES, "--no-input", "--force"], body["refresh_token"])):
                done = subprocess.run(args, input=given, capture_output=True, text=True, env=env, timeout=60)
                if done.returncode:
                    tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
                    raise ValueError(f"Signed in for Franziska, but gog refused the token: {tail[0]}")
            changes["FRANZISKA_GOG_ACCOUNT"] = who
            say.append("The mail assistant uses the same sign-in.")
    save(changes)
    say.append("Restart the bot and the web app to use it.")
    return {"say": " ".join(say), "restart": ["bot", "web"]}


ACTIONS = {"telegram_check": telegram_check, "telegram_find": telegram_find, "sandbox_install": sandbox_install,
           "install_services": install_services}
