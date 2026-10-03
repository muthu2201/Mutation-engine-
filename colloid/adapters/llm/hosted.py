"""More hosted providers behind OpenAI-compatible endpoints: Gemini (Google AI Studio), Groq and xKiro.

Endpoints are as documented by each provider (checked 2026-10-03):

- Gemini: https://generativelanguage.googleapis.com/v1beta/openai/chat/completions; `reasoning_effort` minimal..high.
- Groq: https://api.groq.com/openai/v1/chat/completions.
- Amazon Bedrock: https://bedrock-mantle.{region}.api.aws/v1/chat/completions with a Bedrock API key (Bearer).
- xKiro: https://api.xkiro.com/v1/chat/completions, a third-party gateway reselling many vendors' models. It
  publishes no data-retention or training policy and cannot prove which model answered, so it is for exploration
  on public code only, never for a pre-registered arm or proprietary code.

Keys come only from the environment (the first variable set, cleaned of invisible characters), never from a
file, the command line or a log. Requests are spaced to a conservative per-minute rate; a 429 is retried.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from colloid.adapters.llm.openai_compat import OpenAICompatProvider, clean_key
from colloid.ports import LLMError


@dataclass(frozen=True)
class Hosted:
    name: str
    base_url: str
    chat_path: str
    env: tuple[str, ...]
    requests_per_minute: int
    first_party: bool  # the model's vendor (or its official host) serves the requests


PROVIDERS = {
    "gemini": Hosted("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "/chat/completions",
                     ("GEMINI_API_KEY", "GOOGLE_API_KEY"), 10, True),
    "groq": Hosted("groq", "https://api.groq.com/openai", "/v1/chat/completions", ("GROQ_API_KEY",), 30, True),
    "xkiro": Hosted("xkiro", "https://api.xkiro.com", "/v1/chat/completions", ("XKIRO_API_KEY",), 20, False),
    # Amazon Bedrock's OpenAI-compatible endpoint (bedrock-mantle), authenticated with a Bedrock API key as a Bearer token.
    # The region comes from BEDROCK_REGION / AWS_REGION (default us-east-1); see base_url().
    "bedrock": Hosted("bedrock", "https://bedrock-mantle.{region}.api.aws", "/v1/chat/completions",
                      ("AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_API_KEY"), 30, True),
}


def base_url(name: str) -> str:
    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    return PROVIDERS[name].base_url.format(region=region)


def api_key(name: str) -> str:
    h = PROVIDERS[name]
    for var in h.env:
        key = clean_key(os.environ.get(var, ""))
        if key:
            return key
    raise LLMError(f"{' or '.join(h.env)} is not set; add it in the environment's settings")


def provider(name: str, model: str, reasoning_effort: str | None = None) -> OpenAICompatProvider:
    h = PROVIDERS[name]
    extra: dict[str, Any] = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
    return OpenAICompatProvider(base_url(name), [model], name=name, api_key=api_key(name), context_tokens=131_072, usd_per_cpu_second=0.0,
                                extra_body=extra, min_interval_s=60.0 / h.requests_per_minute, max_retries=6, chat_path=h.chat_path)


def list_models(name: str, get: Any = httpx.get) -> list[str]:
    """The model ids the key can use, from the provider's OpenAI-compatible /models listing."""
    h = PROVIDERS[name]
    r = get(f"{base_url(name)}{h.chat_path.replace('chat/completions', 'models')}", headers={"Authorization": f"Bearer {api_key(name)}"},
            timeout=30.0)
    if r.status_code != 200:
        raise LLMError(f"{name} /models returned {r.status_code}: {r.text[:200]}")
    return sorted(m["id"] for m in r.json().get("data", []))
