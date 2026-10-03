"""Gemini, Groq and xKiro behind their OpenAI-compatible endpoints: no network, no real keys."""

import pytest

from colloid.adapters.llm import hosted, openai_compat
from colloid.ports import LLMError

OK = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code, self._payload, self.text = status, payload, ""

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for h in hosted.PROVIDERS.values():
        for var in h.env:
            monkeypatch.delenv(var, raising=False)


def test_keys_come_only_from_the_environment_in_declared_order(monkeypatch):
    with pytest.raises(LLMError, match="GEMINI_API_KEY or GOOGLE_API_KEY"):
        hosted.api_key("gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "‎google-key")
    assert hosted.api_key("gemini") == "google-key"
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key\n")
    assert hosted.api_key("gemini") == "gemini-key"


@pytest.mark.parametrize("name, url", [
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("groq", "https://api.groq.com/openai/v1/chat/completions"),
    ("xkiro", "https://api.xkiro.com/v1/chat/completions"),
])
def test_each_provider_posts_to_its_documented_endpoint(monkeypatch, name, url):
    for var in hosted.PROVIDERS[name].env:
        monkeypatch.setenv(var, "k")
    sent = {}

    def post(u, json, timeout, headers):
        sent.update(url=u, body=json, headers=headers)
        return _Resp(200, OK)

    monkeypatch.setattr(openai_compat.httpx, "post", post)
    monkeypatch.setattr(openai_compat.time, "sleep", lambda s: None)
    c = hosted.provider(name, "some-model", "low").complete("some-model", "s", [{"role": "user", "content": "u"}], max_tokens=5, temperature=0.2)
    assert sent["url"] == url and sent["body"]["reasoning_effort"] == "low" and sent["headers"] == {"Authorization": "Bearer k"}
    assert c.text == "OK" and c.cost_usd == 0.0


def test_model_listing(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    seen = {}

    def get(u, headers, timeout):
        seen["url"] = u
        return _Resp(200, {"data": [{"id": "b-model"}, {"id": "a-model"}]})

    assert hosted.list_models("groq", get=get) == ["a-model", "b-model"]
    assert seen["url"] == "https://api.groq.com/openai/v1/models"
    with pytest.raises(LLMError, match="401"):
        hosted.list_models("groq", get=lambda *a, **k: _Resp(401, {}))


def test_only_first_party_hosts_are_marked_first_party():
    assert hosted.PROVIDERS["gemini"].first_party and hosted.PROVIDERS["groq"].first_party and not hosted.PROVIDERS["xkiro"].first_party
