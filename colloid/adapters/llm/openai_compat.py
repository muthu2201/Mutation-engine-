"""LLMProvider adapter for self-hosted open-weight models behind an OpenAI-compatible
``/v1/chat/completions`` endpoint (llama.cpp server, vLLM, Ollama, LM Studio...).

This is how Colloid runs *with no external API at all*: the default local setup serves
Qwen2.5-Coder 3B and 1.5B (Q4_K_M GGUF) with ``llama_cpp.server`` on loopback, and each
model becomes its own bandit arm. Small local models are much weaker mutators than frontier
models - the 1.5B model will happily "optimise" an order-preserving dedupe into
``list(set(xs))`` - which is precisely why every proposal goes through the evaluator.

Cost accounting: local inference is not free, it burns CPU. The adapter prices each call as
``latency × cpu_seconds_per_second × usd_per_cpu_second`` so the bandit's reward-per-cost
comparison between local and hosted models is apples to apples.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from colloid.ports import Completion, LLMError, ModelInfo


def clean_key(raw: str) -> str:
    """An API key as pasted into a settings field can carry invisible characters (a left-to-right mark from a
    phone browser, a zero-width space, a trailing newline) that HTTP headers reject; keys never contain them."""
    return "".join(ch for ch in raw if not ch.isspace() and unicodedata.category(ch) != "Cf")


class OpenAICompatProvider:
    PORT_API = "1.0.0"

    def __init__(
        self,
        base_url: str,
        models: Sequence[str],
        *,
        name: str = "local",
        context_tokens: int = 8192,
        usd_per_cpu_second: float = 1.24e-5,
        cpus_used: float = 4.0,
        max_retries: int = 3,
        api_key: str | None = None,
        extra_body: Mapping[str, Any] | None = None,
        fixed_sampling: bool = False,
        min_interval_s: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._models = tuple(models)
        self.name = name
        self.context_tokens = context_tokens
        self.usd_per_cpu_second = usd_per_cpu_second
        self.cpus_used = cpus_used
        self.max_retries = max_retries
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.extra_body = dict(extra_body or {})  # e.g. a hosted reasoning model's {"reasoning": {"effort": "medium"}}
        self.fixed_sampling = fixed_sampling  # the model fixes temperature/top_p itself and wants them omitted
        self.min_interval_s = min_interval_s  # a per-minute request cap, spread evenly
        self._last_request = 0.0

    def models(self) -> Sequence[ModelInfo]:
        return [ModelInfo(m, self.context_tokens, 0.0, 0.0, local=True) for m in self._models]

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/v1/models", timeout=3.0, headers=self.headers)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    def complete(
        self,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float = 300.0,
    ) -> Completion:
        if model not in self._models:
            raise LLMError(f"model {model} is not served by {self.name}")
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *[dict(m) for m in messages]],
            "max_tokens": max_tokens,
            **({} if self.fixed_sampling else {"temperature": temperature, "top_p": 0.95}),
            **self.extra_body,
        }
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            wait = self._last_request + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            start = self._last_request = time.monotonic()
            try:
                r = httpx.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=timeout_s, headers=self.headers)
            except httpx.HTTPError as exc:
                last = exc
            else:
                if r.status_code == 200:
                    data = r.json()
                    latency = time.monotonic() - start
                    choice = data["choices"][0]
                    usage = data.get("usage") or {}
                    return Completion(
                        text=choice["message"]["content"] or "",
                        model=model,
                        tokens_in=int(usage.get("prompt_tokens", 0)),
                        tokens_out=int(usage.get("completion_tokens", 0)),
                        latency_s=latency,
                        cost_usd=latency * self.cpus_used * self.usd_per_cpu_second,
                        finish_reason=str(choice.get("finish_reason", "")),
                    )
                if r.status_code < 500 and r.status_code != 429:
                    raise LLMError(f"{self.name} returned {r.status_code}: {r.text[:300]}")
                last = LLMError(f"{self.name} returned {r.status_code}")
            time.sleep(min(30.0, 2.0 * 2**attempt))
        raise LLMError(f"{self.name} unavailable after retries: {last}")
