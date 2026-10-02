"""Stack Atlas queries + opportunity scoring, and MinHash novelty / tabu basins."""

from colloid.core.atlas import Region, StackAtlas
from colloid.core.models import Edge, EdgeKind, Layer, Mutability, Surface, Unit, UnitKind
from colloid.core.novelty import MinHash, NoveltyFilter, TabuBasins, code_tokens, shingles


def _unit(path, kind, layer, parent=None, tags=None):
    return Unit(id=Unit.make_id(path), kind=kind, layer=layer, name=path, symbol_path=path, parent_id=Unit.make_id(parent) if parent else None, tags=tags or {})


def build_atlas():
    a = StackAtlas()
    a.add_unit(_unit("comp", UnitKind.COMPONENT, Layer.SVC))
    fn = a.add_unit(_unit("fn", UnitKind.FUNCTION, Layer.SVC, "comp", {"risk_class": "B", "mutability": "allowed"}))
    q = a.add_unit(_unit("q", UnitKind.QUERY, Layer.DB, "comp", {"mutability": "frozen"}))
    knob = a.add_unit(_unit("knob", UnitKind.KNOB, Layer.DB, "comp", {"risk_class": "A", "mutability": "allowed", "knob_leverage": 0.5}))
    a.add_edge(Edge(src=fn.id, dst=q.id, kind=EdgeKind.QUERIES))
    a.add_edge(Edge(src=knob.id, dst=q.id, kind=EdgeKind.CONFIGURES))
    a.add_locus(fn.id, Surface.CODE_REGION)
    a.add_locus(knob.id, Surface.KNOB)
    return a, fn, q, knob


def test_ancestry_and_relation():
    a, fn, q, knob = build_atlas()
    assert a.units_related(fn.id, a.unit_by_path("comp").id)
    assert not a.units_related(fn.id, knob.id)


def test_opportunity_uses_leverage_over_hotness():
    a, fn, q, knob = build_atlas()
    a.set_dynamic(fn.id, "hotness", 0.9)
    loc = a.locus_for(fn.id, Surface.CODE_REGION)
    base = a.opportunity(loc.id)
    a.set_dynamic(fn.id, "causal_leverage", 0.2)  # leverage overrides hotness
    assert a.opportunity(loc.id) < base


def test_frozen_locus_zero_opportunity():
    a, fn, q, knob = build_atlas()
    a.add_locus(q.id, Surface.QUERY_HINT)
    loc = a.locus_for(q.id, Surface.QUERY_HINT)
    assert a.opportunity(loc.id) == 0.0


def test_region_selection():
    a, fn, q, knob = build_atlas()
    region = Region("svc", "", lambda u, loc: u.layer == Layer.SVC and loc.surface == Surface.CODE_REGION)
    loci = a.loci_in(region)
    assert len(loci) == 1 and loci[0].unit_id == fn.id


def test_minhash_jaccard_estimates_similarity():
    a = MinHash.of_items([str(i) for i in range(100)], num_perm=128)
    b = MinHash.of_items([str(i) for i in range(50, 150)], num_perm=128)
    # true Jaccard of [0,100) and [50,150) = 50/150 = 0.333
    assert abs(a.jaccard(b) - 1 / 3) < 0.12
    assert a.jaccard(a) == 1.0


def test_minhash_hex_roundtrip():
    m = MinHash.of_code("def f():\n    return 1\n")
    assert MinHash.from_hex(m.to_hex()).slots == m.slots


def test_novelty_filter_rejects_near_duplicate():
    f = NoveltyFilter(threshold=0.9)
    s1 = MinHash.of_code("def f(x):\n    return x + 1\n")
    f.add(s1)
    assert not f.is_novel(s1)  # identical
    s2 = MinHash.of_code("def f(x):\n    y = x\n    return y + 1\n")  # small edit
    assert f.is_novel(s2) or f.max_similarity(s2) < 1.0


def test_tabu_penalty():
    t = TabuBasins(radius=0.8)
    centre = MinHash.of_code("def f():\n    return 42\n")
    t.add(centre)
    assert t.penalty(centre) > 0.9
    far = MinHash.of_code("def g(a, b, c):\n    return a * b * c - 7\n")
    assert t.penalty(far) == 0.0


def test_code_tokens_ignore_comments():
    a = code_tokens("def f():\n    return 1  # hi\n")
    b = code_tokens("def f():\n    return 1\n")
    assert a == b
