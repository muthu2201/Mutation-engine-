"""StackZero target conformance (T05): the baseline runs end to end and records an Evaluation,
genes apply byte-exactly, builds are cached and content-addressed, the differential oracle
passes the baseline against itself, and a known-good index mutation measures a real gain.
Integration (needs Postgres, sandbox, loadgen as root)."""

import os

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
    from colloid.core.genome import Genome

    ws = evaluator.baseline_ws
    build = evaluator.target.build(ws)
    assert build.ok
    res = evaluator.oracle.run(ws, seed=1, size="quick")
    assert res.ok, res.mismatches  # baseline vs baseline: no differences


def test_build_cache_hits(evaluator):
    from colloid.core.genome import Genome

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
