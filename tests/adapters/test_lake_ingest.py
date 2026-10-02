"""Lake ingest + seeding, end to end on a synthetic run store over the real StackZero Atlas:
verified programs become gene + program records, re-ingest is a no-op, later knowledge links
to earlier knowledge (derived_from), and seeds rebuild the *exact* genes, but only while they
still apply to the current stack."""

import copy

import pytest

from colloid.adapters.lake.directory import DirectoryLake
from colloid.adapters.store.sql_store import open_store
from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.core.ids import sha256_hex
from colloid.core.lake import verify_chain
from colloid.core.models import (
    Evaluation,
    Gene,
    ObjectiveEstimate,
    PayloadKind,
    Program,
    ProgramStatus,
    Provenance,
    Stage,
    Surface,
    Verdict,
)
from colloid.services import lake as svc

RATING = "py:service/shop/search.py::rating_summary"


@pytest.fixture(scope="module")
def target():
    t = StackZeroTarget(observe_system=False)
    return t, t.atlas_seed()


def knob_gene(atlas, name, value):
    u = atlas.unit_by_path(f"knob:{name}")
    loc = next(lc for lc in atlas.loci.values() if lc.unit_id == u.id)
    return Gene.make(loc.id, PayloadKind.VALUE, {"value": value}, Provenance(operator="knob_sample"))


def code_gene(atlas, path, transform):
    u = atlas.unit_by_path(path)
    base = str(u.tags["baseline_source"])
    loc = atlas.locus_for(u.id, Surface.CODE_REGION)
    return Gene.make(loc.id, PayloadKind.SOURCE, {"source": transform(base), "base_hash": sha256_hex(base)[:16], "language": "python"},
                     Provenance(operator="llm_rewrite", model="m", template="optimize"))


def make_run(tmp_path, name, atlas, gene_sets, gain=0.3):
    run = tmp_path / name
    run.mkdir()
    store = open_store(f"sqlite:///{run / 'colloid.db'}")
    store.put_atlas(atlas)
    store.put_program(Program(id="base-x", baseline_id="base-x", gene_ids=(), island="baseline", generation=0, status=ProgramStatus.EVALUATED))
    store.kv_set("setup", {"rate_rps": 30.0, "fingerprint": {"kernel": "6.18", "cpu_count": 4}})
    store.kv_set("aa_test", {"noise_floor_per_cycle": {"cost": 0.04}, "gate": "cost: 1/20", "promotions_allowed": True})
    pids = []
    for i, genes in enumerate(gene_sets):
        for g in genes:
            store.put_gene(g)
        pid = Program.make_id("base-x", tuple(g.id for g in genes))
        store.put_program(Program(id=pid, baseline_id="base-x", gene_ids=tuple(g.id for g in genes), island="composition",
                                  generation=1, operator="splice", status=ProgramStatus.PROMOTED))
        est = ObjectiveEstimate(objective="cost", reference="baseline", reference_program_id="base-x", log_ratio=gain + 0.01 * i,
                                ci_lo=gain - 0.05, ci_hi=gain + 0.05, p_value=0.002, n_candidate=10, n_reference=10)
        store.put_evaluation(Evaluation(id=f"ev{name}{i}", program_id=pid, stage=Stage.L6, protocol_id="deep", verdict=Verdict.PASS, objectives=(est,)))
        pids.append(pid)
    store.close()
    return str(run), pids


def test_ingest_seed_roundtrip(tmp_path, target):
    t, atlas = target
    kg = knob_gene(atlas, "db.idx_reviews_product", True)
    cg = code_gene(atlas, RATING, lambda s: s.replace("async def", "async  def", 1))
    lake = DirectoryLake(tmp_path / "lake")
    run1, _ = make_run(tmp_path, "run1", atlas, [[kg]])
    rep = svc.ingest_run(run1, lake, recorded_at="2026-10-01T00:00:00.000000Z", log=lambda m: None)
    assert rep.new_entries == 2 and [p["status"] for p in rep.programs] == ["promoted"]
    again = svc.ingest_run(run1, lake, recorded_at="2026-10-01T00:00:01.000000Z", log=lambda m: None)
    assert again.new_entries == 0 and again.head == rep.head  # idempotent

    run2, _ = make_run(tmp_path, "run2", atlas, [[kg, cg]], gain=0.35)  # the next day: a superset, better
    rep2 = svc.ingest_run(run2, lake, recorded_at="2026-10-02T00:00:00.000000Z", log=lambda m: None)
    assert rep2.new_entries == 2  # the code gene + the new program; the knob gene is already known
    records = lake.records()
    verify_chain(lake.entries(), records)
    newest = records[rep2.programs[0]["record"]]
    assert newest.content["derived_from"] == [rep.programs[0]["record"]]  # day-by-day lineage

    seeds, skipped = svc.seeds(lake, atlas, t.knob_name_of_locus(atlas), top=5)
    assert not skipped and seeds[0].record == newest.id  # best gain first
    assert {g.id for g in seeds[0].genes} == {kg.id, cg.id}  # rebuilt genes are byte-identical
    rows = svc.listing(lake)
    assert [r["seq"] for r in rows] == [0, 1, 2, 3] and rows[0]["recorded_at"] < rows[-1]["recorded_at"]


def test_seed_refuses_a_gene_whose_source_moved_on(tmp_path, target):
    t, atlas = target
    cg = code_gene(atlas, RATING, lambda s: s + "\n")
    lake = DirectoryLake(tmp_path / "lake")
    run, _ = make_run(tmp_path, "run", atlas, [[cg]])
    svc.ingest_run(run, lake, log=lambda m: None)
    changed = copy.deepcopy(atlas)
    u = changed.unit_by_path(RATING)
    u.tags["baseline_source"] = str(u.tags["baseline_source"]) + "# edited upstream\n"
    seeds, skipped = svc.seeds(lake, changed, t.knob_name_of_locus(changed))
    assert seeds == [] and "base_hash mismatch" in skipped[0]


def test_ingest_refuses_a_tampered_lake(tmp_path, target):
    t, atlas = target
    lake = DirectoryLake(tmp_path / "lake")
    run, _ = make_run(tmp_path, "run", atlas, [[knob_gene(atlas, "db.idx_reviews_product", True)]])
    svc.ingest_run(run, lake, log=lambda m: None)
    rec = next((tmp_path / "lake" / "records").glob("*/*.json"))
    rec.write_text(rec.read_text().replace('"promoted"', '"verified"').replace('"knob_sample"', '"forged"'))
    with pytest.raises(Exception, match=r"hash|match"):
        svc.ingest_run(run, lake, log=lambda m: None)


def test_engine_warm_starts_from_the_lake(tmp_path, target, monkeypatch):
    from colloid.adapters.telemetry.jsonl import read_events
    from colloid.services.config import EngineConfig
    from colloid.services.engine import Engine

    t, atlas = target
    kg = knob_gene(atlas, "db.idx_reviews_product", True)
    lake_dir = tmp_path / "lake"
    run, _ = make_run(tmp_path, "earlier", atlas, [[kg]])
    svc.ingest_run(run, DirectoryLake(lake_dir), log=lambda m: None)
    monkeypatch.chdir(tmp_path)
    eng = Engine(EngineConfig(name="warm", lake=str(lake_dir)), target=t)
    eng.baseline_id = "base-x"  # the synthetic run's baseline (seeds are re-addressed to this run's baseline anyway)
    props = eng._lake_proposals(1)
    assert len(props) == 1
    island, (prop, arm, _sig, _ctx) = props[0]
    assert island == "composition" and arm == ("lake_seed", None, None) and prop.genome.gene_ids == (kg.id,)
    assert prop.genome.program_id("base-x") in eng.programs  # registered like any proposal; it still has to pass the cascade
    assert eng._lake_proposals(2) == []  # generation 1 only
    eng.cfg.lake = str(tmp_path / "nowhere-corrupt")
    (tmp_path / "nowhere-corrupt").mkdir()
    (tmp_path / "nowhere-corrupt" / "ledger.jsonl").write_text("{not json\n")
    assert eng._lake_proposals(1) == []  # an unusable lake never stops a run
    eng.tele.close()
    kinds = [e["kind"] for e in read_events(tmp_path / "runs" / "warm" / "events.jsonl")]
    assert "lake.seed" in kinds and "lake.unavailable" in kinds


def test_lake_evidence_seeds_the_engine_bandit(tmp_path, target, monkeypatch):
    from colloid.adapters.store.sql_store import open_store as _open
    from colloid.core.models import AttributionRecord
    from colloid.services.config import EngineConfig
    from colloid.services.engine import Engine

    t, atlas = target
    kg = knob_gene(atlas, "db.idx_reviews_product", True)
    cg = code_gene(atlas, RATING, lambda s: s.replace("async def", "async  def", 1))
    run, (pid,) = make_run(tmp_path, "earlier", atlas, [[kg, cg]])
    st = _open(f"sqlite:///{tmp_path / 'earlier' / 'colloid.db'}")
    st.put_attribution(AttributionRecord(program_id=pid, gene_id=kg.id, method="shapley_exact", objective="cost", value=0.32, ci_lo=0.27, ci_hi=0.37))
    st.put_attribution(AttributionRecord(program_id=pid, gene_id=cg.id, method="shapley_exact", objective="cost", value=0.01, ci_lo=-0.06, ci_hi=0.08))
    st.close()
    lake = DirectoryLake(tmp_path / "lake")
    svc.ingest_run(run, lake, log=lambda m: None)
    ev = svc.operator_evidence(lake)
    assert ev == {("knob_sample", None, None): [0.32], ("llm_rewrite", "m", "optimize"): [0.0]}  # win vs hitchhiker
    monkeypatch.chdir(tmp_path)
    eng = Engine(EngineConfig(name="primed", lake=str(tmp_path / "lake")), target=t)
    eng._lake_priors()
    ctx = ("db",)
    assert eng.bandit.posterior(ctx, ("knob_sample", None, None))[0] > eng.bandit.posterior(ctx, ("llm_rewrite", "m", "optimize"))[0]
    eng.tele.close()
