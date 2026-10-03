"""Cross-implementation transfer from the lake: only carrying genes whose locus means the same
thing on the receiving implementation become seeds; code genes never cross languages."""

import shutil

import pytest

from colloid.adapters.lake.directory import DirectoryLake
from colloid.adapters.target.stackzero_go.adapter import StackZeroGoTarget
from colloid.core.ids import sha256_hex
from colloid.core.lake import Record, append, head
from colloid.core.models import Unit
from colloid.services import lake as svc

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs the Go toolchain (Go Atlas)")


def gene(locus_path: str, surface: str, kind: str, payload: dict, operator: str, language: str | None = None) -> Record:
    return Record.make("gene", {
        "target": "stackzero",
        "locus": {"unit_id": Unit.make_id(locus_path), "unit": locus_path.split(":", 1)[1].split("::")[-1], "symbol_path": locus_path,
                  "surface": surface, "layer": "db", "kind": "knob" if surface != "code_region" else "function", "language": language},
        "payload_kind": kind, "payload": payload, "explain": locus_path, "provenance": {"operator": operator},
    })


@pytest.fixture(scope="module")
def go(tmp_path_factory):
    t = StackZeroGoTarget(observe_system=False, state=tmp_path_factory.mktemp("state"))
    return t, t.atlas_seed()


def lake_with_python_program(tmp_path, attribution):
    index = gene("knob:db.idx_reviews_product", "index_set", "value", {"value": True}, "knob_sample")
    pool = gene("knob:py.pool_max", "knob", "value", {"value": 16}, "knob_perturb")
    code = gene("py:service/shop/search.py::rating_summary", "code_region", "source",
                {"source": "async def rating_summary(db, product_id):\n    return 0, None\n", "base_hash": sha256_hex("x")[:16],
                 "language": "python"}, "llm_rewrite", "python")
    ids = {"index": index.id, "pool": pool.id, "code": code.id}
    program = Record.make("program", {
        "target": "stackzero", "genes": sorted(ids.values()), "status": "verified", "platform": {},
        "effects": {"cost": {"gain_pct": 30.0, "ci_pct": [27.0, 34.0], "p": 0.001}},
        "attribution": [{"gene": ids[k], "method": m, "value": v, "ci": ci} for k, m, v, ci in attribution],
    })
    lake = DirectoryLake(tmp_path / "lake")
    records = [index, pool, code, program]
    entries = append([], records, "2026-10-01T00:00:00.000000Z")
    lake.commit(records, entries, head([]), "test")
    return lake, ids, program


def test_only_carrying_genes_with_the_same_meaning_transfer(tmp_path, go):
    t, atlas = go
    lake, ids, program = lake_with_python_program(tmp_path, [
        ("index", "leave_one_out", 0.30, [0.25, 0.35]),   # carries the gain
        ("code", "leave_one_out", 0.05, [0.01, 0.09]),    # carries too, but code never crosses languages
        ("pool", "shapley", 0.02, [0.01, 0.03]),          # a Python runtime knob: no such locus in Go
    ])
    seeds, skipped = svc.transfer_seeds(lake, atlas, t.knob_name_of_locus(atlas), {k.name: k for k in t.knobs()}, target="stackzero-go")
    assert len(seeds) == 1 and seeds[0].record == program.id and seeds[0].source == "stackzero"
    assert [atlas.units[atlas.loci[g.locus_id].unit_id].name for g in seeds[0].genes] == ["db.idx_reviews_product"]
    assert any("code genes do not transfer" in s for s in skipped)
    assert any("py.pool_max does not mean the same thing" in s for s in skipped)
    # the same lake seeds nothing for the target the evidence came from (that is seeds(), not transfer)
    assert svc.transfer_seeds(lake, atlas, t.knob_name_of_locus(atlas), {}, target="stackzero")[0] == []


def test_a_hitchhiker_never_transfers(tmp_path, go):
    t, atlas = go
    lake, _, _ = lake_with_python_program(tmp_path, [
        ("index", "shapley", 0.30, [0.25, 0.35]),
        ("index", "leave_one_out", 0.00, [-0.02, 0.02]),  # the ablation overrides Shapley: no measurable contribution
    ])
    seeds, skipped = svc.transfer_seeds(lake, atlas, t.knob_name_of_locus(atlas), {k.name: k for k in t.knobs()}, target="stackzero-go")
    assert seeds == [] and any("no gene with a contribution CI above zero" in s for s in skipped)
