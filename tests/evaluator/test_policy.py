"""L0 policy scanner: the evaluator's independent re-check that blocks reward-hacking and
out-of-locus edits. These are the patterns the canary suite exercises end-to-end."""


from colloid_evaluator.policy import diff_size, scan_c, scan_python

BASE = "def handler(db, ids):\n    out = []\n    for i in ids:\n        out.append(i)\n    return out\n"


def ok(src):
    return scan_python(src, BASE) == []


def bad(src, needle):
    reasons = scan_python(src, BASE)
    assert reasons, f"expected rejection, got none for:\n{src}"
    assert any(needle in r for r in reasons), reasons


def test_accepts_legitimate_rewrite():
    assert ok("def handler(db, ids):\n    return list(ids)\n")
    assert ok("def handler(db, ids):\n    return [i for i in ids]\n")


def test_blocks_function_attribute_cache():
    bad("def handler(db, ids):\n    handler.cache = {}\n    return list(ids)\n", "__dict__" if False else "attribute")


def test_blocks_global_state():
    bad("def handler(db, ids):\n    global _c\n    _c = ids\n    return list(ids)\n", "global")


def test_blocks_param_object_mutation():
    bad("def handler(db, ids):\n    db.memo = 1\n    return list(ids)\n", "attribute")


def test_blocks_forbidden_imports_and_calls():
    bad("def handler(db, ids):\n    import os\n    return list(ids)\n", "import")
    bad("def handler(db, ids):\n    open('/x')\n    return list(ids)\n", "open")
    bad("def handler(db, ids):\n    import time\n    return list(ids)\n", "time")


def test_blocks_introspection_and_timer():
    bad("def handler(db, ids):\n    return ids.__class__\n", "__class__")
    bad("def handler(db, ids):\n    import time\n    time.perf_counter = lambda: 0\n    return ids\n", "time")


def test_blocks_background_tasks():
    bad("def handler(db, ids):\n    import asyncio\n    asyncio.ensure_future(db)\n    return list(ids)\n", "")


def test_blocks_signature_change_and_rename():
    bad("def handler(db, ids, x=1):\n    return list(ids)\n", "signature")
    bad("def other(db, ids):\n    return list(ids)\n", "confinement")


def test_blocks_extra_toplevel():
    bad("def handler(db, ids):\n    return list(ids)\ndef sneaky():\n    pass\n", "confinement")


def test_allows_local_mutation():
    assert ok("def handler(db, ids):\n    acc = []\n    for i in ids:\n        acc.append(i)\n    return acc\n")


def test_diff_size():
    assert diff_size(BASE, BASE) == 0
    assert diff_size(BASE, "def handler(db, ids):\n    return list(ids)\n") > 0


def test_c_policy_blocks_dangerous_and_static():
    base_c = "int f(int a) {\n    return a + 1;\n}\n"
    assert scan_c("int f(int a) {\n    return a + 2 - 1;\n}\n", base_c, "f") == []
    assert any("static" in r for r in scan_c("int f(int a) {\n    static int c = 0;\n    return a + c;\n}\n", base_c, "f"))
    assert any("system" in r for r in scan_c('int f(int a) {\n    system("x");\n    return a;\n}\n', base_c, "f"))
    assert any("signature" in r for r in scan_c("long f(int a) {\n    return a;\n}\n", base_c, "f"))


def test_the_judge_is_never_mutable():
    import copy

    from colloid.adapters.target.stackzero.adapter import StackZeroTarget
    from colloid.core.models import Gene, Mutability, PayloadKind, Provenance, Surface
    from colloid_evaluator.policy import check_gene, is_judge_path, judge_violations

    assert is_judge_path("py:colloid_evaluator/policy.py::scan_python")
    assert is_judge_path("py:tests/core/test_stats.py::test_x") and is_judge_path("py:colloid/core/stats.py::paired_ratio_effect")
    assert is_judge_path("c:colloid/adapters/sandbox/sbx_exec.c::main")
    assert not is_judge_path("py:service/shop/search.py::rating_summary") and not is_judge_path("py:colloid/core/bandit.py::select")

    t = StackZeroTarget(observe_system=False)
    atlas = t.atlas_seed()
    assert judge_violations(atlas) == []  # the reference target exposes no part of the judge
    # a (buggy or hostile) target that maps a mutable locus onto the evaluator
    bad = copy.deepcopy(atlas)
    u = bad.unit_by_path("py:service/shop/search.py::rating_summary")
    bad.units[u.id] = u.model_copy(update={"symbol_path": "py:colloid_evaluator/oracles.py::json_equal"})
    loc = bad.locus_for(u.id, Surface.CODE_REGION)
    assert loc.mutability != Mutability.FROZEN and judge_violations(bad)
    g = Gene.make(loc.id, PayloadKind.SOURCE, {"source": "def json_equal(a, b, path='$'):\n    return None\n"}, Provenance(operator="x"))
    reasons = check_gene(g, bad, {}, {}, {}, [])
    assert reasons and "part of the judge" in reasons[0]
