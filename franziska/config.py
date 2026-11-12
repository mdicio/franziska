import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    model: str = "gemma4:12b"
    ollama_base_url: str = "http://localhost:11434/v1"
    # llama.cpp's llama-server for every local call (agents, router, triage, mail) instead of Ollama: one resident
    # model on the GPU (DESIGN decision 19). Empty: Ollama at ollama_base_url.
    base_url: str = ""
    thinking: bool = False  # chat replies should be quick; tools carry the facts
    timeout_s: float = 120.0
    timezone: str = "Europe/Rome"
    data_dir: Path = Path.home() / ".local" / "share" / "franziska"  # outside the repo: nothing personal in git
    telegram_token: str = ""
    allowed_ids: frozenset[int] = field(default_factory=frozenset)
    # AnyJev router: picks secretary / finance / chat per message. Off: the sticky /life and /money modes decide.
    router: bool = True
    router_model: str = ""  # default: `model`, so Ollama never swaps models between routing and answering
    router_min_p: float = 0.6  # below this the bot asks which assistant to use instead of guessing
    brief_at: str = "07:30"  # daily morning brief, local time; empty to disable
    evening_at: str = "20:00"  # evening check-in (open tasks to tick off, tomorrow); empty to disable
    # Money in the brief (expected charges, budget warnings) and a monthly summary on the 1st. Figures then reach
    # Telegram unasked; 0 keeps them to replies you ask for.
    brief_money: bool = True
    # The user's name as banks write it ("Davide Moretti"): tells transfers between their own accounts from payments
    # to or from other people (finance/links.py). Empty: only Revolut top-ups and card top-ups are recognised.
    owner_name: str = ""
    # /think: one turn on ChatGPT through pi (Sign in with ChatGPT). Sees schedule + notes over MCP, never finance.
    pi_model: str = "openai/gpt-5.5"
    pi_timeout_s: float = 300.0
    # A cloud model for some routes (llm.Models, DESIGN decision 31): 'openrouter:<id>' or 'deepinfra:<id>', key in
    # OPENROUTER_API_KEY / DEEPINFRA_API_KEY. Only routes in llm.CLOUD_OK may be listed (never money, mail or the
    # schedule); empty: everything runs on the local model. Reasoning: '' (the model's default), 'none' … 'high'.
    cloud_model: str = ""
    cloud_routes: frozenset[str] = frozenset()
    cloud_reasoning: str = ""
    # OpenRouter privacy filter (zero retention, no training). "0" lifts it, for a :free model on a markets-only test.
    cloud_zdr: bool = True
    # Claude on the user's subscription, through Claude Code's own login (franziska/claude.py, `claude` → /login).
    # Model: an alias ('sonnet', 'opus', 'haiku') or a model id; empty: Claude is off. Routes: those in llm.CLAUDE_OK
    # (never money, mail or a workflow), or 'all' for every one of them. A failed call is answered by the local model.
    claude_model: str = ""
    claude_routes: frozenset[str] = frozenset()
    claude_effort: str = ""  # '' = Claude Code's default; low | medium | high | max
    claude_timeout_s: float = 300.0  # one model step (a reply or a tool call), not the whole turn
    # Who sees what (privacy.py): entries that replace privacy.SEES, e.g. 'chat=public,personal,money'. Empty: the
    # defaults there.
    sees: str = ""
    # Evals on synthetic data only (evals/week.py --cloud): every route on the cloud model. Never read from the env.
    cloud_everywhere: bool = False
    # Always-on box (deploy/BOX.md): Ollama runs on the desktop, which can be woken over the LAN.
    router_vocab: str = ""  # tensor-free GGUF from franziska.export_vocab; empty: read the blob Ollama serves (local)
    desktop_mac: str = ""  # empty: no Wake-on-LAN (Ollama is local, or always on)
    desktop_broadcast: str = "255.255.255.255"
    wake_timeout_s: float = 180.0
    when_off: str = "ask"  # desktop down: 'ask' (buttons), 'cloud' (ChatGPT now, wake in background), 'wake'
    # Google: appointments live in Google Calendar (read/write); Gmail is read-only and triaged on the local model.
    google: bool = False
    google_calendar_id: str = "primary"
    mail_every_min: int = 10  # Gmail fetch interval; 0 disables mail
    # Open-ended email (franziska/mail/agent.py): a local agent over `gog mcp` (search, read, drafts, labels; never
    # sends). The Gmail account signed in with `gog auth add`; empty disables it.
    gog_account: str = ""
    mail_gog: str = "gog"  # the gog command; evals: 'python -m evals.fake_gog'
    mail_model: str = ""  # default: `model` on the same server
    mail_base_url: str = ""  # default: base_url; an OpenAI-compatible server just for mail, e.g. http://localhost:8091/v1
    mail_thinking: bool = True  # open-ended work gains from reasoning; quick turns don't need it
    mail_reasoning: str = ""  # llama-server only: the template's reasoning_effort (Qwen3.8: low/medium/xhigh; empty = xhigh)
    mail_timeout_s: float = 300.0
    # Web UI (franziska/web): served by the box, reached over Tailscale (`tailscale serve` gives it HTTPS, which an
    # installable app needs). Without a token it answers only this machine's browser.
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_token: str = ""
    # The market report (franziska/market): a PDF on weekdays at this time, the weekly edition on Mondays. Empty
    # disables the push; /markets still works. Its language; SearXNG instead of Google News search feeds.
    market_at: str = "07:30"
    market_lang: str = "en"
    market_mail: bool = True  # mail every market report PDF to the Google account itself (needs the gmail.send scope)
    searxng_url: str = ""
    # Voice notes (franziska/voice.py): a faster-whisper model, on the CPU so it never competes with the resident
    # llama-server for VRAM. Empty disables them.
    voice_model: str = "large-v3-turbo"
    voice_device: str = "cpu"
    # Nightly backups (franziska/backup.py): every SQLite store and the memory files, one dated folder a night.
    # Default: inside data_dir, i.e. on the same disk; point it at another disk or a synced folder.
    backup_dir: Path | None = None
    backup_keep: int = 14  # nightly folders kept
    # The document library (franziska/library.py): a CPU llama-server with an embedding model for search by meaning
    # (deploy/franziska-embed.service), and optionally one with a reranker. Empty: the library searches by words only.
    embed_url: str = ""
    rerank_url: str = ""
    # Code/data sandbox (franziska/sandbox.py): a dedicated, minimal Python venv.  It is mounted read-only in bwrap;
    # jobs get only copied inputs and a writable scratch directory, never this application's venv or $HOME.
    sandbox_venv: Path | None = None
    sandbox_timeout_s: int = 120
    # Coding jobs (franziska/code.py, DESIGN §12 Level 3): the repositories a job may work on, name → path
    # ('franziska=~/src/franziska'); a 'scratch' job (an empty repository) is always possible. A job's
    # wall clock, its step cap, and whether its model reasons before each step.
    code_repos: dict[str, Path] = field(default_factory=dict)
    code_minutes: int = 30
    code_steps: int = 60
    code_thinking: bool = True
    # The pc route (franziska/pc.py): questions about this computer and a few actions on it, each a button. The
    # folders its file tools may see (default: the XDG user folders that exist); empty pc=0 turns the route off.
    pc: bool = True
    # Assistants from outside this repo (franziska/modules.py): comma-separated import paths, each with a MODULE.
    modules: str = ""
    pc_folders: tuple[Path, ...] = ()
    # Traces (franziska/obs.py) also go to this OTLP/HTTP endpoint, e.g. a local Phoenix: http://localhost:6006/v1/traces
    otlp: str = ""

    @property
    def google_token(self) -> Path:
        return self.data_dir / "google_token.json"

    @property
    def memory_dir(self) -> Path:
        """What the agents learned: data/memory/<scope>/<name>.md (franziska/memory.py)."""
        return self.data_dir / "memory"

    @property
    def turns_db(self) -> Path:
        """One row per answered message (franziska/turns.py): never opened by the MCP server or the cloud path."""
        return self.data_dir / "turns.sqlite"

    @property
    def traces_db(self) -> Path:
        """Every run's spans (franziska/obs.py), with message content: like the turn log, local only; not backed up."""
        return self.data_dir / "traces.sqlite"

    @property
    def usage_db(self) -> Path:
        """Tokens and cost of every cloud model request (franziska/usage.py): no message text."""
        return self.data_dir / "usage.sqlite"

    @property
    def market_db(self) -> Path:
        """Public market data only: closes, articles, the model's reports (franziska/market/store.py)."""
        return self.data_dir / "market.sqlite"

    @property
    def library_db(self) -> Path:
        """Every document the user sent or an agent read, with its pages, passages and vectors (franziska/library.py)."""
        return self.data_dir / "library.sqlite"

    @property
    def doc_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "docs"

    @property
    def sandbox_jobs_dir(self) -> Path:
        return self.data_dir / "sandbox" / "jobs"

    @property
    def sandbox_results_dir(self) -> Path:
        """Artifacts copied from a completed sandbox job; the web and bot adapters send only these paths."""
        return self.data_dir / "sandbox" / "results"

    @property
    def sandbox_venv_dir(self) -> Path:
        """The dedicated interpreter visible to code jobs; unlike the app venv it contains no app source."""
        return self.sandbox_venv or self.data_dir / "sandbox-venv"

    @property
    def code_dir(self) -> Path:
        """Coding jobs: code.sqlite and one folder per job (its clone while it runs, then its bundle and diff)."""
        return self.data_dir / "code"

    @property
    def reports_dir(self) -> Path:
        """The market report PDFs (they include the portfolio overlay: as private as the ledger)."""
        return self.data_dir / "reports"

    @property
    def ledger_db(self) -> Path:
        return self.data_dir / "ledger.sqlite"

    @property
    def life_db(self) -> Path:
        return self.data_dir / "life.sqlite"

    @property
    def ollama_url(self) -> str:
        """Ollama's native API root (the router needs /api/generate, not the OpenAI-compatible /v1)."""
        return self.ollama_base_url.removesuffix("/").removesuffix("/v1")

    @property
    def server_url(self) -> str:
        """The local model server's root: llama-server's (/completion, /health) or Ollama's (/api/...)."""
        return self.base_url.removesuffix("/").removesuffix("/v1") if self.base_url else self.ollama_url

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """From the process environment, or from `environ` (the web UI's Setup page reads the saved .env this way)."""
        source = os.environ if environ is None else environ
        ids = source.get("FRANZISKA_ALLOWED_IDS", "")
        env = source.get
        return cls(
            model=env("FRANZISKA_MODEL", cls.model),
            ollama_base_url=env("FRANZISKA_OLLAMA_BASE_URL", cls.ollama_base_url),
            base_url=env("FRANZISKA_BASE_URL", ""),
            thinking=env("FRANZISKA_THINKING", "0") != "0",
            timeout_s=float(env("FRANZISKA_TIMEOUT_S", cls.timeout_s)),
            timezone=env("FRANZISKA_TIMEZONE", cls.timezone),
            data_dir=Path(env("FRANZISKA_DATA_DIR", cls.data_dir)),
            telegram_token=env("FRANZISKA_TELEGRAM_TOKEN", ""),
            allowed_ids=frozenset(int(i) for i in ids.split(",") if i.strip()),
            router=env("FRANZISKA_ROUTER", "1") != "0",
            router_model=env("FRANZISKA_ROUTER_MODEL", ""),
            router_min_p=float(env("FRANZISKA_ROUTER_MIN_P", cls.router_min_p)),
            brief_at=env("FRANZISKA_BRIEF_AT", cls.brief_at),
            evening_at=env("FRANZISKA_EVENING_AT", cls.evening_at),
            brief_money=env("FRANZISKA_BRIEF_MONEY", "1") != "0",
            owner_name=env("FRANZISKA_OWNER_NAME", ""),
            pi_model=env("FRANZISKA_PI_MODEL", cls.pi_model),
            pi_timeout_s=float(env("FRANZISKA_PI_TIMEOUT_S", cls.pi_timeout_s)),
            cloud_model=env("FRANZISKA_CLOUD_MODEL", ""),
            cloud_routes=frozenset(r.strip() for r in env("FRANZISKA_CLOUD_ROUTES", "").split(",") if r.strip()),
            cloud_reasoning=env("FRANZISKA_CLOUD_REASONING", ""),
            cloud_zdr=env("FRANZISKA_CLOUD_ZDR", "1") != "0",
            claude_model=env("FRANZISKA_CLAUDE_MODEL", ""),
            claude_routes=frozenset(r.strip() for r in env("FRANZISKA_CLAUDE_ROUTES", "").split(",") if r.strip()),
            claude_effort=env("FRANZISKA_CLAUDE_EFFORT", ""),
            claude_timeout_s=float(env("FRANZISKA_CLAUDE_TIMEOUT_S", cls.claude_timeout_s)),
            sees=env("FRANZISKA_SEES", ""),
            router_vocab=env("FRANZISKA_ROUTER_VOCAB", ""),
            desktop_mac=env("FRANZISKA_DESKTOP_MAC", ""),
            desktop_broadcast=env("FRANZISKA_DESKTOP_BROADCAST", cls.desktop_broadcast),
            wake_timeout_s=float(env("FRANZISKA_WAKE_TIMEOUT_S", cls.wake_timeout_s)),
            when_off=env("FRANZISKA_WHEN_OFF", cls.when_off),
            google=env("FRANZISKA_GOOGLE", "0") != "0",
            google_calendar_id=env("FRANZISKA_GOOGLE_CALENDAR", cls.google_calendar_id),
            mail_every_min=int(env("FRANZISKA_MAIL_EVERY_MIN", cls.mail_every_min)),
            gog_account=env("FRANZISKA_GOG_ACCOUNT", ""),
            mail_gog=env("FRANZISKA_MAIL_GOG", cls.mail_gog),
            mail_model=env("FRANZISKA_MAIL_MODEL", ""),
            mail_base_url=env("FRANZISKA_MAIL_BASE_URL", ""),
            mail_thinking=env("FRANZISKA_MAIL_THINKING", "1") != "0",
            mail_reasoning=env("FRANZISKA_MAIL_REASONING", ""),
            mail_timeout_s=float(env("FRANZISKA_MAIL_TIMEOUT_S", cls.mail_timeout_s)),
            web_host=env("FRANZISKA_WEB_HOST", cls.web_host),
            web_port=int(env("FRANZISKA_WEB_PORT", cls.web_port)),
            web_token=env("FRANZISKA_WEB_TOKEN", ""),
            market_at=env("FRANZISKA_MARKET_AT", cls.market_at),
            market_lang=env("FRANZISKA_MARKET_LANG", cls.market_lang),
            market_mail=env("FRANZISKA_MARKET_MAIL", "1") not in ("0", "false", "no", ""),
            searxng_url=env("FRANZISKA_SEARXNG_URL", ""),
            voice_model=env("FRANZISKA_VOICE_MODEL", cls.voice_model),
            voice_device=env("FRANZISKA_VOICE_DEVICE", cls.voice_device),
            backup_dir=Path(env("FRANZISKA_BACKUP_DIR")) if env("FRANZISKA_BACKUP_DIR") else None,
            backup_keep=int(env("FRANZISKA_BACKUP_KEEP", cls.backup_keep)),
            embed_url=env("FRANZISKA_EMBED_URL", ""),
            rerank_url=env("FRANZISKA_RERANK_URL", ""),
            sandbox_venv=Path(env("FRANZISKA_SANDBOX_VENV")).expanduser() if env("FRANZISKA_SANDBOX_VENV") else None,
            sandbox_timeout_s=int(env("FRANZISKA_SANDBOX_TIMEOUT_S", cls.sandbox_timeout_s)),
            otlp=env("FRANZISKA_OTLP", ""),
            code_repos=_repos(env("FRANZISKA_CODE_REPOS", "")),
            code_minutes=int(env("FRANZISKA_CODE_MINUTES", cls.code_minutes)),
            code_steps=int(env("FRANZISKA_CODE_STEPS", cls.code_steps)),
            code_thinking=env("FRANZISKA_CODE_THINKING", "1") != "0",
            pc=env("FRANZISKA_PC", "1") != "0",
            modules=env("FRANZISKA_MODULES", ""),
            pc_folders=tuple(Path(f.strip()).expanduser() for f in env("FRANZISKA_PC_FOLDERS", "").split(",")
                             if f.strip()),
        )


def _repos(raw: str) -> dict[str, Path]:
    """'name=path,name=path' → {name: path}; 'scratch' is reserved for the empty repository."""
    repos: dict[str, Path] = {}
    for item in raw.split(","):
        name, _, path = item.strip().partition("=")
        if not item.strip():
            continue
        if not name.strip() or not path.strip() or name.strip() == "scratch":
            raise ValueError(f"FRANZISKA_CODE_REPOS: expected 'name=path', got {item.strip()!r}")
        repos[name.strip()] = Path(path.strip()).expanduser()
    return repos
