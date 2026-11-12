"""`python -m franziska init` (a first configuration) and `python -m franziska doctor` (what works, what doesn't).

init writes ~/.config/franziska/.env from .env.example: every setting keeps its comment, the answers fill in the
values, and the repo's .env becomes a link to it (secrets stay outside the repo). It picks the local model from the
GPU it finds and asks which modules to turn on. It never overwrites an existing file without --force.

doctor checks each piece the configuration asks for: the model server, the GPU, Telegram, the data directory, the
sandbox, Google, mail, the search and embedding servers, the tools on the PATH, the services, the git hook, and the
failures health.py has recorded. One line each, ✅ / ⚠️ / ❌, with the fix.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from franziska.config import ROOT, Settings

CONFIG_DIR = Path.home() / ".config" / "franziska"
ENV = CONFIG_DIR / ".env"


# ---- hardware ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Gpu:
    name: str
    total_mb: int
    used_mb: int


def gpus() -> list[Gpu]:
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    found = []
    for line in out.strip().splitlines():
        name, total, used = (x.strip() for x in line.split(","))
        found.append(Gpu(name, int(total), int(used)))
    return found


@dataclass(frozen=True)
class Tier:
    label: str
    env: dict[str, str]
    note: str


def tier(vram_mb: int) -> Tier:
    """The local model for this much VRAM. 12 GB is what the project is measured on (docs/ARCHITECTURE.md)."""
    if vram_mb >= 11_500:
        return Tier("gemma-4-26B-A4B on llama-server (the measured setup)",
                    {"FRANZISKA_BASE_URL": "http://localhost:8080/v1", "FRANZISKA_MODEL": "gemma4-26b-a4b"},
                    "Install llama.cpp and the GGUF, then deploy/franziska-llm.service (see the file's header).")
    if vram_mb >= 7_500:
        return Tier("gemma4:12b on Ollama", {"FRANZISKA_MODEL": "gemma4:12b"},
                    "Install Ollama, then: ollama pull gemma4:12b")
    return Tier("gemma4:12b on Ollama, partly on the CPU (no GPU with 8 GB found)", {"FRANZISKA_MODEL": "gemma4:12b"},
                "Install Ollama, then: ollama pull gemma4:12b. Below 8 GB of VRAM it is slow and not measured; "
                "consider FRANZISKA_CLAUDE_MODEL for the life and chat routes (your money and mail stay local).")


def local_timezone() -> str:
    try:
        return Path("/etc/timezone").read_text().strip() or "Europe/Rome"
    except OSError:
        link = Path("/etc/localtime")
        if link.is_symlink():
            return str(link.resolve()).split("zoneinfo/")[-1]
    return "Europe/Rome"


# ---- init -------------------------------------------------------------------------------------------------------
def fill(template: str, values: dict[str, str]) -> str:
    """.env.example with `values` set: an existing 'KEY=' or commented '# KEY=' line takes the value (and loses its
    '# '), keeping the comment after it; a key the template lacks is appended."""
    lines, done = template.splitlines(), set()
    for i, line in enumerate(lines):
        m = re.match(r"^(#\s*)?([A-Z][A-Z0-9_]*)=(\S*)(\s+#.*)?$", line)
        if m and m.group(2) in values and m.group(2) not in done:
            lines[i] = f"{m.group(2)}={values[m.group(2)]}{m.group(4) or ''}"
            done.add(m.group(2))
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    return "\n".join(lines) + "\n"


def _ask(prompt: str, default: str = "", yes: bool = False) -> str:
    if yes:
        return default
    got = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return got or default


def _yn(prompt: str, default: bool, yes: bool = False) -> bool:
    if yes:
        return default
    got = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not got else got.startswith("y")


def write_config(values: dict[str, str]) -> list[str]:
    """~/.config/franziska/.env from .env.example with `values`, mode 600, linked from the repo's .env; the git hook
    on. What it did, one line each. init and the web UI's Setup page both start a configuration here."""
    done = []
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENV.write_text(fill((ROOT / ".env.example").read_text(), values))
    ENV.chmod(0o600)
    done.append(f"\nWrote {ENV}")
    link = ROOT / ".env"
    if not link.exists() and not link.is_symlink():
        link.symlink_to(ENV)
        done.append(f"Linked {link} → {ENV}")
    if (ROOT / ".git").exists():
        subprocess.run(["git", "-C", str(ROOT), "config", "core.hooksPath", ".githooks"], check=False)
        done.append("Enabled the git hooks (.githooks): no secrets or data of yours in a commit.")
    return done


def init(force: bool = False, yes: bool = False) -> int:
    if ENV.exists() and not force:
        print(f"{ENV} exists already: edit it, or rerun with --force to start over. `python -m franziska doctor` "
              "checks it.")
        return 1
    found = gpus()
    vram = max((g.total_mb for g in found), default=0)
    t = tier(vram)
    print(f"GPU: {', '.join(f'{g.name} ({g.total_mb / 1024:.0f} GB)' for g in found) or 'none found'}")
    print(f"Local model: {t.label}\n  {t.note}\n")
    values = dict(t.env)
    values["FRANZISKA_TIMEZONE"] = _ask("Time zone", local_timezone(), yes)
    token = _ask("Telegram bot token from @BotFather (empty: web UI only)", "", yes)
    if token:
        values["FRANZISKA_TELEGRAM_TOKEN"] = token
    values["FRANZISKA_BRIEF_AT"] = _ask("Morning brief at (HH:MM, empty: none)", "07:30", yes)
    values["FRANZISKA_EVENING_AT"] = _ask("Evening check-in at (HH:MM, empty: none)", "20:00", yes)
    money = _yn("Money in the brief (expected charges, budget warnings, a monthly summary)?", True, yes)
    values["FRANZISKA_BRIEF_MONEY"] = "1" if money else "0"
    market = money and _yn("A market report PDF on weekdays (public news + your portfolio, computed locally)?",
                           False, yes)
    values["FRANZISKA_MARKET_AT"] = "07:30" if market else ""
    if _yn("Google Calendar and Gmail (needs your own Google Cloud OAuth client, deploy/BOX.md)?", False, yes):
        values["FRANZISKA_GOOGLE"] = "1"
    if not _yn("The PC route (questions about this computer, its files, coding jobs)?", True, yes):
        values["FRANZISKA_PC"] = "0"
    if not _yn("Voice notes (transcribed on this machine's CPU)?", True, yes):
        values["FRANZISKA_VOICE_MODEL"] = ""
    for line in write_config(values):
        print(line)
    print("\nNext:\n"
          f"  1. {t.note}\n"
          "  2. uv run python -m franziska.sandbox bootstrap        # the isolated runtime for run_python\n"
          "  3. uv run --env-file .env python -m franziska doctor   # check everything\n"
          + ("  4. uv run --env-file .env python -m franziska.bot      # then send /whoami to the bot and put your id\n"
             "     in FRANZISKA_ALLOWED_IDS\n" if token else
             "  4. uv run --env-file .env python -m franziska.web      # http://127.0.0.1:8765\n"))
    return 0


# ---- doctor -----------------------------------------------------------------------------------------------------
OK, WARN, BAD = "✅", "⚠️ ", "❌"


def _get(url: str, timeout: float = 4.0) -> httpx.Response | None:
    try:
        return httpx.get(url, timeout=timeout)
    except httpx.HTTPError:
        return None


def checks(s: Settings) -> list[tuple[str, str, str]]:
    """(mark, what, detail or fix) for each piece the configuration uses."""
    out: list[tuple[str, str, str]] = []
    add = lambda mark, what, detail="": out.append((mark, what, detail))  # noqa: E731

    if not os.environ.get("FRANZISKA_MODEL") and not os.environ.get("FRANZISKA_BASE_URL"):
        add(WARN, "configuration", "no FRANZISKA_* settings in the environment: run with --env-file .env "
                                   "(python -m franziska init writes one)")
    # The model
    if s.base_url:
        r = _get(s.server_url + "/health")
        if r is not None and r.status_code == 200:
            props = _get(s.server_url + "/props")
            ctx = props.json().get("default_generation_settings", {}).get("n_ctx") if props is not None else None
            add(OK, "model server", f"llama-server at {s.server_url}" + (f", {ctx} tokens of context" if ctx else ""))
            if ctx and ctx < 16384:
                add(WARN, "context window", f"{ctx} tokens: agents with tools need ~16k; 32k is the measured setup")
        else:
            add(BAD, "model server", f"no llama-server at {s.server_url}: systemctl --user start franziska-llm")
    else:
        r = _get(s.ollama_url + "/api/tags")
        if r is None:
            add(BAD, "model server", f"no Ollama at {s.ollama_url}: install it, or set FRANZISKA_BASE_URL")
        else:
            names = {m.get("name", "") for m in r.json().get("models", [])}
            if s.model in names or f"{s.model}:latest" in names:
                add(OK, "model server", f"Ollama with {s.model}")
            else:
                add(BAD, "model", f"Ollama runs, but {s.model} isn't pulled: ollama pull {s.model}")
    found = gpus()
    if found:
        for g in found:
            add(OK if g.total_mb >= 11_500 else WARN, "GPU",
                f"{g.name}, {g.total_mb / 1024:.0f} GB, {g.used_mb / 1024:.1f} GB in use"
                + ("" if g.total_mb >= 11_500 else ": below the 12 GB the project is measured on"))
    elif not s.desktop_mac:
        add(WARN, "GPU", "none found (nvidia-smi): the local model will be slow")
    # Talking to it
    if s.telegram_token:
        add(OK if s.allowed_ids else BAD, "Telegram",
            f"{len(s.allowed_ids)} allowed user(s)" if s.allowed_ids else
            "FRANZISKA_ALLOWED_IDS is empty, so nobody is answered: send /whoami to the bot and add your id")
    else:
        add(WARN, "Telegram", "no FRANZISKA_TELEGRAM_TOKEN: the web UI only (python -m franziska.web)")
    if s.web_host not in ("127.0.0.1", "localhost", "::1") and not s.web_token:
        add(BAD, "web UI", f"listens on {s.web_host} without FRANZISKA_WEB_TOKEN: it refuses to start")
    # Data
    try:
        s.data_dir.mkdir(parents=True, exist_ok=True)
        probe = s.data_dir / ".doctor"
        probe.write_text("ok")
        probe.unlink()
        add(OK, "data directory", str(s.data_dir))
    except OSError as e:
        add(BAD, "data directory", f"{s.data_dir}: {e}")
    banks = sorted(p.stem for p in (s.data_dir / "banks").glob("*.json"))
    if banks:
        add(OK, "mapped banks", ", ".join(banks))
    if s.backup_dir and not s.backup_dir.exists():
        add(WARN, "backups", f"{s.backup_dir} doesn't exist")
    elif not s.backup_dir:
        add(WARN, "backups", "nightly backups go to the same disk (FRANZISKA_BACKUP_DIR: point it elsewhere)")
    # Code and documents
    from franziska.sandbox import Sandbox

    problem = Sandbox.from_settings(s).problem()
    add(OK if problem is None else WARN, "sandbox", problem or "run_python and coding jobs available")
    for tool, why in (("pdftotext", "PDF text (apt: poppler-utils)"), ("tesseract", "OCR of photos and scans"),
                      ("bwrap", "the code sandbox (apt: bubblewrap)")):
        if not shutil.which(tool):
            add(WARN, tool, f"missing: {why}")
    # Integrations the configuration asks for
    if s.google:
        add(OK if s.google_token.exists() else BAD, "Google",
            "signed in" if s.google_token.exists() else "FRANZISKA_GOOGLE=1 but no token: python -m franziska.google.auth")
    if s.gog_account:
        add(OK if shutil.which(s.mail_gog) else BAD, "mail agent",
            f"gog for {s.gog_account}" if shutil.which(s.mail_gog) else f"{s.mail_gog} not on the PATH (gogcli)")
    for what, url, path in (("embeddings", s.embed_url, "/health"), ("reranker", s.rerank_url, "/health"),
                            ("SearXNG", s.searxng_url, "/healthz")):
        if url:
            r = _get(url.rstrip("/") + path)
            add(OK if r is not None and r.status_code < 500 else BAD, what,
                url if r is not None else f"nothing at {url}")
    if not s.embed_url:
        add(WARN, "embeddings", "FRANZISKA_EMBED_URL is empty: documents are searched by words only")
    if shutil.which("pi") is None:
        add(WARN, "/think", "pi isn't on the PATH: /think (ChatGPT) is off (cd pi && npm ci)")
    # Running services
    if shutil.which("systemctl"):
        for unit in ("franziska-llm", "franziska-bot", "franziska-web"):
            state = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True).stdout
            if state.strip() not in ("active", "inactive", "unknown", ""):
                add(BAD, unit, state.strip())
            elif state.strip() == "active":
                add(OK, unit, "running")
    if (ROOT / ".git").exists():
        hooks = subprocess.run(["git", "-C", str(ROOT), "config", "core.hooksPath"], capture_output=True,
                               text=True).stdout.strip()
        if hooks != ".githooks":
            add(WARN, "git hook", "off: git config core.hooksPath .githooks (keeps your data out of commits)")
    # What failed at runtime
    if (s.data_dir / "health.sqlite").exists():
        from franziska.health import Health

        for line in Health(s.data_dir / "health.sqlite", datetime.now).problems():
            add(BAD, "runtime", line)
    return out


def doctor() -> int:
    s = Settings.from_env()
    rows = checks(s)
    width = max(len(w) for _, w, _ in rows)
    for mark, what, detail in rows:
        print(f"{mark} {what:<{width}}  {detail}")
    bad = sum(1 for m, _, _ in rows if m == BAD)
    print(f"\n{bad} problem(s)" if bad else "\nNo problems.")
    return 1 if bad else 0
