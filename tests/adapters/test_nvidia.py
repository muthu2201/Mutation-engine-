"""NVIDIA's hosted endpoints for the SWE-bench API arm (ADR 0012, amendment 2): no network, no real key."""

import importlib.util
from pathlib import Path

import pytest

from colloid.adapters.llm import nvidia, openai_compat
from colloid.ports import Completion, LLMError
from colloid.services import swebench as swe

OK = {"choices": [{"message": {"content": "```python\nx = 1\n```"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}}


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code, self._payload, self.text = status, payload, ""

    def json(self) -> dict:
        return self._payload


def test_the_key_comes_only_from_the_environment(monkeypatch):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(LLMError, match="environment"):
        nvidia.provider()
    monkeypatch.setenv("NVIDIA_API_KEY", "\u200etest-key\u200b\n")  # as pasted from a phone browser
    assert nvidia.provider().headers == {"Authorization": "Bearer test-key"}
    monkeypatch.setenv("NVIDIA_API_KEY", "\u200e")
    with pytest.raises(LLMError, match="environment"):
        nvidia.provider()


def test_kimi_k3_gets_its_effort_and_no_sampling_overrides(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    sent: list[dict] = []

    def post(url, json, timeout, headers):
        sent.append({"url": url, "body": json})
        return _Resp(200, OK)

    monkeypatch.setattr(openai_compat.httpx, "post", post)
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: None)
    nvidia.provider("moonshotai/kimi-k3", "high").complete("moonshotai/kimi-k3", "s", [{"role": "user", "content": "u"}], max_tokens=10, temperature=0.7)
    body = sent[0]["body"]
    assert sent[0]["url"] == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert body["reasoning_effort"] == "high" and "temperature" not in body and "top_p" not in body
    nvidia.provider("z-ai/glm-5.3").complete("z-ai/glm-5.3", "s", [{"role": "user", "content": "u"}], max_tokens=10, temperature=0.7)
    assert sent[1]["body"]["temperature"] == 0.7 and "reasoning_effort" not in sent[1]["body"]


def test_a_rate_limit_is_retried(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    replies = [_Resp(429, {}), _Resp(429, {}), _Resp(200, OK)]
    slept: list[float] = []
    monkeypatch.setattr(openai_compat.httpx, "post", lambda *a, **k: replies.pop(0))
    monkeypatch.setattr(openai_compat.time, "sleep", slept.append)
    c = nvidia.provider().complete(nvidia.DEFAULT_MODEL, "s", [{"role": "user", "content": "u"}], max_tokens=10, temperature=0.0)
    assert c.tokens_out == 7 and not replies and len([s for s in slept if s >= 2.0]) == 2


def test_every_call_is_logged_with_how_it_ended():
    class FakeLLM:
        def complete(self, model, system, messages, *, max_tokens, temperature, timeout_s):
            return Completion(text="", model=model, tokens_in=3, tokens_out=max_tokens, latency_s=12.345, cost_usd=0.0, finish_reason="length")

    r = swe.Repairer(FakeLLM(), ["m"], swe.Budget(), log=lambda m: None)
    r._ask("m", "s", "p", temperature=0.2, max_tokens=50)
    assert r.call_log == [{"model": "m", "latency_s": 12.35, "tokens_in": 3, "tokens_out": 50, "finish_reason": "length", "empty": True}]


def _api_pilot():
    spec = importlib.util.spec_from_file_location("api_pilot", Path(__file__).resolve().parents[2] / "stress/api_pilot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_pilot_rule_picks_the_highest_effort_that_fits_the_budget():
    ap = _api_pilot()

    def s(lat, unusable, errors=()):
        return {"calls": 20, "errors": list(errors), "median_latency_s": lat, "unusable_share": unusable}

    assert ap.choose({"high": s(60, 0.1), "low": s(20, 0.0)})[0] == "high"
    assert ap.choose({"high": s(90, 0.1), "low": s(20, 0.0)})[0] == "low"  # too slow for 16 calls in 20 minutes
    assert ap.choose({"high": s(60, 0.5), "low": s(20, 0.0)})[0] == "low"  # too many calls cut off
    assert ap.choose({"high": s(120, 0.1), "low": s(80, 0.0)}) == ("low", "none fits; the lowest effort runs and the time budget binds")
    assert ap.choose({"high": s(60, 0.1, ["x: 400"]), "low": s(20, 0.0, ["y: 400"])})[0] is None
