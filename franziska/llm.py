"""Model construction shared by every agent: llama-server when `base_url` is set, else Ollama.

`Models` picks the model per route: the cloud model (OpenRouter or DeepInfra) on the routes of FRANZISKA_CLOUD_ROUTES,
the local one everywhere else (DESIGN decision 31). Which routes may go to the cloud at all is CLOUD_OK, here in code:
a new route is local until it is added on purpose.

Claude on the user's subscription (franziska/claude.py) is a third place a route can run: FRANZISKA_CLAUDE_ROUTES,
checked against CLAUDE_OK. It comes before the cloud model, and a run there sees personal data but never money or mail
(privacy.CLAUDE_SEES). `where` says which of the three a model is; Core's privacy decisions follow it.
"""

import logging
import os
from collections.abc import Iterable

from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.usage import UsageLimits

from franziska import gpu
from franziska.config import Settings
from franziska.usage import Metered, UsageLog

log = logging.getLogger(__name__)

# No cap on a local response (2026-11-02): a reply can use the whole context window. A runaway reply (seen at
# temperature 0 after forget_memory, 2026-10-29) is stopped by the request timeout (settings.timeout_s) instead.
MAX_TOKENS: int | None = None
# A chat turn is a few tool calls plus the reply, with room for a couple of retries.
USAGE_LIMITS = UsageLimits(request_limit=8)

# Routes whose requests carry no personal data: the market report reads public news and closes (decision 27), and a
# cloud chat or web turn gets no profile memory (core.turn). The schedule, notes, money and mail never leave this machine.
CLOUD_OK = frozenset({"chat", "web", "markets"})
# What may run on Claude (the user's own subscription, signed in on this machine): the assistants without money or mail
# and the jobs whose input is theirs. 'plan' is the planner (it reads the message, and runs on Claude only when no step
# is money or mail); 'summary' and 'learn' fold and learn a route's turns (on Claude only when that route is too);
# 'code' is a coding job's loop. Wired but refused: money, mail, the workflows, and 'describe' (a document's card:
# the document may be a bank statement or a mail attachment). Adding one here is the whole switch.
CLAUDE_OK = frozenset({"life", "chat", "web", "pc", "plan", "summary", "learn", "code"})
DEEPINFRA_URL = "https://api.deepinfra.com/v1/openai"
# OpenRouter: only zero-retention endpoints of providers that don't train on prompts, that support every parameter
# sent (tools, the market report's JSON schema), and not the 4-bit ones (a weaker copy of the same model).
OPENROUTER_PROVIDER = {"zdr": True, "data_collection": "deny", "require_parameters": True,
                       "quantizations": ["fp8", "mxfp8", "fp16", "bf16", "fp32", "unknown"]}


def build_model(settings: Settings) -> gpu.Local:
    """The local model; each request takes the GPU lock (gpu.py), so local jobs run one at a time."""
    model_settings = OpenAIChatModelSettings(temperature=0.0, timeout=settings.timeout_s)
    if MAX_TOKENS:
        model_settings["max_tokens"] = MAX_TOKENS
    if settings.base_url:
        # The chat template reads enable_thinking per request (Gemma 4, Qwen 3.x).
        model_settings["extra_body"] = {"chat_template_kwargs": {"enable_thinking": settings.thinking}}
        return gpu.Local(OpenAIChatModel(settings.model, settings=model_settings,
                                         provider=OpenAIProvider(base_url=settings.base_url, api_key="local")))
    if not settings.thinking:
        # The unified `thinking=False` is a no-op for Ollama in pydantic-ai 2.61; reasoning_effort='none' works.
        model_settings["openai_reasoning_effort"] = "none"
    return gpu.Local(OllamaModel(
        settings.model,
        provider=OllamaProvider(base_url=settings.ollama_base_url),
        settings=model_settings,
    ))


def build_cloud_model(settings: Settings) -> Model:
    """FRANZISKA_CLOUD_MODEL, 'openrouter:<id>' or 'deepinfra:<id>'. No GPU lock: cloud requests run in parallel."""
    provider, _, name = settings.cloud_model.partition(":")
    effort = settings.cloud_reasoning
    if provider == "openrouter":
        model_settings = OpenRouterModelSettings(temperature=0.0, timeout=settings.timeout_s,
                                                 openrouter_provider=(OPENROUTER_PROVIDER if settings.cloud_zdr else
                                                                      {k: v for k, v in OPENROUTER_PROVIDER.items()
                                                                       if k not in ("zdr", "data_collection")}),
                                                 openrouter_usage={"include": True})
        if effort:
            model_settings["openrouter_reasoning"] = {"effort": effort}
        provider = OpenRouterProvider(api_key=os.environ.get("OPENROUTER_API_KEY"))
        # pydantic-ai's per-family profiles say "no JSON schema output" for some families (DeepSeek), but which
        # endpoint supports it is OpenRouter's business: require_parameters only routes to those that do, and when
        # none does the request fails and the local model answers (Models' fallback).
        profile = {**(provider.model_profile(name) or {}), "supports_json_schema_output": True}
        return OpenRouterModel(name, provider=provider, profile=profile, settings=model_settings)
    if provider == "deepinfra":
        model_settings = OpenAIChatModelSettings(temperature=0.0, timeout=settings.timeout_s)
        if effort:
            model_settings["openai_reasoning_effort"] = effort
        key = os.environ.get("DEEPINFRA_API_KEY")
        if not key:
            raise ValueError("FRANZISKA_CLOUD_MODEL is a DeepInfra model but DEEPINFRA_API_KEY is not set")
        return OpenAIChatModel(name, provider=OpenAIProvider(base_url=DEEPINFRA_URL, api_key=key),
                               settings=model_settings)
    raise ValueError(f"FRANZISKA_CLOUD_MODEL={settings.cloud_model!r}: expected 'openrouter:<id>' or 'deepinfra:<id>'")


def cloud_failed(e: Exception) -> bool:
    """When a cloud request falls back to the local model: any API error (no credit, a 404 from the provider
    filter, a 503, a rate limit, a timeout). The log says why; the usage log shows the local model's name."""
    if not isinstance(e, ModelAPIError):
        return False
    log.warning("cloud model failed, the local model answers: %s", e)
    return True


def cloud_on(settings: Settings, route: str) -> bool:
    """`route` runs on the cloud model (the market report checks this before looking for the local server)."""
    return bool(settings.cloud_model) and (settings.cloud_everywhere or route in settings.cloud_routes)


def build_claude_model(settings: Settings) -> Model:
    """Claude through Claude Code on the subscription (claude.py). No GPU lock: its runs go in parallel."""
    from franziska.claude import ClaudeCodeModel

    cwd = settings.data_dir / "claude"  # an empty folder: no CLAUDE.md or project settings for it to find
    cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
    return ClaudeCodeModel(settings.claude_model, effort=settings.claude_effort or None,
                           timeout_s=settings.claude_timeout_s, cwd=cwd)


def claude_routes(settings: Settings) -> frozenset[str]:
    """FRANZISKA_CLAUDE_ROUTES, 'all' expanded; a route outside CLAUDE_OK refuses to start."""
    if not settings.claude_model:
        return frozenset()
    routes = CLAUDE_OK if "all" in settings.claude_routes else settings.claude_routes
    if refused := routes - CLAUDE_OK:
        raise ValueError(f"FRANZISKA_CLAUDE_ROUTES: {', '.join(sorted(refused))} may not run on Claude "
                         f"(allowed: {', '.join(sorted(CLAUDE_OK))})")
    return routes


class Models:
    """The model per route. A cloud or Claude route falls back to the local model when the call fails (no credit, an
    outage, a rate limit, the plan's limit), so the answer comes either way; the usage log shows which answered."""

    def __init__(self, settings: Settings, local: Model | None = None):
        self.settings = settings
        self._cloud: dict[str, Model] = {}
        self._claude_routes: dict[str, Model] = {}
        self.local = local or build_model(settings)
        if settings.cloud_model and not settings.cloud_everywhere:
            if refused := settings.cloud_routes - CLOUD_OK:
                raise ValueError(f"FRANZISKA_CLOUD_ROUTES: {', '.join(sorted(refused))} never leave this machine "
                                 f"(allowed: {', '.join(sorted(CLOUD_OK))})")
        self._base = build_cloud_model(settings) if settings.cloud_model else None
        self.claude_on = claude_routes(settings)
        self._claude = build_claude_model(settings) if self.claude_on else None
        self.usage = UsageLog(settings.usage_db) if self._base or self._claude else None

    @property
    def local(self) -> Model:
        return self._local

    @local.setter
    def local(self, model: Model) -> None:
        """A new local model (tests swap in a scripted one): the cloud routes fall back to it from now on."""
        self._local = model
        self._cloud.clear()
        if hasattr(self, "_claude_routes"):
            self._claude_routes.clear()

    @property
    def cloud_name(self) -> str:
        return self.settings.cloud_model.partition(":")[2]

    @property
    def claude_name(self) -> str:
        return self.settings.claude_model

    def for_route(self, route: str) -> Model:
        if self._claude and route in self.claude_on:
            if route not in self._claude_routes:
                self._claude_routes[route] = Metered(FallbackModel(self._claude, self.local, fallback_on=cloud_failed),
                                                     route, self.usage)
            return self._claude_routes[route]
        if not self._base or not cloud_on(self.settings, route):
            return self.local
        if route not in self._cloud:
            self._cloud[route] = Metered(FallbackModel(self._base, self.local, fallback_on=cloud_failed), route, self.usage)
        return self._cloud[route]

    def for_job(self, job: str, routes: Iterable[str] = ()) -> Model:
        """A job's model ('plan', 'summary', 'learn', 'describe', 'code'): on Claude only when the job is listed and so
        is every route whose data it reads, so a fold of money's turns never leaves for a job on Claude."""
        routes = list(routes)
        if self._claude and job in self.claude_on and all(r in self.claude_on for r in routes):
            return self.for_route(job)
        return self.local

    def where(self, model: Model) -> str:
        """'local', 'claude' or 'cloud': where a model from this object runs, which decides what its runs may see."""
        if model is self.local:
            return "local"
        return "claude" if any(model is m for m in self._claude_routes.values()) else "cloud"
