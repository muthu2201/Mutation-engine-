"""OpenRouter as a hosted provider, for the SWE-bench API arm (ADR 0012).

The key is read only from the ``OPENROUTER_API_KEY`` environment variable (set in the
environment's settings), never from a file, the command line or a log. Free model variants are
capped per day; :func:`pace` waits until a whole instance's call budget is available, so no
instance's search is cut short by the cap.
"""

from __future__ import annotations

import datetime
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from colloid.adapters.llm.openai_compat import OpenAICompatProvider
from colloid.ports import LLMError

BASE_URL = "https://openrouter.ai/api"


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise LLMError("OPENROUTER_API_KEY is not set; add it in the environment's settings")
    return key


def provider(model: str, reasoning_effort: str | None = None) -> OpenAICompatProvider:
    extra: dict[str, Any] = {"reasoning": {"effort": reasoning_effort}} if reasoning_effort else {}
    return OpenAICompatProvider(BASE_URL, [model], name="openrouter", api_key=api_key(), context_tokens=262_144,
                                usd_per_cpu_second=0.0, extra_body=extra)


def free_requests_remaining(get: Callable[..., Any] = httpx.get) -> int | None:
    """Free-model requests left today, or ``None`` when the account is not capped or does not say."""
    r = get(f"{BASE_URL}/v1/key", headers={"Authorization": f"Bearer {api_key()}"}, timeout=30.0)
    if r.status_code != 200:
        return None
    daily = (r.json().get("data") or {}).get("free_model_daily_requests") or {}
    if daily.get("remaining") is not None:
        return int(daily["remaining"])
    if daily.get("limit") is not None and daily.get("used") is not None:
        return int(daily["limit"]) - int(daily["used"])
    return None


def seconds_to_reset(now: datetime.datetime) -> float:
    """Until one minute past the next UTC midnight, when daily caps reset."""
    nxt = (now + datetime.timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return (nxt - now).total_seconds()


def pace(needed: int, *, log: Callable[[str], None] = print, remaining: Callable[[], int | None] = free_requests_remaining,
         sleep: Callable[[float], None] = time.sleep,
         now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)) -> None:
    left = remaining()
    if left is None or left >= needed:
        return
    wait = seconds_to_reset(now())
    log(f"[pace] {left} free requests left today, an instance needs up to {needed}: waiting {wait / 3600:.1f} h for the daily reset")
    sleep(wait)
