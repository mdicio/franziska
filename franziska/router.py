"""Which assistant should answer a message: one AnyJev decision read from the local model's next-token logprobs.

No text is generated or parsed. L0 reads the question once per rotation of the options (4 options → 4 short
prefills, ~0.8 s on the RTX 3060) and divides out the model's content-free label preference, so the probability is
usable as a threshold: below `min_p` the bot asks instead of guessing.

Runs on the same model as the agents: llama-server (`server_url`, jev_server) or Ollama (jev_ollama), so routing never
loads a second model. Both return only the top N logprobs (50 / 20); an option outside them gets the Nth value
(`missing="bound"`), an upper bound that can only make the decision less confident, never more. Measured on 12 assistant messages: 12/12.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from franziska import modules, obs

log = logging.getLogger(__name__)

# Short descriptions after the key measurably beat bare labels (bare: 'memory' vs 'schedule' was confused).
# The assistants every installation has; the optional ones (mail, pc, outside modules) and workflows come from Core.
ROUTES = {m.name: m.description for m in modules.BUILTIN if m.name in modules.CORE}
QUESTION = "Which assistant should handle this message?"
# A new task's priority when the user doesn't say it (tasks nag by it: high every 2 h, normal daily, low every 2 days).
PRIORITIES = {
    "high": "high: has to be done as soon as possible, today, or there will be trouble",
    "normal": "normal: should get done in the next days",
    "low": "low: no pressure, whenever there is time",
}


NO_RULE = "none of them: something else"


@dataclass(frozen=True)
class Route:
    name: str  # a key of ROUTES
    p: float
    probs: dict[str, float]


class Router:
    def __init__(self, model: str, ollama_url: str = "http://localhost:11434", min_p: float = 0.6, backend=None,
                 vocab: str = "", routes: dict[str, str] | None = None, server_url: str = ""):
        self.model, self.ollama_url, self.min_p, self.vocab = model, ollama_url, min_p, vocab
        self.server_url = server_url  # llama-server's root; empty: Ollama
        self.routes = {**ROUTES, **(routes or {})}  # extra routes: one per workflow ("name: description")
        self._backend = backend
        self._decider = None
        self.error: Exception | None = None  # why the last route() returned None

    @classmethod
    def from_settings(cls, settings, routes: dict[str, str] | None = None) -> Router:
        """The router on the agents' own model server: llama-server with FRANZISKA_BASE_URL, else Ollama."""
        return cls(settings.router_model or settings.model, settings.ollama_url, settings.router_min_p,
                   vocab=settings.router_vocab, routes=routes, server_url=settings.base_url and settings.server_url)

    def _load(self):
        from anyjev import Decider, Question

        from franziska.jev_ollama import OllamaBackend
        from franziska.jev_server import ServerBackend

        if self._backend is None:
            # The GGUF's vocabulary only (no weights): from `vocab` (a tensor-free export, on the box) or from the file
            # the server loaded (on the desktop).
            if self.server_url:
                backend = ServerBackend(self.server_url, gguf_path=self.vocab or None, missing="bound")
            else:
                backend = OllamaBackend(self.model, base_url=self.ollama_url, missing="bound", keep_alive="30m",
                                        gguf_path=self.vocab or None)
            self._backend = _Locked(backend)
        self._question = Question.choice(QUESTION, list(self.routes.values()), name="route")
        # Stops reading rotations once the route is clear: 4.5 → 1.4 s per message with 7 routes, same accuracy
        # (evals/router_eval.py, 2026-10-29).
        self._decider = Decider(self._backend, level="L0", prior="content_free", canonical_order=True,
                                adaptive_shifts=True)
        # Other questions (e.g. a merchant's category) can have many options: stop reading shifts once decided.
        self._many = Decider(self._backend, level="L0", prior="content_free", canonical_order=True,
                             adaptive_shifts=True)

    @property
    def backend(self):
        """The AnyJev backend, for other decisions on the same model (mail triage)."""
        if self._decider is None:
            self._load()
        return self._backend

    def route(self, text: str, previous: tuple[str, str] | None = None) -> Route | None:
        """The best route with its probability, or None if the router is unavailable (the caller falls back).

        `previous` is (route, user message) of the last turn, so follow-ups like 'and in August?' stay put.
        """
        state = text
        if previous:
            state = (f"Previous message (handled by {previous[0]}): {previous[1]}\n"
                     f"New message: {text}")
        with obs.span("route", previous=previous and previous[0], text=text[:200]) as s:
            try:
                if self._decider is None:
                    self._load()
                d = self._decider.decide(state, [self._question])["route"]
            except Exception as e:
                log.exception("router failed; falling back")
                obs.failed(e)
                self.error = e
                return None
            self.error = None
            keys = {v: k for k, v in self.routes.items()}
            probs = {keys[o]: p for o, p in d.distribution.items()}
            name = max(probs, key=probs.get)
            s.set_attributes(obs.clean(route=name, p=probs[name], probs={k: round(v, 3) for k, v in probs.items()}))
            return Route(name, probs[name], probs)

    def choose(self, state: str, question: str, options: list[str]) -> dict[str, float] | None:
        """Any other one-of-n question on the same model: option → probability, best first; None if unavailable."""
        from anyjev import Question

        with obs.span("decide", question=question, state=state[:200]) as s:
            try:
                if self._decider is None:
                    self._load()
                d = self._many.decide(state, [Question.choice(question, options, name="q")])["q"]
            except Exception as e:
                log.exception("choose failed")
                obs.failed(e)
                return None
            s.set_attributes(obs.clean(probs={k[:40]: round(v, 3) for k, v in d.distribution.items()}))
            return dict(sorted(d.distribution.items(), key=lambda kv: kv[1], reverse=True))

    def choose_many(self, states: list[str], question: str, options: list[str]) -> list[dict[str, float]]:
        """One one-of-n question about many states (a batch of payees): option → probability per state. Raises when
        the model is unavailable: a batch job should stop rather than record nothing as an answer."""
        from anyjev import Question

        if self._decider is None:
            self._load()
        q = Question.choice(question, options, name="q")
        with obs.span("decide batch", question=question, states=len(states)):
            return [d.distribution for d in self._many.decide_batch(states, q)]

    def decide(self, state: str, questions: list) -> dict:
        """Several AnyJev questions about one state in one call (Question.choice / Question.noul), by name.
        Raises when the model is unavailable: callers that can't do without the decision should fail loudly."""
        if self._decider is None:
            self._load()
        with obs.span("decide", questions=[q.name for q in questions], state=state[:200]) as s:
            out = self._many.decide(state, questions)
            s.set_attributes(obs.clean(probs={d.question.id: {k[:40]: round(v, 3) for k, v in d.distribution.items()}
                                              for d in out}))
            return out

    def priority(self, task: str) -> str | None:
        """'normal' / 'low' for a new task, or None if the model is unavailable. Never 'high' (a nag every 2 hours):
        the guess made errands urgent (bread, basil, parking: 12 in realmonth 2026-10-30); high is the user's word."""
        d = self.choose(f"A to-do the user just added: {task}", "How urgent is this to-do?",
                        [PRIORITIES["normal"], PRIORITIES["low"]])
        if not d:
            return None
        best = next(iter(d))
        log.info("priority %s p=%.2f", best.split(":")[0], d[best])
        return next(k for k, v in PRIORITIES.items() if v == best)

    def reminder_rules(self, tasks: list[str], rules: list[str]) -> list[int | None]:
        """For each to-do title, which of the user's reminder preferences (what each is about: 'palestra') it falls
        under: an index into `rules`, or None. One batch of AnyJev decisions; raises when the model is unavailable."""
        options = [f"about {r}" for r in rules] + [NO_RULE]
        dists = self.choose_many([f"A to-do of the user: {t}" for t in tasks],
                                 "Which of the user's standing reminder preferences is this to-do about?", options)
        return [None if (best := max(d, key=d.get)) == NO_RULE else options.index(best) for d in dists]

    def confident(self, r: Route | None) -> bool:
        return r is not None and r.p >= self.min_p


class _Locked:
    """An AnyJev backend whose requests take the GPU lock (gpu.py); everything else is the backend's own."""

    def __init__(self, backend):
        self._backend = backend

    def __getattr__(self, name):
        return getattr(self._backend, name)

    def next_token_logprobs(self, prompts, token_ids):
        from franziska import gpu

        with gpu.use("router"):
            return self._backend.next_token_logprobs(prompts, token_ids)
