"""Who may see what: one table for the documents (library.py), the turn log (`past_conversations`) and the chat's
thread (Core._thread). To relax or tighten a filter, change SEES here or set FRANZISKA_SEES; nothing else decides.

Every route's data has one class (HOLDS): the documents that came in on it and the turns it answered. A route sees
the classes SEES lists for it. Money figures stay with money unless the table says otherwise; mail content is seen by
life, mail and money (a bill or a receipt indexed from Gmail is a money question as much as a life one). Two limits
sit on top of the table, because there the data would leave the machine:
- a run on a cloud model sees CLOUD_SEES (nothing by default);
- a run on Claude, the user's own subscription (claude.py), sees CLAUDE_SEES: public and personal, never money or mail;
- web sees only public data: what it reads can become a search query.

FRANZISKA_SEES replaces whole entries: 'chat=public,personal,money;pc=public,personal,mail'.
"""

from collections.abc import Iterable, Mapping

from franziska import modules

CLASSES = modules.CLASSES

# The class of what a route handles (modules.py declares it per assistant; 'cloud' is the desktop-off ChatGPT turn). A
# workflow's turns get its scope's class (Core registers them); a route that is not here (the planner's own row, which
# mixes several) is seen by nobody: its steps are logged under their routes.
HOLDS: dict[str, str] = {**{m.name: m.holds for m in modules.BUILTIN}, "cloud": "personal"}

ROUTES = tuple(HOLDS)  # the assistants, before any workflow is registered (outside modules are added: add_module)

# What each route may see (modules.py; money sees mail: the bills and receipts indexed from Gmail, 2026-11-01).
SEES: dict[str, tuple[str, ...]] = {m.name: m.sees for m in modules.BUILTIN}
CLOUD_SEES: tuple[str, ...] = ()  # what a cloud model's run may see of the turn log and the thread
CLAUDE_SEES: tuple[str, ...] = ("public", "personal")  # what a run on Claude may see (never money or mail)
# The chat's turn just before (within core.FOLLOW_UP_S) is in every local run's thread whatever its class: the user's
# follow-up refers to it ('ricordami di pagarlo' after money found a charge). False: the table alone decides.
FOLLOW_UP = True


def configure(overrides: Mapping[str, Iterable[str]]) -> None:
    """Apply FRANZISKA_SEES (Settings.sees): each named route's entry is replaced. Web stays public only."""
    for route, classes in overrides.items():
        classes = tuple(c for c in classes if c in CLASSES)
        SEES[route] = ("public",) if route == "web" else classes


def add_module(module: modules.Module) -> None:
    """An outside module (modules.load): its class and what it sees, unless FRANZISKA_SEES already set them."""
    global ROUTES
    HOLDS.setdefault(module.name, module.holds)
    SEES.setdefault(module.name, ("public",) if module.name == "web" else module.sees)
    ROUTES = tuple(dict.fromkeys((*ROUTES, module.name)))


def register(route: str, like: str) -> None:
    """A workflow's turns: the class of the route it belongs to (Workflow.scope)."""
    if like in HOLDS:
        HOLDS.setdefault(route, HOLDS[like])


def sees(route: str, cloud: bool = False, claude: bool = False) -> tuple[str, ...]:
    """The classes a run on `route` may see; a cloud run only those also in CLOUD_SEES, a Claude run CLAUDE_SEES."""
    classes = SEES.get(route, ())
    if claude:
        return tuple(c for c in classes if c in CLAUDE_SEES)
    return tuple(c for c in classes if c in CLOUD_SEES) if cloud else classes


def where_sees(route: str, where: str) -> tuple[str, ...]:
    """`sees` for a run on a model that runs `where` ('local', 'claude' or 'cloud', llm.Models.where)."""
    return sees(route, cloud=where == "cloud", claude=where == "claude")


def visible(route: str, of: Iterable[str], cloud: bool = False, claude: bool = False) -> list[str]:
    """Which of the routes `of` a run on `route` may see the turns of."""
    classes = sees(route, cloud, claude)
    return [r for r in of if HOLDS.get(r) in classes]


def parse(value: str) -> dict[str, tuple[str, ...]]:
    """'chat=public,personal;pc=public' → {'chat': ('public', 'personal'), 'pc': ('public',)}."""
    out = {}
    for part in value.split(";"):
        route, _, classes = part.partition("=")
        if route.strip() and classes.strip():
            out[route.strip()] = tuple(c.strip() for c in classes.split(",") if c.strip())
    return out
