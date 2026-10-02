"""StackZero target conformance (T05): the baseline runs end to end and records an Evaluation,
genes apply byte-exactly, builds are cached and content-addressed, the differential oracle
passes the baseline against itself, and a known-good index mutation measures a real gain.
Integration (needs Postgres, sandbox, loadgen as root)."""


import pytest

from tests.conftest import requires_integration

pytestmark = requires_integration


@pytest.fixture(scope="module")
def evaluator():
    from colloid.adapters.cost.static_prices import StaticPriceCostModel
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget
    from colloid_evaluator.cascade import Evaluator

    ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=45.0)
    ev.setup("baseline")
    yield ev
    ev.shutdown()


def _knob_gene(ev, name, value):
    from colloid.core.models import Gene, PayloadKind, Provenance

    u = ev.atlas.unit_by_path(f"knob:{name}")
    loc = next(lc for lc in ev.atlas.loci.values() if lc.unit_id == u.id)
    return Gene.make(loc.id, PayloadKind.VALUE, {"value": value}, Provenance(operator="test"))


def test_baseline_builds_and_oracle_self_consistent(evaluator):

    ws = evaluator.baseline_ws
    build = evaluator.target.build(ws)
    assert build.ok
    res = evaluator.oracle.run(ws, seed=1, size="quick")
    assert res.ok, res.mismatches  # baseline vs baseline: no differences


def test_build_cache_hits(evaluator):

    ws = evaluator.baseline_ws
    b1 = evaluator.target.build(ws)
    b2 = evaluator.target.build(ws)
    assert b2.cached and b1.artifact_hash == b2.artifact_hash


def test_index_mutation_measures_gain(evaluator):
    from colloid.core.genome import Genome

    g = Genome.of([_knob_gene(evaluator, "db.idx_items_product_cover", True),
                   _knob_gene(evaluator, "db.idx_orders_customer_placed", True)], evaluator.atlas)
    pid = g.program_id("baseline")
    assert evaluator.l0(pid, g).passed
    r1 = evaluator.l1(pid, g)
    assert r1.passed
    assert evaluator.l2(pid, g, r1.ws).passed
    r5 = evaluator.l5(pid, g, r1.ws, "baseline", Genome())
    est = r5.evaluation.objective("cost", "baseline")
    assert est is not None and est.log_ratio > 0 and est.ci_lo > 0  # a real, significant cost win


def test_out_of_range_knob_rejected_at_l0(evaluator):
    from colloid.core.genome import Genome

    g = Genome.of([_knob_gene(evaluator, "db.work_mem_kb", 10**9)], evaluator.atlas)
    assert not evaluator.l0(g.program_id("baseline"), g).passed


def _native_fuzz(ev, genome, *, sanitize):
    from colloid_evaluator import oracles
    from colloid_evaluator.cascade import fresh_seed

    r1 = ev.l1(genome.program_id("baseline"), genome)
    assert r1.passed
    return oracles.native_fuzz(ev.target.sandbox, ev.baseline_ws, r1.ws, ev.target.work, seed=fresh_seed(), iterations=600, sanitize=sanitize)


@pytest.mark.parametrize("sanitize", [False, True], ids=["quick", "asan"])
def test_native_fuzz_leak_check_is_exact_and_ptrace_free(evaluator, sanitize):
    """Regression: LSan aborted every sanitized fuzz run inside the seccomp sandbox (no ptrace),
    failing every native candidate at L6. The driver's own heap accounting must (a) report zero
    net growth for the leak-free baseline and (b) catch a dropped free() in both builds."""
    from colloid.core.genome import Genome
    from colloid_evaluator.canaries.hacks import native_leak

    ok, out = _native_fuzz(evaluator, Genome.of([_flag_gene(evaluator)], evaluator.atlas), sanitize=sanitize)
    assert ok, out
    assert "no leaks" in out and "Sanitizer has encountered a fatal error" not in out
    ok, out = _native_fuzz(evaluator, native_leak(evaluator), sanitize=sanitize)
    assert not ok and "LEAK levenshtein" in out, out


def _flag_gene(ev):
    """A behaviour-neutral native change (compiler flags), so the fuzz compares two real builds."""
    from colloid.core.models import Gene, PayloadKind, Provenance

    u = ev.atlas.unit_by_path("knob:cc.opt")
    loc = next(lc for lc in ev.atlas.loci.values() if lc.unit_id == u.id)
    return Gene.make(loc.id, PayloadKind.VALUE, {"value": "O3"}, Provenance(operator="test"))


def test_sanitizer_runtime_crash_is_infrastructure_not_candidate_fault():
    from colloid_evaluator.oracles import sanitizer_infrastructure_failure

    assert sanitizer_infrastructure_failure("==1==LeakSanitizer has encountered a fatal error.\n==1==HINT: ...ptrace")
    assert not sanitizer_infrastructure_failure("MISMATCH levenshtein(\"a\", \"b\"): candidate 0, baseline 1")
    assert not sanitizer_infrastructure_failure("==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x60")


def test_redteam_liveness_probe_has_both_controls(evaluator):
    """The maximal-variant probe must be able to say both things: a maximal float attack on a
    request-path unit (rating_summary feeds product pages) is caught by the oracle, and the same
    attack on the ASGI lifespan hook (returns None) is inert. Without the positive control,
    'inert' verdicts would prove nothing."""
    import random

    from colloid.core.genome import Genome
    from colloid.core.models import Provenance
    from colloid.core.operators.base import code_gene
    from colloid.core.operators.redteam import redteam_variant

    def l2(path):
        u = evaluator.atlas.unit_by_path(path)
        src = str(u.tags["baseline_source"])
        lid = evaluator.atlas.locus_for(u.id, __import__("colloid.core.models", fromlist=["Surface"]).Surface.CODE_REGION).id
        g = Genome.of([code_gene(lid, u, src, redteam_variant(src, "round_floats", random.Random(0), maximal=True),
                                 Provenance(operator="redteam", notes="maximal liveness probe: round_floats"))], evaluator.atlas)
        pid = g.program_id("baseline")
        r1 = evaluator.l1(pid, g)
        assert r1.passed
        return bool(evaluator.atlas.paths_through(u.id)), evaluator.l2(pid, g, r1.ws)

    on_path, live = l2("py:service/shop/search.py::rating_summary")
    assert on_path and not live.passed, live.evaluation.reasons
    on_path, inert = l2("py:service/shop/app.py::lifespan")
    assert not on_path and inert.passed, inert.evaluation.reasons
