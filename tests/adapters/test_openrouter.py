"""The hosted provider for the SWE-bench API arm (ADR 0012): no network, no real key."""

import datetime

import pytest

from colloid.adapters.llm import openai_compat, openrouter
from colloid.ports import LLMError


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code, self._payload, self.text = status, payload, ""

    def json(self) -> dict:
        return self._payload


def test_the_key_comes_only_from_the_environment(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(LLMError, match="environment"):
        openrouter.provider("qwen/qwen3.8-27b:free")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    p = openrouter.provider("qwen/qwen3.8-27b:free", "medium")
    assert p.headers == {"Authorization": "Bearer test-key"} and p.usd_per_cpu_second == 0.0


def test_reasoning_settings_reach_the_request_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    sent = {}

    def post(url, json, timeout, headers):
        sent.update(url=url, body=json)
        return _Resp(200, {"choices": [{"message": {"content": "```python\nx = 1\n```"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 7}})

    monkeypatch.setattr(openai_compat.httpx, "post", post)
    c = openrouter.provider("qwen/qwen3.8-27b:free", "medium").complete("qwen/qwen3.8-27b:free", "sys", [{"role": "user", "content": "u"}],
                                                                         max_tokens=10, temperature=0.2)
    assert sent["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert sent["body"]["reasoning"] == {"effort": "medium"} and c.tokens_out == 7 and c.cost_usd == 0.0


def test_free_quota_parsing(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert openrouter.free_requests_remaining(lambda *a, **k: _Resp(200, {"data": {"free_model_daily_requests": {"remaining": 7}}})) == 7
    assert openrouter.free_requests_remaining(lambda *a, **k: _Resp(200, {"data": {"free_model_daily_requests": {"limit": 50, "used": 48}}})) == 2
    assert openrouter.free_requests_remaining(lambda *a, **k: _Resp(200, {"data": {}})) is None
    assert openrouter.free_requests_remaining(lambda *a, **k: _Resp(401, {})) is None


def test_pace_waits_for_the_daily_reset_only_when_an_instance_would_be_cut_short():
    slept: list[float] = []
    noon = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=datetime.UTC)
    openrouter.pace(16, remaining=lambda: 20, sleep=slept.append, now=lambda: noon, log=lambda m: None)
    openrouter.pace(16, remaining=lambda: None, sleep=slept.append, now=lambda: noon, log=lambda m: None)
    assert slept == []
    openrouter.pace(16, remaining=lambda: 3, sleep=slept.append, now=lambda: noon, log=lambda m: None)
    assert slept == [12 * 3600 + 60]
