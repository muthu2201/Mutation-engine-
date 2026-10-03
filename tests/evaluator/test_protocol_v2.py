"""Protocol v2 (ADR 0014): the engine changes behind `--protocol v2`, and v1 left exactly as it was."""

from pathlib import Path

from colloid.core import repair
from colloid.ports import Completion
from colloid.services import swebench as swe
from colloid_evaluator.swebench import repos
from colloid_evaluator.swebench.judge import Verdict

ISSUE = "shout() in pkg/words.py must upper-case its argument, but shout('hi') returns 'hi'."
SRC = "def shout(text):\n    return text\n"
REPRO = "```python\nprint('ISSUE REPRODUCED')\nprint('ISSUE RESOLVED')\n```"
FIX = "```python\ndef shout(text):\n    return text.upper()\n```"
SAME = "```python\ndef shout(text):\n    return text\n```"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir(parents=True)
    (tmp_path / "pkg/words.py").write_text(SRC)
    return tmp_path


class FakeLLM:
    """Replays scripted responses: repro prompts get REPRO, fix prompts the next item of ``fixes``."""

    def __init__(self, fixes: list[str]) -> None:
        self.fixes = list(fixes)
        self.calls = 0

    def complete(self, model, system, messages, *, max_tokens, temperature, timeout_s):
        self.calls += 1
        text = REPRO if system == repair.SYSTEM_REPRO else (self.fixes.pop(0) if self.fixes else FIX)
        return Completion(text=text, model=model, tokens_in=10, tokens_out=len(text), latency_s=0.1, cost_usd=0.0, finish_reason="stop")


class FakeJudge:
    def __init__(self) -> None:
        self.evaluated = 0

    def validate_repro(self, script: str) -> str:
        return "ISSUE REPRODUCED"

    def evaluate(self, rel: str, new_text: str, scripts):
        self.evaluated += 1
        diff = f"--- a/{rel}\n+++ b/{rel}\n-    return text\n+" + new_text.splitlines()[1]
        return Verdict("L3", True, diff=diff, votes=len(scripts), repro=["ISSUE RESOLVED"] * len(scripts))


def _solve(tmp_path, fixes, protocol, calls=6):
    llm = FakeLLM(fixes)
    r = swe.Repairer(llm, ["big", "small"], swe.Budget(llm_calls=calls, protocol=protocol), log=lambda m: None)
    rec = r.solve({"instance_id": "x", "problem_statement": ISSUE, "repo": "acme/pkg"}, FakeJudge(), _repo(tmp_path))
    return rec, llm


def test_v1_still_stops_at_the_first_candidate_that_resolves_every_reproduction(tmp_path):
    rec, _ = _solve(tmp_path, [FIX], "v1")
    assert rec["stop"] == "every validated reproduction resolved" and rec["protocol"] == "v1"
    assert len(rec["repro"]) == 2 and "free_declines" not in rec


def test_v2_writes_three_focused_reproductions_routes_first_to_the_largest_model_and_does_not_stop_early(tmp_path):
    rec, _ = _solve(tmp_path, [FIX] * 10, "v2")
    assert [r.get("focus") for r in rec["repro"]] == [0, 1, 2]
    assert rec["stop"] == "budget" and rec["candidates"][0]["model"] == "big"
    assert rec["submission"] is not None and rec["submission"]["votes"] == 1  # identical scripts are deduplicated


def test_v2_charges_an_unchanged_snippet_as_a_decline_and_moves_on(tmp_path):
    rec, _ = _solve(tmp_path, [SAME] * 20, "v2", calls=6)
    assert rec["free_declines"] == 2 and rec["stop"] == "every snippet declined" and rec["submission"] is None


def test_free_declines_are_capped_at_half_the_budget(tmp_path):
    (tmp_path / "pkg").mkdir()
    src = "".join(f"def f{i}(text):\n    return text\n\n\n" for i in range(3))
    (tmp_path / "pkg/words.py").write_text(src)
    same = [f"```python\ndef f{i}(text):\n    return text\n```" for i in range(3)]
    llm = FakeLLM([same[0], same[1], same[2]] * 4)
    r = swe.Repairer(llm, ["big"], swe.Budget(llm_calls=8, protocol="v2"), log=lambda m: None)
    issue = "f0(), f1() and f2() in pkg/words.py must upper-case their argument."
    rec = r.solve({"instance_id": "x", "problem_statement": issue, "repo": "acme/pkg"}, FakeJudge(), tmp_path)
    # 3 reproductions + 2 charged declines once the 4 free ones (half of 8) are used; 6 declines exhaust the 3 snippets
    assert rec["free_declines"] == 4 and rec["llm_calls"] == 5 and rec["stop"] == "every snippet declined"


def test_v2_retries_an_empty_endpoint_response_without_charging_it(tmp_path):
    rec, llm = _solve(tmp_path, ["", FIX] + [FIX] * 10, "v2", calls=6)
    assert rec["empty_retries"] == 1 and rec["llm_calls"] == 6 and llm.calls == 7
    assert any(c.get("retry") == 1 for c in rec["calls"])


def test_v2_breaks_ties_by_independent_agreement(tmp_path):
    other = "```python\ndef shout(text):\n    return str(text).upper()\n```"
    rec, _ = _solve(tmp_path, [other, FIX, FIX], "v2", calls=6)
    assert rec["submission"]["diff"].endswith("return text.upper()")


def test_unfenced_code_is_accepted_only_in_v2_and_only_when_it_parses():
    src = "LIMIT = 1\nNAME = 'x'\n"
    snip = repair.snippets(src, "pkg/conf.py")[0]
    bare = "LIMIT = 2\nNAME = 'x'\n"
    assert not repair.parse_fix(bare, snip, src).ok
    assert repair.parse_fix(bare, snip, src, allow_unfenced=True).ok
    assert not repair.parse_fix("The bug is elsewhere.", snip, src, allow_unfenced=True).ok


def test_diff_signature_ignores_whitespace():
    a = "--- a/f\n+++ b/f\n-x = 1\n+x  =  2\n"
    b = "--- a/f\n+++ b/f\n-x=1\n+x = 2\n"
    assert repair.diff_signature(a) == repair.diff_signature(b) != repair.diff_signature(a.replace("2", "3"))


def test_transitive_selection_finds_the_tests_of_modules_that_use_the_edited_one(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/words.py").write_text(SRC)
    (tmp_path / "pkg/greet.py").write_text("from pkg import words\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_greet.py").write_text("from pkg import greet\n")
    assert repos.select_tests(tmp_path, ["pkg/words.py"]) == []
    assert repos.select_tests(tmp_path, ["pkg/words.py"], transitive=True) == ["tests/test_greet.py"]


def test_an_llm_error_while_writing_reproductions_is_recorded_not_fatal(tmp_path):
    from colloid.ports import LLMError

    class Flaky(FakeLLM):
        def complete(self, model, system, messages, **kw):
            if system == repair.SYSTEM_REPRO and self.calls == 0:
                self.calls += 1
                raise LLMError("nvidia unavailable after retries: Server disconnected without sending a response.")
            return super().complete(model, system, messages, **kw)

    for protocol in ("v1", "v2"):
        r = swe.Repairer(Flaky([FIX] * 10), ["big"], swe.Budget(llm_calls=6, protocol=protocol), log=lambda m: None)
        rec = r.solve({"instance_id": "x", "problem_statement": ISSUE, "repo": "acme/pkg"}, FakeJudge(), _repo(tmp_path / protocol))
        assert rec["repro"][0]["outcome"] == "llm error" and rec["submission"] is not None


def test_a_call_cannot_outlive_the_search_budget(tmp_path):
    seen = []

    class Slow(FakeLLM):
        def complete(self, model, system, messages, *, timeout_s, **kw):
            seen.append(timeout_s)
            return super().complete(model, system, messages, timeout_s=timeout_s, **kw)

    r = swe.Repairer(Slow([FIX]), ["big"], swe.Budget(llm_calls=4, search_s=90.0, protocol="v1"), log=lambda m: None)
    r.solve({"instance_id": "x", "problem_statement": ISSUE, "repo": "acme/pkg"}, FakeJudge(), _repo(tmp_path))
    assert seen and all(60.0 <= t <= 90.0 for t in seen)
