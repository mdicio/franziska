"""The Settings page's backend: what is configured, what is running, and the few knobs worth turning from the UI.

Two kinds of knob, both edited in place and applied by a restart you choose (nothing restarts by itself):
- **.env keys** (FIELDS): a whitelist, never secrets. The file is rewritten line by line, so comments and every other
  key stay as they are. A service is "behind" while its process environment differs from the file.
- **llama-server units** (UNITS): the GGUF, the context size and the vision projector live in the systemd unit, not in
  .env. A change is written as a drop-in (<unit>.d/franziska-ui.conf) derived from the shipped unit, so the repo's
  unit file stays the source of truth and deleting the drop-in undoes every change. "Behind" = the running command
  line differs from the configured one.
"""

import asyncio
import os
import re
import shutil
from pathlib import Path

import httpx

from franziska.config import ROOT, Settings
from franziska.llm import CLOUD_OK

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
MODELS = Path.home() / "models" / "gguf"
DROPIN = "franziska-ui.conf"
SERVICES = {  # id -> (unit, what it is)
    "llm": ("franziska-llm.service", "Local model server (llama.cpp)"),
    "embed": ("franziska-embed.service", "Embedding server (document library)"),
    "bot": ("franziska-bot.service", "Telegram bot and scheduler"),
    "web": ("franziska-web.service", "This web UI"),
}
ENV_SERVICES = ("bot", "web")  # the units that read .env

# kind: text | bool | number | select. group order is the page order. `choices` is for select, `datalist` for text.
FIELDS = [
    dict(key="FRANZISKA_BASE_URL", group="Language model", label="llama.cpp server URL", kind="text", default="",
         help="Empty = use Ollama instead. Agents, router, triage and mail all use this server."),
    dict(key="FRANZISKA_OLLAMA_BASE_URL", group="Language model", label="Ollama URL", kind="text",
         default=Settings.ollama_base_url, help="Used only while the llama.cpp URL above is empty."),
    dict(key="FRANZISKA_MODEL", group="Language model", label="Model name", kind="text", default=Settings.model,
         datalist="ollama", help="With Ollama: the model to run. With llama.cpp: a label only (the file is chosen above)."),
    dict(key="FRANZISKA_THINKING", group="Language model", label="Thinking in chat", kind="bool", default="0",
         help="Reasoning before chat replies. Slower; tools already carry the facts."),
    dict(key="FRANZISKA_ROUTER", group="Router", label="Automatic routing", kind="bool", default="1",
         help="Off: the sticky /life and /money modes decide who answers."),
    dict(key="FRANZISKA_ROUTER_MODEL", group="Router", label="Router model", kind="text", default="", datalist="ollama",
         help="Empty = the main model (no swap)."),
    dict(key="FRANZISKA_ROUTER_MIN_P", group="Router", label="Ask below confidence", kind="number", default="0.6",
         help="Under this the bot asks which assistant instead of guessing."),
    dict(key="FRANZISKA_MAIL_MODEL", group="Mail agent", label="Mail model", kind="text", default="", datalist="ollama",
         help="Empty = the main model on the same server."),
    dict(key="FRANZISKA_MAIL_BASE_URL", group="Mail agent", label="Mail server URL", kind="text", default="",
         help="Empty = the main server. An OpenAI-compatible server just for mail."),
    dict(key="FRANZISKA_MAIL_THINKING", group="Mail agent", label="Thinking", kind="bool", default="1", help=""),
    dict(key="FRANZISKA_MAIL_REASONING", group="Mail agent", label="Reasoning effort", kind="select", default="",
         choices=["", "low", "medium", "xhigh"], help="llama-server only: the chat template's reasoning_effort."),
    dict(key="FRANZISKA_CLOUD_MODEL", group="Cloud model", label="Cloud model", kind="text", default="",
         help="openrouter:<id> or deepinfra:<id>. Empty = everything stays local."),
    dict(key="FRANZISKA_CLOUD_ROUTES", group="Cloud model", label="Routes on the cloud", kind="multi", default="",
         choices=sorted(CLOUD_OK), help="Never money, mail or the schedule."),
    dict(key="FRANZISKA_CLOUD_REASONING", group="Cloud model", label="Reasoning", kind="select", default="",
         choices=["", "none", "low", "medium", "high"], help="Empty = the model's default."),
    dict(key="FRANZISKA_CLOUD_ZDR", group="Cloud model", label="Zero-retention only", kind="bool", default="1",
         help="OpenRouter privacy filter. Off lifts it: markets-only tests."),
    dict(key="FRANZISKA_EMBED_URL", group="Document library", label="Embedding server URL", kind="text", default="",
         help="Search by meaning. Empty = words only."),
    dict(key="FRANZISKA_RERANK_URL", group="Document library", label="Reranker URL", kind="text", default="",
         help="Optional; +recall but seconds a query on the CPU."),
    dict(key="FRANZISKA_VOICE_MODEL", group="Voice notes", label="Whisper model", kind="select", default=Settings.voice_model,
         choices=["", "tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"], help="Empty turns voice notes off."),
    dict(key="FRANZISKA_VOICE_DEVICE", group="Voice notes", label="Device", kind="select", default=Settings.voice_device,
         choices=["cpu", "cuda"], help="cuda only if VRAM is free next to the model server."),
    dict(key="FRANZISKA_SEARXNG_URL", group="Web search", label="SearXNG URL", kind="text", default="",
         help="Web route and brief searches. Empty = DuckDuckGo / Google News."),
    dict(key="FRANZISKA_PI_MODEL", group="Web search", label="/think model (ChatGPT)", kind="text",
         default=Settings.pi_model, help="The model /think asks through pi."),
    dict(key="FRANZISKA_WHEN_OFF", group="Desktop", label="When the desktop is off", kind="select",
         default=Settings.when_off, choices=["ask", "cloud", "wake"], help="ask: buttons · cloud: ChatGPT now · wake: wait."),
]
BY_KEY = {f["key"]: f for f in FIELDS}

# What a llama-server unit lets the page change. flag -> (label, kind). Paths are chosen from files, never typed.
UNITS = {
    "llm": dict(title="Language model file", flags={
        "-m": ("GGUF model", "gguf", lambda n: "mmproj" not in n.lower() and "embedding" not in n.lower()
               and "embed" not in n.lower() and "rerank" not in n.lower()),
        "-c": ("Context window (tokens)", "ctx", None),
        "--mmproj": ("Vision projector (VLM)", "mmproj", lambda n: "mmproj" in n.lower())}),
    "embed": dict(title="Embedding model file", flags={
        "-m": ("GGUF model", "gguf", lambda n: "embed" in n.lower()),
        # a different embedder re-embeds every document (the vectors are tagged with the model that made them)
    }),
}


# ---- .env ------------------------------------------------------------------------------------------------------------
def env_path() -> Path:
    return (ROOT / ".env").resolve()


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    path = env_path()
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)=(.*)$", line)
        if m:
            values[m[1]] = unquote(m[2].strip())
    return values


def unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def quote(value: str) -> str:
    return f'"{value}"' if re.search(r"[\s#'\"]", value) else value


def clean(field: dict, value) -> str:
    """The .env string for a submitted value; ValueError for anything the field doesn't allow."""
    if field["kind"] == "bool":
        return "1" if value in (True, "1", 1, "true") else "0"
    text = ",".join(value) if isinstance(value, list) else str(value if value is not None else "").strip()
    if "\n" in text or "\r" in text or "\0" in text:
        raise ValueError(f"{field['label']}: one line only")
    if field["kind"] == "number" and text:
        float(text)
    if field["kind"] == "select" and text not in field["choices"]:
        raise ValueError(f"{field['label']}: {text!r} is not one of {field['choices']}")
    if field["kind"] == "multi" and any(p not in field["choices"] for p in filter(None, text.split(","))):
        raise ValueError(f"{field['label']}: routes must be among {field['choices']}")
    if field["kind"] == "text" and field["key"].endswith("_URL") and text and not re.match(r"https?://\S+$", text):
        raise ValueError(f"{field['label']}: expected an http(s) URL")
    return text


def write_env(changes: dict[str, str]) -> None:
    """Replace each changed key in place (a commented `# KEY=` example is taken over), keeping comments and the mode."""
    path = env_path()
    lines = path.read_text().splitlines() if path.exists() else []
    pending = dict(changes)
    active = {m[1] for line in lines if (m := re.match(r"([A-Z][A-Z0-9_]*)=", line))}
    for i, line in enumerate(lines):
        m = re.match(r"(#\s*)?([A-Z][A-Z0-9_]*)=", line)
        if not m or m[2] not in pending or (m[1] and m[2] in active):
            continue
        key = m[2]
        if m[1] and not pending[key]:
            pending.pop(key)  # an example line, and still empty: leave it alone
            continue
        lines[i] = f"{key}={quote(pending.pop(key))}"
        active.add(key)
    lines += [f"{key}={quote(value)}" for key, value in pending.items() if value or key in active]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    os.replace(tmp, path)


# ---- systemd ---------------------------------------------------------------------------------------------------------
async def systemctl(*args: str, timeout: float = 20) -> tuple[int, str]:
    if not shutil.which("systemctl"):
        return 1, "systemctl is unavailable"
    proc = await asyncio.create_subprocess_exec("systemctl", "--user", *args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "systemctl timed out"
    return proc.returncode or 0, out.decode().strip()


async def unit_props(unit: str) -> dict[str, str]:
    code, out = await systemctl("show", "-p", "LoadState,ActiveState,SubState,MainPID", unit)
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line) if not code else {}


def process_tree(pid: int) -> list[int]:
    """pid and its descendants (the /proc scan works where /proc/<pid>/task/*/children is empty)."""
    parent: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                parent[int(entry.name)] = int(Path(entry, "stat").read_text().rsplit(")", 1)[1].split()[1])
            except (OSError, IndexError, ValueError):
                pass
    out, frontier = [pid], [pid]
    while frontier:
        frontier = [p for p, pp in parent.items() if pp in frontier and p not in out]
        out += frontier
    return out


def running_env(pid: int) -> dict[str, str]:
    """FRANZISKA_* as the service's Python process holds them (uv run --env-file loads them into the child)."""
    found: dict[str, str] = {}
    for p in process_tree(pid):
        try:
            raw = Path(f"/proc/{p}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for item in raw:
            k, _, v = item.decode(errors="replace").partition("=")
            if k.startswith("FRANZISKA_"):
                found[k] = v
    return found


def running_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


async def service_state(sid: str) -> dict:
    unit, title = SERVICES[sid]
    props = await unit_props(unit)
    state = "missing" if props.get("LoadState") != "loaded" else props.get("ActiveState", "unknown")
    pid = int(props.get("MainPID") or 0)
    info = {"id": sid, "unit": unit, "title": title, "state": state}
    if state != "active" or not pid:
        return info
    if sid in ENV_SERVICES:
        file, live = read_env(), running_env(pid)
        info["behind"] = sorted(k[len("FRANZISKA_"):].lower() for k in BY_KEY
                                if file.get(k, "") != live.get(k, ""))
    elif sid in UNITS:
        want, have = unit_flags(sid), flags_of(running_cmdline(pid), UNITS[sid]["flags"])
        info["behind"] = [flag for flag in UNITS[sid]["flags"] if want.get(flag) != have.get(flag)]
    return info


# ---- llama-server units ----------------------------------------------------------------------------------------------
def unit_text(path: Path) -> str:
    return path.read_text().replace("\\\n", " ") if path.exists() else ""


def exec_line(text: str) -> str:
    found = re.findall(r"^ExecStart=(.*)$", text, re.M)  # a drop-in's first, empty one resets the shipped command
    return found[-1] if found else ""


def flags_of(command: str, flags) -> dict[str, str]:
    command = command.replace("%h", str(Path.home())).split("llama-server", 1)[-1]
    out = {}
    for flag in flags:
        m = re.search(rf"(?:^|\s){re.escape(flag)}\s+(\S+)", command)
        if m:
            out[flag] = m[1]
    return out


def configured_command(sid: str) -> str:
    """The unit's command with the drop-in applied: the drop-in's ExecStart if there is one, else the shipped one."""
    unit = UNIT_DIR / SERVICES[sid][0]
    drop = UNIT_DIR / (SERVICES[sid][0] + ".d") / DROPIN
    return exec_line(unit_text(drop)) or exec_line(unit_text(unit))


def unit_flags(sid: str) -> dict[str, str]:
    return flags_of(configured_command(sid), UNITS[sid]["flags"])


def gguf_files(pick) -> list[str]:
    if not MODELS.is_dir():
        return []
    return sorted(p.name for p in MODELS.glob("*.gguf") if pick is None or pick(p.name))


def unit_options(sid: str) -> list[dict]:
    current = unit_flags(sid)
    out = []
    for flag, (label, kind, pick) in UNITS[sid]["flags"].items():
        value = current.get(flag, "")
        item = {"flag": flag, "label": label, "kind": kind, "value": Path(value).name if kind != "ctx" else value}
        if kind != "ctx":
            item["choices"] = (["" ] if kind == "mmproj" else []) + gguf_files(pick)
        out.append(item)
    return out


def set_unit_flags(sid: str, changes: dict[str, str]) -> None:
    """Write the drop-in: the shipped command with -m / -c / --mmproj swapped. An empty --mmproj removes the flag."""
    spec = UNITS[sid]["flags"]
    unit = UNIT_DIR / SERVICES[sid][0]
    command = configured_command(sid)
    if not command:
        raise ValueError(f"{unit.name} isn't installed in {UNIT_DIR}")
    for flag, value in changes.items():
        if flag not in spec:
            raise ValueError(f"{flag} can't be changed here")
        label, kind, pick = spec[flag]
        if kind == "ctx":
            if not str(value).isdigit() or not 2048 <= int(value) <= 262144:
                raise ValueError("Context window: a number between 2048 and 262144")
            new = str(int(value))
        elif value == "" and kind == "mmproj":
            new = ""
        else:
            if value not in gguf_files(pick):
                raise ValueError(f"{label}: {value!r} isn't a file in {MODELS}")
            new = f"%h/models/gguf/{value}"
        head, sep, tail = command.partition("llama-server")  # flags only after the binary (not `sh -c`)
        pattern = rf"(?<=\s){re.escape(flag)}\s+\S+"
        if re.search(pattern, tail):
            tail = re.sub(pattern, (f"{flag} {new}" if new else ""), tail, count=1)
        elif new:
            tail = tail.replace('"', '"', 1)
            tail = re.sub(r'^("?)', rf'\1 {flag} {new}', tail, count=1)
        command = head + sep + tail
    command = re.sub(r"[ ]{2,}", " ", command)
    drop = UNIT_DIR / (SERVICES[sid][0] + ".d")
    drop.mkdir(parents=True, exist_ok=True)
    (drop / DROPIN).write_text("# Written by the web UI's Settings page; delete this file to go back to the shipped unit.\n"
                               f"[Service]\nExecStart=\nExecStart={command}\n")


# ---- what is reachable, for the overview -----------------------------------------------------------------------------
async def probe(url: str, path: str) -> dict | list | None:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(url.rstrip("/") + path)
            return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def ollama_models(settings: Settings) -> list[str]:
    data = await probe(settings.ollama_url, "/api/tags")
    return sorted(m["name"] for m in (data or {}).get("models", []))


async def overview(settings: Settings) -> dict:
    """Everything the page shows: services, the live model, each field's file value, the unit options."""
    env = read_env()
    states = await asyncio.gather(*(service_state(s) for s in SERVICES))
    props = await probe(settings.server_url, "/props") if settings.base_url else None
    embed = await probe(settings.embed_url, "/v1/models") if settings.embed_url else None
    backend = "llama.cpp" if env.get("FRANZISKA_BASE_URL") else "Ollama"
    running = {
        "backend": "llama.cpp" if settings.base_url else "Ollama",
        "url": settings.base_url or settings.ollama_base_url,
        "model": settings.model,
        "file": Path((props or {}).get("model_path", "")).name or None,
        "window": ((props or {}).get("default_generation_settings") or {}).get("n_ctx"),
        "vision": bool(((props or {}).get("modalities") or {}).get("vision")),
        "embedding": Path(((embed or {}).get("data") or [{}])[0].get("id", "")).stem or None,
        "reranker": bool(settings.rerank_url),
        "cloud": settings.cloud_model or None,
        "cloud_routes": sorted(settings.cloud_routes),
    }
    fields = [{**f, "value": env.get(f["key"], f["default"] if f["key"] not in env else "")} for f in FIELDS]
    return {"backend": backend, "running": running, "services": states, "fields": fields,
            "units": {sid: {"title": spec["title"], "unit": SERVICES[sid][0], "options": unit_options(sid)}
                      for sid, spec in UNITS.items()},
            "ollama": await ollama_models(settings) if not settings.base_url or not env.get("FRANZISKA_BASE_URL") else []}


def save(values: dict) -> dict:
    """Validate the submitted .env fields, write what changed. Returns the keys changed."""
    env = read_env()
    changes = {}
    for key, raw in values.items():
        if key not in BY_KEY:
            raise ValueError(f"{key} can't be changed here")
        new = clean(BY_KEY[key], raw)
        if new != env.get(key, ""):
            changes[key] = new
    if changes:
        write_env(changes)
    return {"changed": sorted(changes)}


async def restart(sid: str) -> dict:
    if sid not in SERVICES:
        raise ValueError(f"unknown service {sid}")
    unit = SERVICES[sid][0]
    if sid in UNITS:
        await systemctl("daemon-reload")
    # --no-block: restarting this web UI kills the request that asked for it; the page polls until it is back.
    code, out = await systemctl("--no-block", "restart", unit)
    return {"error": out or f"restart {unit} failed"} if code else {"restarting": sid}


async def apply_units(sid: str, changes: dict[str, str]) -> dict:
    set_unit_flags(sid, changes)
    await systemctl("daemon-reload")
    return {"saved": sid}


