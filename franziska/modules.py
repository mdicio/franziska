"""The assistants, declared in one place (ROADMAP, open source: modules). A route's description for the router, its
label, the class of data it holds and the classes it may see (privacy.py), whether it gets the document tools, the
code sandbox, a cloud model or Claude, whom it may ask, and when it is switched on: all here. The tables the rest of
the code reads (router.ROUTES, privacy.HOLDS and SEES, library.ROUTES, llm.CLOUD_OK and CLAUDE_OK, plan.ASSISTANTS,
delegation.ASK, core.LABELS) are built from BUILTIN.

A module outside this repo (a private one in an overlay, or a package of your own) is a Python module with a
`MODULE` (or a list `MODULES`) of `Module`, named in FRANZISKA_MODULES ('mypkg.garden,mypkg.health'). Core loads them
at start (`load`): each must give `build`, which returns its agent and that agent's deps for one message. What an
outside module can't do yet: join the planner (plan.Step's choices are fixed at import) or bring background jobs.

Rules checked here, not left to convention: a module holding money or mail never runs on a cloud model or on Claude,
and web sees public data only.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CLASSES = ("public", "personal", "mail", "money")  # privacy.py's data classes, least private first
LOCAL_ONLY = ("mail", "money")  # data of these classes never leaves the machine


def always(settings) -> bool:
    return True


@dataclass(frozen=True)
class Module:
    name: str  # the route's key: turns, histories, /commands and FRANZISKA_*_ROUTES use it
    label: str  # how the chat shows it ('📅 Life')
    description: str  # what the router reads to pick it: 'name: what it handles'
    holds: str  # the class of what it handles (privacy.HOLDS): the documents that come in on it, its turns
    sees: tuple[str, ...]  # the classes of turns and documents a run on it may see (privacy.SEES)
    enabled: Callable[[Any], bool] = always  # Settings → whether this installation has it
    documents: bool = False  # the library's tools (search_documents, read_document)
    receives: bool = False  # a document the user sends may be answered here (core.DOCUMENT_ROUTES)
    code: bool = False  # run_python in the sandbox (local model only)
    cloud: bool = False  # may run on the cloud model (FRANZISKA_CLOUD_ROUTES)
    claude: bool = False  # may run on Claude (FRANZISKA_CLAUDE_ROUTES)
    plannable: bool = False  # a planner step may go to it (plan.py)
    asks: tuple[str, ...] = ()  # whom its assistant may ask a question (delegation.ASK)
    relies: tuple[str, ...] = ()  # components its answers rely on: while one fails, the reply says so (health.py)
    # Outside modules: (core, text, said) → (agent, deps). The built-in ones are built by Core itself.
    build: Callable[..., tuple[Any, Any]] | None = field(default=None, compare=False)

    def check(self) -> None:
        if not self.name.isidentifier():
            raise ValueError(f"module {self.name!r}: the name must be a plain word (letters, digits, _)")
        if not self.description.startswith(f"{self.name}: "):
            raise ValueError(f"module {self.name}: the description starts with '{self.name}: '")
        if self.holds not in CLASSES or any(c not in CLASSES for c in self.sees):
            raise ValueError(f"module {self.name}: classes are {', '.join(CLASSES)}")
        if self.holds not in self.sees:
            raise ValueError(f"module {self.name}: it must see the class it holds ({self.holds})")
        if self.holds in LOCAL_ONLY and (self.cloud or self.claude):
            raise ValueError(f"module {self.name}: {self.holds} data never runs on a cloud model or Claude")


BUILTIN: tuple[Module, ...] = (
    Module("life", "📅 Life",
           "life: appointments, tasks and reminders (also 'done' or 'paid' for one of them), what is planned or "
           "what the user has to do, or saving, looking up or forgetting personal notes and facts, and what the "
           "assistant remembers about someone, and what the user's saved documents say (a contract, a policy, a "
           "manual, a letter)",
           holds="personal", sees=("public", "personal", "mail"), documents=True, receives=True, claude=True,
           plannable=True, asks=("mail",), relies=("calendar",)),
    Module("money", "💶 Money",
           "money: the user's own money: their expenses, income, bills with an amount to pay, balances, savings, "
           "the bank statement and its payees (who a payee is, its spending group), their investments and their "
           "broker portfolio",
           holds="money", sees=("public", "personal", "mail", "money"),  # mail: bills and receipts indexed from Gmail
           documents=True, receives=True, code=True, plannable=True, asks=("life", "mail")),
    Module("web", "🌐 Web",
           "web: look something up on the internet: news and current events in the world, things that change and must "
           "be checked online now (opening hours, prices, results), or anything the user asks to search for on the web",
           holds="public", sees=("public",), documents=True, cloud=True, claude=True, plannable=True),
    Module("chat", "💬 Chat",
           "chat: anything else: general knowledge and explanations (how ETFs, funds or mortgages work in general), "
           "small talk",
           holds="personal", sees=("public", "personal"), receives=True, code=True, cloud=True, claude=True),
    # Open-ended email (mail/agent.py over `gog mcp`, local model), as opposed to the mail_bills workflow.
    Module("mail", "✉️ Google",
           "mail: anything else about the user's email or Google files: find, read or summarise messages and "
           "threads, draft a reply, label, archive or clean up the inbox; find files in Google Drive, read or "
           "add to a Google Doc, read or update a Google Sheet",
           holds="mail", sees=("public", "personal", "mail"), enabled=lambda s: bool(s.gog_account), documents=True,
           plannable=True, relies=("calendar",)),
    # This computer, its files and coding jobs (pc.py). Never the cloud model; Claude when the user lists it.
    Module("pc", "🖥 PC",
           "pc: this computer itself: free disk space, memory, the GPU, running programs, Franziska's own services "
           "and their logs; finding, listing, sending, moving or deleting files in the user's folders (Downloads, "
           "Documents, Pictures); writing a script or program, or changing code in a repository",
           holds="personal", sees=("public", "personal"), enabled=lambda s: bool(s.pc), claude=True, plannable=True),
)
# Always on: the router always has these (a configuration without the others still routes).
CORE = ("life", "money", "web", "chat")

REGISTRY: dict[str, Module] = {m.name: m for m in BUILTIN}
for _m in BUILTIN:
    _m.check()


def get(name: str) -> Module | None:
    return REGISTRY.get(name)


def installed(settings) -> list[Module]:
    """The modules this installation has switched on, built-in first, in declaration order."""
    return [m for m in REGISTRY.values() if m.enabled(settings)]


def names(flag: str) -> tuple[str, ...]:
    """The modules with `flag` set ('documents', 'code', ...), whether or not they are switched on."""
    return tuple(m.name for m in REGISTRY.values() if getattr(m, flag))


def has(name: str, flag: str) -> bool:
    """Whether the module `name` exists and has `flag` set."""
    return bool((m := REGISTRY.get(name)) and getattr(m, flag))


def register(module: Module) -> None:
    module.check()
    if module.name in REGISTRY and REGISTRY[module.name] is not module:
        raise ValueError(f"module {module.name}: the name is taken")
    if module.build is None:
        raise ValueError(f"module {module.name}: an outside module gives build(core, text, said) → (agent, deps)")
    REGISTRY[module.name] = module


def load(spec: str) -> list[Module]:
    """FRANZISKA_MODULES: comma-separated import paths, each with MODULE or MODULES. Registers and returns them."""
    found = []
    for path in (p.strip() for p in spec.split(",") if p.strip()):
        mod = importlib.import_module(path)
        items = getattr(mod, "MODULES", None) or [getattr(mod, "MODULE", None)]
        if not all(isinstance(m, Module) for m in items):
            raise ValueError(f"FRANZISKA_MODULES: {path} has no MODULE or MODULES of franziska.modules.Module")
        for m in items:
            register(m)
            found.append(m)
    return found
