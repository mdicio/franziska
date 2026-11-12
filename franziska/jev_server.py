"""AnyJev backend over a running llama.cpp `llama-server` (raw / L0): the router's decisions on the same server as the
agents, so one model stays resident on the GPU (DESIGN decision 19).

One `/completion` request per prompt, with the prompt sent as token ids (AnyJev has already rendered it through the
GGUF's chat template; ids leave the server no BOS or special-token parsing to get wrong), `n_predict=1` and
`n_probs=TOP_N`. llama-server's probabilities are the softmax of the raw logits (`post_sampling_probs` off), so the
sampler settings don't change them, and each entry carries its token id: labels are matched by id, not by text.

Like jev_ollama, only the top TOP_N are returned; `missing` is the same choice ("raise" or "bound", see there).
The tokenizer is the GGUF's own vocabulary, loaded vocab-only from the file the server reports in /props (or
`gguf_path`: a tensor-free export from franziska.export_vocab, on the box).
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Sequence

import numpy as np
from anyjev.readout import LabelTokenError

TOP_N = 50  # 20 on Ollama; more costs nothing measurable on llama-server


def _request(url: str, body: dict | None, timeout: float) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def server_model_path(base_url: str) -> str:
    """The GGUF the server at `base_url` loaded (only readable on the machine that runs it)."""
    return _request(base_url.rstrip("/") + "/props", None, timeout=30)["model_path"]


class ServerBackend:
    def __init__(self, base_url: str = "http://localhost:8080", gguf_path: str | None = None, tokenizer=None,
                 missing: str = "raise", timeout: float = 120.0):
        if missing not in ("raise", "bound"):
            raise ValueError("missing must be 'raise' or 'bound'")
        self.base_url = base_url.rstrip("/")
        self.missing = missing
        self.timeout = timeout
        self.stats = {"requests": 0, "bounded_labels": 0, "labels_read": 0}
        if tokenizer is None:
            from anyjev.backends.llamacpp import GGUFTokenizer
            from llama_cpp import Llama

            self._vocab = Llama(model_path=gguf_path or server_model_path(self.base_url), vocab_only=True,
                                verbose=False)
            tokenizer = GGUFTokenizer(self._vocab)
        self.tokenizer = tokenizer
        self.name = f"llama-server:{self.base_url}"

    def _one(self, prompt: str, ids: Sequence[int]) -> np.ndarray:
        body = {"prompt": self.tokenizer.encode(prompt), "n_predict": 1, "n_probs": TOP_N, "temperature": 0.0,
                "cache_prompt": True}
        out = _request(self.base_url + "/completion", body, self.timeout)
        self.stats["requests"] += 1
        top = {e["id"]: float(e["logprob"]) for e in out["completion_probabilities"][0]["top_logprobs"]}
        floor = min(top.values())
        res, missing = [], []
        for tid in ids:
            lp = top.get(int(tid))
            if lp is None:
                missing.append(tid)
                lp = floor
            res.append(lp)
        self.stats["labels_read"] += len(ids)
        if missing:
            if self.missing == "raise":
                raise LabelTokenError(f"label tokens {missing} are outside the server's top {TOP_N} logprobs")
            self.stats["bounded_labels"] += len(missing)
        return np.array(res, dtype=np.float64)

    def next_token_logprobs(self, prompts: Sequence[str], token_ids: Sequence[Sequence[int]]) -> list[np.ndarray]:
        if len(prompts) != len(token_ids):
            raise ValueError("prompts and token_ids differ in length")
        return [self._one(p, ids) for p, ids in zip(prompts, token_ids)]
