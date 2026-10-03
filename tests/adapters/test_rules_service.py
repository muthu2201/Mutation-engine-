"""Mining CRL rules from a lake, storing them as hash-chained records, and applying them."""

from colloid.adapters.lake.directory import DirectoryLake
from colloid.core.atlas import StackAtlas
from colloid.core.knobs import KnobSpec
from colloid.core.lake import Record, append, head, verify_chain
from colloid.core.models import Unit, UnitKind
from colloid.services import rules as svc

TABLES = ("orders", "reviews")
QUERIES = {
    "q-summary": "SELECT id FROM orders WHERE customer_id = %s ORDER BY placed_at DESC, id DESC",
    "q-rating": "SELECT count(*) FROM reviews WHERE product_id = %s",
}
SCHEMA = "CREATE TABLE orders (id bigserial PRIMARY KEY, customer_id int);\nCREATE TABLE reviews (id bigserial PRIMARY KEY, product_id int);\n"


def knob(name, ddl):
    return KnobSpec.model_validate({"name": name, "layer": "db", "type": "bool", "default": False, "mechanism": "index",
                                    "key": name, "extra": {"ddl": ddl}})


KNOBS = [knob("db.idx_orders_customer_placed", "CREATE INDEX x1 ON orders (customer_id, placed_at DESC, id DESC)"),
         knob("db.idx_reviews_product", "CREATE INDEX x2 ON reviews (product_id)"),
         knob("db.idx_orders_customer", "CREATE INDEX x3 ON orders (customer_id)")]


def atlas() -> StackAtlas:
    a = StackAtlas()
    for t in TABLES:
        a.add_unit(Unit(id=Unit.make_id(f"table:{t}"), kind=UnitKind.TABLE, layer="db", name=t, symbol_path=f"table:{t}"))
    for qid, sql in QUERIES.items():
        a.add_unit(Unit(id=Unit.make_id(qid), kind=UnitKind.QUERY, layer="db", name=qid, symbol_path=qid, tags={"sql": sql}))
    return a


def gene(name):
    return Record.make("gene", {"locus": {"unit_id": Unit.make_id(f"knob:{name}"), "unit": name, "surface": "index_set"},
                                "payload_kind": "value", "payload": {"value": True}, "explain": f"{name} = True",
                                "provenance": {"operator": "knob_sample"}})


def lake_with_evidence(tmp_path):
    g1, g2, g3 = gene("db.idx_orders_customer_placed"), gene("db.idx_reviews_product"), gene("db.idx_orders_customer")
    prog = Record.make("program", {"target": "shop", "genes": sorted([g1.id, g2.id, g3.id]), "status": "verified", "platform": {},
                                   "effects": {"cost": {"gain_pct": 31.0, "ci_pct": [27.0, 34.0]}},
                                   "attribution": [{"gene": g1.id, "method": "leave_one_out", "value": 0.0778, "ci": [0.0112, 0.1443]},
                                                   {"gene": g2.id, "method": "leave_one_out", "value": 0.3340, "ci": [0.2670, 0.4006]},
                                                   {"gene": g3.id, "method": "leave_one_out", "value": 0.0010, "ci": [-0.0200, 0.0220]}]})
    lake = DirectoryLake(tmp_path / "lake")
    recs = [g1, g2, g3, prog]
    lake.commit(recs, append([], recs, "2026-10-02T00:00:00.000000Z"), head([]), "evidence")
    return lake


def test_mine_commit_load_apply(tmp_path):
    lake = lake_with_evidence(tmp_path)
    rep = svc.mine(lake, atlas_of=lambda target: (atlas(), KNOBS))
    assert sorted(r.name for r in rep.rules) == ["equality-filter-index", "equality-filter-sorted-index"]
    assert {i["index"] for i in rep.instances} == {"db.idx_orders_customer_placed", "db.idx_reviews_product"}  # the hitchhiker is not evidence
    n, _ = svc.commit_rules(lake, rep.rules)
    assert n == 2 and svc.commit_rules(lake, rep.rules)[0] == 0  # idempotent
    verify_chain(lake.entries(), lake.records())
    loaded = svc.load_rules(lake)
    assert {r.rule_id for r in loaded} == {r.rule_id for r in rep.rules}
    mapped = {(m.proposal.rule, m.proposal.render()): m.knob for m in svc.apply(loaded, atlas(), KNOBS, SCHEMA)}
    assert mapped == {
        ("equality-filter-index", "orders (customer_id)"): "db.idx_orders_customer",
        ("equality-filter-index", "reviews (product_id)"): "db.idx_reviews_product",
        ("equality-filter-sorted-index", "orders (customer_id, placed_at desc, id desc)"): "db.idx_orders_customer_placed",
    }
    # with the sorted index already in the genome, the plain orders proposal is covered and disappears
    after = svc.apply(loaded, atlas(), KNOBS, SCHEMA, {"db.idx_orders_customer_placed": True})
    assert ("equality-filter-index", "orders (customer_id)") not in {(m.proposal.rule, m.proposal.render()) for m in after}
