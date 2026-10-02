"""ProgramStore conformance kit - runs against SQLite always, and Postgres when COLLOID_PG is set.

A store adapter is accepted only if it round-trips every record type and the content-address
invariants hold (same gene id -> one row; program status transitions persist)."""

import os

import pytest

from colloid.core.ids import content_hash
from colloid.core.models import (
    Alert,
    AttributionRecord,
    EpistasisRecord,
    Evaluation,
    Gene,
    LLMCallRecord,
    MetricSummary,
    ObjectiveEstimate,
    PayloadKind,
    Program,
    ProgramStatus,
    Provenance,
    Stage,
    Verdict,
)

STORES = ["sqlite"]
if os.environ.get("COLLOID_PG"):
    STORES.append("postgres")


@pytest.fixture(params=STORES)
def store(request, tmp_path):
    from colloid.adapters.store.sql_store import PostgresStore, SQLiteStore

    if request.param == "sqlite":
        s = SQLiteStore(str(tmp_path / "t.db"))
    else:
        s = PostgresStore(os.environ["COLLOID_PG"])
    yield s
    s.close()


def gene(v):
    return Gene.make("loc1", PayloadKind.VALUE, {"value": v}, Provenance(operator="t"))


def test_gene_content_address_single_row(store):
    g = gene(5)
    store.put_gene(g)
    store.put_gene(gene(5))  # identical -> same id, no second row
    assert store.get_gene(g.id) == g


def test_program_roundtrip_and_status(store):
    g = gene(7)
    store.put_gene(g)
    prog = Program(id="p1", baseline_id="base", gene_ids=(g.id,), island="svc", generation=1, operator="knob_sample")
    store.put_program(prog)
    assert store.get_program("p1").gene_ids == (g.id,)
    store.set_status("p1", ProgramStatus.ELITE)
    assert store.get_program("p1").status == ProgramStatus.ELITE
    assert [p.id for p in store.programs(status=ProgramStatus.ELITE)] == ["p1"]


def test_evaluation_roundtrip(store):
    ev = Evaluation(
        id="e1", program_id="p1", stage=Stage.L5, protocol_id="L5", verdict=Verdict.PASS,
        metrics={"usd_per_mreq": MetricSummary(median=0.3, ci_lo=0.28, ci_hi=0.32, n=8, unit="USD")},
        objectives=(ObjectiveEstimate(objective="cost", reference="baseline", reference_program_id="base", log_ratio=0.1,
                                      ci_lo=0.05, ci_hi=0.15, p_value=0.01, n_candidate=8, n_reference=8),),
        env_fingerprint={"kernel": "x"}, cost_usd=0.02, duration_s=12.0)
    store.put_evaluation(ev)
    got = store.evaluations("p1")[0]
    assert got.verdict == Verdict.PASS
    assert got.objective("cost", "baseline").significant_gain
    assert got.metrics["usd_per_mreq"].median == 0.3


def test_lineage_and_children(store):
    store.put_lineage("child", "parent", "knob_sample", ["g1"])
    assert store.children("parent") == ["child"]
    assert store.lineage("child")[0]["op"] == "knob_sample"


def test_attribution_epistasis_llm_alert_kv(store):
    store.put_attribution(AttributionRecord(program_id="p1", gene_id="g1", method="shapley", objective="cost", value=0.1, ci_lo=0.05, ci_hi=0.15))
    assert store.attributions("p1")[0].value == 0.1
    store.put_epistasis(EpistasisRecord(gene_a="b", gene_b="a", objective="cost", epsilon=0.05, ci_lo=0.01, ci_hi=0.09, n=1))
    e = store.epistasis()[0]
    assert (e.gene_a, e.gene_b) == ("a", "b")  # stored sorted
    store.put_llm_call(LLMCallRecord(id="c1", provider="local", model="m", template="optimize", params={}, prompt_hash="h",
                                     response_hash="r", tokens_in=10, tokens_out=20, latency_s=1.0, cost_usd=0.0, ok=True))
    assert store.llm_calls()[0].tokens_out == 20
    store.put_alert(Alert(id="a1", kind="canary", severity="info", message="hi"))
    assert store.alerts()[0].message == "hi"
    store.kv_set("k", {"x": 1})
    assert store.kv_get("k") == {"x": 1}


def test_signatures(store):
    store.put_signature("p1", "deadbeef")
    assert ("p1", "deadbeef") in store.signatures()
