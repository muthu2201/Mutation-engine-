"""The evidence ladder: every gate opens only on its pre-registered evidence."""

from colloid.core.ladder import BLOCKED, OPEN, PASSED, ArmResult, LadderInput, evaluate

LANG = {"py": "python", "go": "go", "shopx": "go"}
SCHEMA = {"py": "shop", "go": "shop", "shopx": "ledger"}


def program(target, genes=(), attribution=()):
    return {"target": target, "genes": list(genes), "attribution": list(attribution)}


def gates(**kw):
    base = {"programs": [program("py")], "language_of": LANG, "schema_of": SCHEMA, "canaries": {"py": {"all_rejected": True, "rejected": 17, "total": 17}}}
    base.update(kw)
    return {g.rung: g for g in evaluate(LadderInput(**base))}


def test_one_language_is_not_neutrality_and_missing_canaries_hold_the_judge_gate():
    g = gates()
    assert g["J"].status == PASSED and g["M1a"].status == OPEN and g["M3"].status == BLOCKED
    g = gates(programs=[program("py"), program("go")])
    assert g["M1a"].status == PASSED and g["J"].status == OPEN  # a go program without a go canary report


def test_transfer_gate_uses_the_preregistered_margins():
    cold = ArmResult("cold", 30.0, 2.0, 1.0, 3, 40)  # 15 %/h
    fast = ArmResult("primed", 29.0, 0.5, 0.5, 3, 40)  # 58 %/h, endpoint -1 pp: passes
    worse = ArmResult("primed", 20.0, 0.5, 0.5, 3, 40)  # 40 %/h but -10 pp: inferior endpoint
    slow = ArmResult("primed", 31.0, 1.8, 0.9, 3, 40)  # 17 %/h: not 1.25x
    assert gates(transfer=(cold, fast))["M1b"].status == PASSED
    assert gates(transfer=(cold, worse))["M1b"].status == OPEN
    assert gates(transfer=(cold, slow))["M1b"].status == OPEN
    assert gates(transfer=(ArmResult("cold", None, None, None, 0, 40), fast))["M1b"].status == PASSED


def test_rules_generalise_only_on_a_held_out_schema():
    rule = {"name": "equality-filter-index", "version": 1, "evidence": [{"target": "py"}]}
    genes = {"g1": {"provenance": {"operator": "rule_apply", "template": "equality-filter-index"}}}
    carrying = [{"gene": "g1", "method": "leave_one_out", "value": 0.1, "ci": [0.05, 0.15]}]
    same_schema = gates(rules=[rule], genes=genes, programs=[program("py"), program("go", ["g1"], carrying)])
    assert same_schema["M2"].status == OPEN  # another implementation of the same database is not held out
    held_out = gates(rules=[rule], genes=genes, programs=[program("py"), program("shopx", ["g1"], carrying)])
    assert held_out["M2"].status == PASSED and held_out["M3"].status == OPEN
    hitchhiker = [{"gene": "g1", "method": "leave_one_out", "value": 0.0, "ci": [-0.02, 0.02]}]
    assert gates(rules=[rule], genes=genes, programs=[program("shopx", ["g1"], hitchhiker)])["M2"].status == OPEN
