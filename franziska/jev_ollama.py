"""AnyJev backend over a running Ollama server (raw / L0). Adapted from the AnyJev lab's Ollama backend,
where it was benchmarked; keep the two in step.

One request per prompt to `/api/generate` with `raw=true` (the prompt is already rendered by
AnyJev through the GGUF's own chat template), `num_predict=1` and `top_logprobs=20`. Ollama's
logprobs are the model's distribution before sampling (temperature / top_k do not change them),
but the server returns at most the top 20 entries and no logit restriction, so a label outside
the top 20 has no observed value.

`missing` decides what happens then:
  "raise" (default) -- LabelTokenError, as VLLMBackend does: never a silently wrong distribution;
  "bound"           -- use the 20th logprob, an upper bound on the true value, and count it in
                       `stats["bounded_labels"]`. Only for measuring how much truncation matters.

The tokenizer is the GGUF's own vocabulary, loaded vocab-only through llama-cpp-python from the
same blob Ollama serves, so prompts, label ids and the model's weights all come from one file.
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Sequence

import numpy as np
from anyjev.readout import LabelTokenError

TOP_LOGPROBS_MAX = 20  # Ollama 0.40: "top_logprobs must be between 0 and 20"


def ollama_model_blob(model: str, base_url: str = "http://localhost:11434") -> str:
    """Path of the model GGUF that Ollama serves for `model` (the first FROM; later ones are projectors)."""
    out = _post(base_url + "/api/show", {"model": model}, timeout=30)
    froms = [ln.split(" ", 1)[1].strip() for ln in out.get("modelfile", "").splitlines() if ln.startswith("FROM ")]
    if not froms:
        raise ValueError(f"Ollama reports no FROM blob for {model!r}")
    return froms[0]


def _post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


class OllamaBackend:
    def __init__(self, model: str, base_url: str = "http://localhost:11434", gguf_path: str | None = None,
                 tokenizer=None, missing: str = "raise", keep_alive: str = "10m", timeout: float = 120.0):
        if missing not in ("raise", "bound"):
            raise ValueError("missing must be 'raise' or 'bound'")
        self.base_url = base_url.rstrip("/")
        self.name = f"ollama:{model}"
        self.model = model
        self.missing = missing
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.stats = {"requests": 0, "bounded_labels": 0, "labels_read": 0}
        if tokenizer is None:
            from anyjev.backends.llamacpp import GGUFTokenizer
            from llama_cpp import Llama

            self._vocab = Llama(model_path=gguf_path or ollama_model_blob(model, self.base_url),
                                vocab_only=True, verbose=False)
            tokenizer = GGUFTokenizer(self._vocab)
        self.tokenizer = tokenizer
        self._label_text: dict = {}

    def _text(self, tid: int) -> str:
        if tid not in self._label_text:
            self._label_text[tid] = self.tokenizer.decode([tid])
        return self._label_text[tid]

    def _one(self, prompt: str, ids: Sequence[int]) -> np.ndarray:
        body = {"model": self.model, "prompt": prompt, "raw": True, "stream": False, "keep_alive": self.keep_alive,
                "logprobs": True, "top_logprobs": TOP_LOGPROBS_MAX,
                "options": {"num_predict": 1, "temperature": 0.0, "seed": 0}}
        out = _post(self.base_url + "/api/generate", body, self.timeout)
        self.stats["requests"] += 1
        if "error" in out:
            raise RuntimeError(out["error"])
        top = out["logprobs"][0]["top_logprobs"]
        by_text = {}
        for e in top:  # first occurrence wins; Ollama lists them in descending order
            by_text.setdefault(e["token"], float(e["logprob"]))
        floor = min(by_text.values())
        res, missing = [], []
        for tid in ids:
            lp = by_text.get(self._text(tid))
            if lp is None:
                missing.append(tid)
                lp = floor
            res.append(lp)
        self.stats["labels_read"] += len(ids)
        if missing:
            if self.missing == "raise":
                raise LabelTokenError(
                    f"label tokens {[self._text(t) for t in missing]} are outside Ollama's top "
                    f"{TOP_LOGPROBS_MAX} logprobs (got {list(by_text)})")
            self.stats["bounded_labels"] += len(missing)
        return np.array(res, dtype=np.float64)

    def next_token_logprobs(self, prompts: Sequence[str], token_ids: Sequence[Sequence[int]]) -> list[np.ndarray]:
        if len(prompts) != len(token_ids):
            raise ValueError("prompts and token_ids differ in length")
        return [self._one(p, ids) for p, ids in zip(prompts, token_ids)]

    def unload(self) -> None:
        _post(self.base_url + "/api/generate", {"model": self.model, "keep_alive": 0}, self.timeout)
