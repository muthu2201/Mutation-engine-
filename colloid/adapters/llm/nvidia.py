"""NVIDIA's hosted endpoints (build.nvidia.com) as a provider for the SWE-bench API arm (ADR 0012, amendment 2).

The key is read only from the ``NVIDIA_API_KEY`` environment variable, set in the environment's
settings; never from a file, the command line or a log. A verified account's free endpoints have
no daily cap, only a per-minute one (about 40 requests a minute for most models), so requests are
spaced rather than paced by day, and a 429 is retried with backoff.
"""

from __future__ import annotations

import os
from typing import Any

from colloid.adapters.llm.openai_compat import OpenAICompatProvider, clean_key
from colloid.ports import LLMError

BASE_URL = "https://integrate.api.nvidia.com"
DEFAULT_MODEL = "moonshotai/kimi-k3"
REQUESTS_PER_MINUTE = 40
# models that fix their own sampling (Kimi K3: temperature 1.0, top_p 0.95) and expect it omitted
FIXED_SAMPLING = {"moonshotai/kimi-k3"}
CONTEXT = {"moonshotai/kimi-k3": 1_048_576}


def api_key() -> str:
    key = clean_key(os.environ.get("NVIDIA_API_KEY", ""))
    if not key:
        raise LLMError("NVIDIA_API_KEY is not set; add it in the environment's settings")
    return key


def provider(model: str = DEFAULT_MODEL, reasoning_effort: str | None = None) -> OpenAICompatProvider:
    extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    return OpenAICompatProvider(BASE_URL, [model], name="nvidia", api_key=api_key(), context_tokens=CONTEXT.get(model, 131_072),
                                usd_per_cpu_second=0.0, extra_body=extra, fixed_sampling=model in FIXED_SAMPLING,
                                min_interval_s=60.0 / REQUESTS_PER_MINUTE, max_retries=6)
