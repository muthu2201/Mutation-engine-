"""LLMProvider adapter for Claude via the official Anthropic Python SDK.

Configuration notes that matter for a mutation engine:

* **Model** - defaults to ``claude-opus-5-5``. Any model id from the price table below can
  be added as an extra bandit arm (e.g. ``claude-sonnet-5-5`` as a cheaper mutator).
* **Sampling** - current Claude models do not accept ``temperature``; diversity across
  proposals comes from the varying MutationContext (different loci, neighbours, failure
  summaries, templates) rather than from sampling noise. The ``temperature`` argument of the
  port is therefore ignored by this adapter.
* **Thinking / effort** - thinking is adaptive and always on for Opus 5.5; depth is set
  with ``output_config.effort`` (``high`` by default here: code rewriting is
  intelligence-sensitive, and the default on Opus 5.5 would otherwise be ``medium``).
* **Refusals** - the server-side ``fallbacks="default"`` option is enabled by default, so a
  safety-classifier decline is re-run on a fallback model inside the same call. A final
  ``stop_reason == "refusal"`` is surfaced as an :class:`~colloid.ports.LLMError` (the
  engine records it as a failed proposal; it never reaches the evaluator).
* **Retries** - the SDK retries 408/409/429/5xx and connection errors with exponential
  backoff (``max_retries``); anything still failing becomes an ``LLMError``.

Credentials are resolved by the SDK (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN`` or an
``ant auth login`` profile). The adapter never logs prompts or keys; the engine stores only
prompt/response hashes plus token counts in the program DB.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from colloid.ports import Completion, LLMError, ModelInfo

# USD per million tokens (input, output) - Anthropic first-party API list prices.
PRICES: dict[str, tuple[float, float]] = {
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
CONTEXT: dict[str, int] = {"claude-haiku-4-5": 200_000}
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    PORT_API = "1.0.0"
    name = "anthropic"

    def __init__(
        self,
        models: Sequence[str] = ("claude-opus-5-5",),
        *,
        effort: str = "high",
        refusal_fallbacks: bool = True,
        max_retries: int = 4,
        client: Any | None = None,
    ) -> None:
        unknown = [m for m in models if m not in PRICES]
        if unknown:
            raise ValueError(f"no price data for models {unknown}; add them to PRICES")
        self._models = tuple(models)
        self.effort = effort
        self.refusal_fallbacks = refusal_fallbacks
        self.max_retries = max_retries
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(max_retries=self.max_retries)
        return self._client

    def models(self) -> Sequence[ModelInfo]:
        return [ModelInfo(m, CONTEXT.get(m, 1_000_000), PRICES[m][0], PRICES[m][1], local=False) for m in self._models]

    def available(self) -> bool:
        """True when the SDK can find credentials (env vars or an ``ant`` CLI profile)."""
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        profile_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "anthropic"
        return profile_dir.exists() and any(profile_dir.iterdir())

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
        import anthropic

        if model not in PRICES:
            raise LLMError(f"model {model} is not configured for this provider")
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [dict(m) for m in messages],
            "output_config": {"effort": self.effort},
            "timeout": timeout_s,
        }
        if self.refusal_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        start = time.monotonic()
        try:
            response = self._get_client().beta.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            raise LLMError(f"bad request: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("authentication failed (check ANTHROPIC_API_KEY / ant auth status)") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"permission denied: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"model or endpoint not found: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited after retries") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"connection error: {exc}") from exc
        latency = time.monotonic() - start
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"model declined the request (refusal, category={category})")
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        usage = response.usage
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0) + int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        served = getattr(response, "model", model) or model
        p_in, p_out = PRICES.get(served, PRICES[model])
        cost = tokens_in / 1e6 * p_in + tokens_out / 1e6 * p_out
        return Completion(
            text=text,
            model=served,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_s=latency,
            cost_usd=cost,
            finish_reason=str(response.stop_reason),
        )
