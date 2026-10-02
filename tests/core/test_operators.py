"""Operators: peephole rewrites preserve behaviour, LLM response parsing confines to the locus,
knob sampling/perturbation stays in range, GI edits stay parseable, red-team variants build."""

import ast
import random

import pytest

from colloid.core.knobs import KnobSpec, from_unit, perturb_value, sample_value, to_unit, validate_value
from colloid.core.operators.gi_edit import gi_edit
from colloid.core.operators.llm_rewrite import build_request, parse_c_response, parse_python_response, MutationContext
from colloid.core.operators.py_rewrite import find_rewrites
from colloid.core.operators.redteam import HACKS, redteam_variant

# ------------------------------------------------------------------ peephole rewrites

# each case: (rule, source, list of (args_tuple, expected))
CASES = [
    ("membership_set", "def f(x):\n    return x in [1, 2, 3]\n", [((2,), True), ((9,), False)]),
    ("accumulate_sum", "def f(xs):\n    t = 0\n    for v in xs:\n        t += v\n    return t\n", [(([1, 2, 3],), 6), (([],), 0)]),
    ("append_to_listcomp", "def f(xs):\n    r = []\n    for v in xs:\n        r.append(v * 2)\n    return r\n", [(([1, 2],), [2, 4])]),
    ("dict_get", "def f(d, k):\n    if k in d:\n        v = d[k]\n    else:\n        v = 0\n    return v\n", [(({"a": 5}, "a"), 5), (({"a": 5}, "z"), 0)]),
    ("sorted_first", "def f(xs):\n    return sorted(xs, key=lambda z: -z)[0]\n", [(([1, 5, 3],), 5)]),
]


@pytest.mark.parametrize("rule,src,cases", CASES)
def test_rewrite_applies_and_parses(rule, src, cases):
    sites = [s for s in find_rewrites(src) if s.rule == rule]
    assert sites, f"{rule} not found"
    new = sites[0].apply()
    ast.parse(new)  # valid
    assert new != src


@pytest.mark.parametrize("rule,src,cases", CASES)
def test_rewrite_preserves_behaviour(rule, src, cases):
    site = next(s for s in find_rewrites(src) if s.rule == rule)
    ns_old, ns_new = {}, {}
    exec(compile(src, "old", "exec"), ns_old)  # noqa: S102 - test fixture code is trusted
    exec(compile(site.apply(), "new", "exec"), ns_new)  # noqa: S102
    for args, expect in cases:
        assert ns_old["f"](*args) == ns_new["f"](*args) == expect


def test_dedupe_seen_set_preserves_order():
    src = "def f(xs):\n    seen = []\n    for x in xs:\n        if x not in seen:\n            seen.append(x)\n    return seen\n"
    site = next(s for s in find_rewrites(src) if s.rule == "dedupe_seen_set")
    ns_o, ns_n = {}, {}
    exec(compile(src, "o", "exec"), ns_o)  # noqa: S102
    exec(compile(site.apply(), "n", "exec"), ns_n)  # noqa: S102
    data = [3, 1, 3, 2, 1, 4]
    assert ns_o["f"](data) == ns_n["f"](data) == [3, 1, 2, 4]


# ------------------------------------------------------------------ LLM parsing

ORIG = "def handler(db, ids):\n    out = []\n    for i in ids:\n        out.append(i)\n    return out\n"


def test_llm_parse_accepts_confined_rewrite():
    resp = "Here:\n```python\ndef handler(db, ids):\n    return list(ids)\n```\n"
    r = parse_python_response(resp, ORIG)
    assert r.ok and "return list(ids)" in r.source


def test_llm_parse_rejects_signature_change():
    r = parse_python_response("```python\ndef handler(db, ids, extra=1):\n    return ids\n```", ORIG)
    assert not r.ok and "signature" in r.reason


def test_llm_parse_rejects_extra_toplevel():
    r = parse_python_response("```python\nimport os\ndef handler(db, ids):\n    return ids\n```", ORIG)
    assert not r.ok


def test_llm_parse_rejects_rename():
    r = parse_python_response("```python\ndef other(db, ids):\n    return ids\n```", ORIG)
    assert not r.ok


def test_llm_parse_c_signature_guard():
    orig = "int add(int a, int b) {\n    return a + b;\n}\n"
    good = parse_c_response("```c\nint add(int a, int b) {\n    return b + a;\n}\n```", orig, "add")
    assert good.ok
    bad = parse_c_response("```c\nlong add(int a, int b) {\n    return a + b;\n}\n```", orig, "add")
    assert not bad.ok


def test_build_request_includes_localisation():
    ctx = MutationContext(locus_id="l", unit_name="handler", module="h.py", layer="svc", language="python", source=ORIG,
                          template="sql_batching", leverage=0.8, hotness=0.3, paths=("GET /x",), target_context="schema here",
                          available_names=("db", "util"))
    req = build_request(ctx)
    text = req.messages[0]["content"]
    assert "Causal leverage" in text and "schema here" in text and "round-trips" in text.lower()
    assert req.prompt_hash


# ------------------------------------------------------------------ knobs

def test_knob_roundtrip_and_range():
    spec = KnobSpec(name="k", layer="db", type="int", low=16, high=2048, scale="log", default=128, mechanism="guc", key="x")
    assert validate_value(spec, 128) is None
    assert validate_value(spec, 5000) is not None
    assert abs(to_unit(spec, from_unit(spec, 0.5)) - 0.5) < 0.01
    rng = random.Random(0)
    for _ in range(100):
        assert validate_value(spec, sample_value(spec, rng)) is None
        assert validate_value(spec, perturb_value(spec, 128, rng)) is None


def test_knob_perturb_changes_value():
    spec = KnobSpec(name="k", layer="x", type="enum", choices=("a", "b", "c"), default="a", mechanism="e", key="k")
    rng = random.Random(0)
    assert perturb_value(spec, "a", rng) != "a"
    b = KnobSpec(name="b", layer="x", type="bool", default=False, mechanism="e", key="k")
    assert perturb_value(b, False, rng) is True


# ------------------------------------------------------------------ GI + redteam

def test_gi_edit_stays_parseable():
    src = "def f(xs):\n    a = 1\n    b = 2\n    c = a + b\n    return c\n"
    rng = random.Random(0)
    for _ in range(50):
        res = gi_edit(src, rng)
        if res is not None:
            ast.parse(res[0])


def test_redteam_variants_build():
    src = "async def h(db, ids):\n    out = []\n    for i in ids:\n        out.append(i)\n    return out\n"
    rng = random.Random(0)
    built = [redteam_variant(src, h, rng) for h in HACKS]
    assert any(v is not None for v in built)
    for v in built:
        if v is not None:
            ast.parse(v)
