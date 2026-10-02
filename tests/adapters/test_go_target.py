"""The Go implementation as a Colloid target: Go code representation, Atlas, knobs, launch
configuration and LLM-response parsing. No database or sandbox needed."""

import shutil

import pytest

from colloid.adapters.target.stackzero_go.adapter import StackZeroGoTarget
from colloid.adapters.target.stackzero_go.catalog import launch_config
from colloid.core.models import EdgeKind, UnitKind
from colloid.ports import CodeUnit
from colloid.services.lake import knob_fingerprint
from colloid_evaluator.policy import judge_violations

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="needs the Go toolchain")


@pytest.fixture(scope="module")
def go_target(tmp_path_factory):
    t = StackZeroGoTarget(observe_system=False, state=tmp_path_factory.mktemp("state"))
    return t, t.atlas_seed()


def test_units_span_source_and_sql(go_target):
    t, _ = go_target
    units = {u.name: u for u in t.go_code.units(t.root, "service/handlers.go")}
    detail = units["productDetail"]
    assert detail.source.startswith("func productDetail(") and detail.source.rstrip().endswith("}")
    assert len(detail.sql) == 4 and all("$1" in q for q in detail.sql)
    assert "fetchRow" in detail.calls and "fetch" in detail.calls  # generic calls resolve to the function name


def test_splice_composes_and_refuses_extra_code(go_target):
    t, _ = go_target
    text = (t.root / "service" / "search.go").read_text()
    unit = next(u for u in t.go_code.units(t.root, "service/search.go") if u.name == "ratingSummary")
    new = unit.source.replace("productID)", "productID) // spliced", 1)
    out = t.go_code.replace(text, CodeUnit(unit.symbol_path, unit.name, unit.file, 0, 0, "", unit.source, "go"), new)
    assert out.count("// spliced") == 1 and len(out) == len(text) + len(" // spliced")
    with pytest.raises(ValueError):
        t.go_code.replace(text, CodeUnit(unit.symbol_path, unit.name, unit.file, 0, 0, "", unit.source, "go"), new + "\nfunc extra() {}\n")


def test_atlas_paths_cover_every_endpoint_and_share_the_database(go_target):
    _, atlas = go_target
    assert len(atlas.paths) == 7
    assert judge_violations(atlas) == []
    queries = [u for u in atlas.units.values() if u.kind == UnitKind.QUERY]
    assert queries and all("$" not in u.tags["sql"] for u in queries)  # placeholders normalised: one query unit per statement
    idx = atlas.unit_by_path("knob:db.idx_reviews_product")
    configured = {e.dst for e in atlas.out_edges(idx.id, EdgeKind.CONFIGURES)}
    assert any(atlas.units[q].kind == UnitKind.QUERY for q in configured)
    summary = atlas.unit_by_path("endpoint:GET /customers/{id}/summary")
    path = next(p for p in atlas.paths if p.unit_ids[0] == summary.id)
    layers = {atlas.units[u].layer.value for u in path.unit_ids}
    assert {"svc", "db"} <= layers


def test_query_units_are_the_same_units_as_the_python_implementations():
    pytest.importorskip("colloid.adapters.code.c_clang")
    if shutil.which("clang") is None:
        pytest.skip("the Python implementation's Atlas needs clang")
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget

    py = {u.symbol_path for u in StackZeroTarget(observe_system=False).atlas_seed().units.values() if u.kind == UnitKind.QUERY}
    go = {u.symbol_path for u in StackZeroGoTarget(observe_system=False).atlas_seed().units.values() if u.kind == UnitKind.QUERY}
    assert go and go == py  # the faithful port issues exactly the same statements


def test_shared_knobs_mean_the_same_thing_and_language_layers_differ(go_target):
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget

    t, _ = go_target
    py = {k.name: k for k in StackZeroTarget.catalog()}
    go = {k.name: k for k in t.knobs()}
    shared = set(py) & set(go)
    assert {"db.idx_reviews_product", "db.work_mem_kb", "os.cpu_layout"} <= shared
    assert all(knob_fingerprint(py[n]) == knob_fingerprint(go[n]) for n in shared)
    assert not any(n.startswith(("py.", "alloc.", "cc.")) for n in go)
    assert {"go.gogc", "go.exec_mode", "go.amd64"} <= set(go)
    assert go["go.no_bounds_checks"].mutability == "frozen"


def test_launch_config_from_knob_values(go_target):
    t, _ = go_target
    base = launch_config(t.knobs(), {})
    assert base["env"] == {} and base["indexes"] == {} and base["go_build"] == {"GOAMD64": "v1", "flags": []}
    cfg = launch_config(t.knobs(), {"go.gogc": 400, "go.maxprocs": "auto", "go.exec_mode": "exec", "db.idx_orders_customer": True,
                                    "db.work_mem_kb": 8192, "go.amd64": "v3"})
    assert cfg["env"] == {"GOGC": "400", "SHOP_EXEC_MODE": "exec"}  # "auto" leaves GOMAXPROCS to the runtime
    assert list(cfg["indexes"]) == ["colloid_idx_orders_customer"] and cfg["pg_session"] == {"work_mem": "8192kB"}
    assert cfg["go_build"]["GOAMD64"] == "v3"


def test_llm_answers_are_parsed_by_the_go_parser(go_target):
    t, atlas = go_target
    unit = atlas.unit_by_path("go:service/search.go::ratingSummary")
    base = str(unit.tags["baseline_source"])
    better = base.replace("row, _, err :=", "row, _, err :=  ")
    answer = f"Sure:\n```go\nimport \"context\"\n\n{better}```\n"
    ok = t.parse_response(answer, base, unit)
    assert ok.ok and "err :=  " in ok.source
    assert not t.parse_response(answer.replace("productID int64)", "id int64)"), base, unit).ok
    with_global = t.parse_response(answer.replace(better, better + "\nvar cache = map[int64]int64{}\n"), base, unit)
    assert not with_global.ok and "extra top-level" in with_global.reason
    assert t.parse_response("no code here", base, unit).reason == "no code block in response"


def test_profiler_probe_is_armed_at_function_entry(go_target):
    t, atlas = go_target
    unit = atlas.unit_by_path("go:service/handlers.go::categoryTop")
    probed = t.probe_source(unit.id, 0.4)
    assert probed is not None and "{\n\tdefer colloidProbe(0.4)()\n" in probed
