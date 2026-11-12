"""Two open-source tools behind the web UI's sign-in, for digging rather than daily use (docs/WEB.md §9).

- **Datasette** (datasette.io) at /sql/: every store in the data dir with SQL, facets, filters, links between tables
  and JSON/CSV out. It runs in this process (an ASGI app mounted by create_app) and reads each store with
  `mode=ro`, like the Data view; with no signed-in Datasette actor its write API is refused.
- **Phoenix** (arize-phoenix) at /phoenix/: the LLM trace UI for the spans that FRANZISKA_OTLP sends it. It is heavy
  and only wanted while looking, so it is a systemd user unit (deploy/franziska-phoenix.service) that the Telemetry
  link starts; its UI listens on the loopback only and is proxied here, so it reaches the phone over Tailscale with
  the same cookie as everything else, and is never on the tailnet without it.
"""

import asyncio
import logging
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from datasette.app import Datasette
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger(__name__)

SQL_ROOT = "/sql"
PHOENIX_ROOT = "/phoenix"  # its PHOENIX_HOST_ROOT_PATH: the links it writes start with it, the proxy takes it off
PHOENIX_UNIT = "franziska-phoenix.service"
LLM_UNIT = "franziska-llm.service"
PHOENIX_DEFAULT = "http://127.0.0.1:6006"
PHOENIX_START_S = 120  # the first start after an upgrade has uvx install it
# Not passed through the proxy: per-connection headers, and our sign-in cookie (Phoenix has no use for it).
HOP = frozenset({"connection", "keep-alive", "proxy-connection", "transfer-encoding", "te", "trailer", "upgrade",
                 "host", "cookie"})


def datasette(data_dir: Path):
    """Datasette over the stores that exist now (a new store shows after a restart), as an ASGI app for /sql."""
    ds = Datasette(
        files=[str(p) for p in sorted(data_dir.glob("*.sqlite"))],
        settings={"base_url": SQL_ROOT + "/", "sql_time_limit_ms": 5000, "default_page_size": 50,
                  "suggest_facets": True},
        # Datasette puts its base_url in front of these: served from its own mount, /sql/franziska/…
        static_mounts=[("franziska", str(Path(__file__).parent / "static"))],
        metadata={"title": "Franziska · SQL", "extra_css_urls": ["/franziska/datasette.css"],
                  "extra_js_urls": ["/franziska/datasette.js"],
                  "description_html": "Every store in the data dir, read-only. <a href='/#data'>‹ Back to Franziska</a>"},
    )
    inner = ds.app()

    async def app(scope, receive, send):
        # Mount puts /sql in root_path, and Datasette adds its base_url on top of that: keep the prefix once.
        await inner({**scope, "root_path": ""} if scope["type"] != "lifespan" else scope, receive, send)
    return app


class Phoenix:
    """Phoenix started on demand (systemd --user) and reached through `proxy`."""

    def __init__(self, otlp: str):
        # Where the traces already go (FRANZISKA_OTLP=http://localhost:6006/v1/traces) is where its UI is.
        url = urlsplit(otlp or PHOENIX_DEFAULT)
        self.base = f"{url.scheme}://{url.netloc}"
        self.otlp = bool(otlp)
        self.client = httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(60, connect=2))
        self.starting: asyncio.Task | None = None

    async def up(self) -> bool:
        try:
            return (await self.client.get("/healthz", timeout=2)).status_code == 200
        except httpx.HTTPError:
            return False

    async def systemctl(self, *args: str) -> tuple[int, str]:
        if not shutil.which("systemctl"):
            return 1, "no systemctl on this machine"
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode().strip()


    async def status(self) -> dict:
        """state: up | starting | off | failed | missing (the unit isn't installed)."""
        if await self.up():
            state = "up"
        else:
            _, shown = await self.systemctl("show", "-p", "LoadState,ActiveState", PHOENIX_UNIT)
            props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
            active = props.get("ActiveState", "")
            state = ("missing" if props.get("LoadState") != "loaded" else "failed" if active == "failed"
                     else "starting" if active in ("active", "activating") or self.starting else "off")
        return {"state": state, "url": PHOENIX_ROOT + "/", "otlp": self.otlp, "unit": PHOENIX_UNIT}

    async def start(self) -> dict:
        """Start the unit and wait for its UI; two taps share one start."""
        if not await self.up():
            if self.starting is None or self.starting.done():
                self.starting = asyncio.create_task(self._start())
            error = await asyncio.shield(self.starting)
            if error:
                return {**await self.status(), "error": error}
        return await self.status()

    async def _start(self) -> str | None:
        code, out = await self.systemctl("start", PHOENIX_UNIT)
        if code:
            return out or f"systemctl start {PHOENIX_UNIT} failed"
        for _ in range(PHOENIX_START_S):
            if await self.up():
                log.info("phoenix is up at %s", self.base)
                return None
            await asyncio.sleep(1)
        return f"Phoenix didn't answer in {PHOENIX_START_S} s (journalctl --user -u {PHOENIX_UNIT})"

    async def stop(self) -> dict:
        code, out = await self.systemctl("stop", PHOENIX_UNIT)
        return {**await self.status(), **({"error": out} if code else {})}

    async def proxy(self, request: Request) -> Response:
        """One request to Phoenix's UI or API, streamed both ways' bodies as they are (compressed stays compressed)."""
        if not await self.up():
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                return HTMLResponse(STARTING, status_code=503)
            return JSONResponse({"detail": "Phoenix is not running"}, status_code=503)
        url = request.url.path.removeprefix(PHOENIX_ROOT) + (f"?{request.url.query}" if request.url.query else "")
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP]
        if "accept-encoding" not in request.headers:  # else httpx asks for gzip, and the raw body reaches a client that can't
            headers.append(("accept-encoding", "identity"))
        sent = self.client.build_request(request.method, url, headers=headers, content=await request.body())
        try:
            got = await self.client.send(sent, stream=True)
        except httpx.HTTPError as e:
            return JSONResponse({"detail": f"Phoenix: {type(e).__name__}"}, status_code=502)
        response = StreamingResponse(got.aiter_raw(), status_code=got.status_code, background=BackgroundTask(got.aclose))
        for k, v in got.headers.multi_items():
            if k.lower() not in HOP:
                response.headers.append(k, v)
        return response


class LocalModel:
    """Manual start/stop for the one configured llama.cpp user service."""

    async def systemctl(self, *args: str) -> tuple[int, str]:
        if not shutil.which("systemctl"):
            return 1, "systemctl is unavailable"
        proc = await asyncio.create_subprocess_exec("systemctl", "--user", *args, LLM_UNIT,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 1, "systemctl timed out"
        return proc.returncode or 0, out.decode().strip()

    async def status(self) -> dict:
        code, shown = await self.systemctl("show", "-p", "LoadState,ActiveState,SubState")
        props = dict(line.split("=", 1) for line in shown.splitlines() if "=" in line)
        if code:
            return {"state": "unknown", "error": shown}
        state = "missing" if props.get("LoadState") != "loaded" else props.get("ActiveState", "unknown")
        return {"state": state, "unit": LLM_UNIT}

    async def set_running(self, running: bool) -> dict:
        # A stop waits for llama-server to finish its request and free the GPU (up to TimeoutStopSec); don't hold
        # the page on that: queue it and report "deactivating", the Settings page polls until it's off.
        code, out = await self.systemctl("start" if running else "stop", *(() if running else ("--no-block",)))
        status = await self.status()
        if code:
            status["error"] = out
        return status


# What /phoenix/ shows while Phoenix is off: it starts it (a POST, so opening the link has no side effect of its own)
# and reloads when the UI answers.
STARTING = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Telemetry · Franziska</title>
<link rel=stylesheet href=/static/style.css>
<body class=login><div class="card starting">
<div class=brand><span class=mark>F</span> Telemetry</div>
<p id=say><span class=spin></span> Starting Phoenix, the trace viewer… this takes a few seconds.</p>
<p class=fine>Runs from now on show up there (FRANZISKA_OTLP). Older runs stay in traces.sqlite, under
<a href="/#data/traces/spans">Data › traces</a>.</p>
<p><a href="/">‹ Back to Franziska</a></p></div>
<script>
fetch("/api/telemetry/start", {method: "POST"}).then((r) => r.json()).then((s) => {
  if (s.state === "up") location.reload();
  else document.getElementById("say").textContent = s.state === "missing"
    ? "Phoenix isn't installed as a service yet: cp deploy/franziska-phoenix.service ~/.config/systemd/user/ && systemctl --user daemon-reload"
    : "Phoenix didn't start: " + (s.error || s.state);
}).catch((e) => { document.getElementById("say").textContent = "Couldn't reach the box: " + e.message; });
</script></body></html>"""
