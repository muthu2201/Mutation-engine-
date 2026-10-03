"""Bake-off pieces that need no running stack: the TypeScript targets' knobs and launch
configuration, and the cost-at-scale projection."""

from colloid.adapters.target import TARGETS, target_class
from colloid.adapters.target.stackzero_ts.adapter import launch_config, load_ts_knobs
from colloid.services.bakeoff import scale_projection


def test_typescript_targets_are_registered_with_shared_database_loci_only():
    for name in ("stackzero-node", "stackzero-bun"):
        assert name in TARGETS and target_class(name).language == "typescript"
    knobs = {k.name for k in load_ts_knobs(observe_system=False)}
    assert "db.idx_reviews_product" in knobs and "os.cpu_layout" in knobs
    assert not any(k.startswith(("py.", "go.", "alloc.", "cc.")) for k in knobs)


def test_typescript_launch_config():
    knobs = load_ts_knobs(observe_system=False)
    base = launch_config(knobs, {})
    assert base["env"] == {} and base["indexes"] == {} and base["svc_cpus"] == "1-3"
    cfg = launch_config(knobs, {"db.idx_orders_customer": True, "db.random_page_cost": 1.5})
    assert list(cfg["indexes"]) == ["colloid_idx_orders_customer"] and cfg["pg_session"] == {"random_page_cost": "1.5"}


def test_scale_projection_is_linear_in_measured_cpu_and_states_its_assumptions():
    report = {"implementations": {
        "a": {"at_equal_load": {"cpu_us_per_req": {"point": 30_000.0}, "mem_pss_mb": {"point": 600.0}}},
        "b": {"at_equal_load": {"cpu_us_per_req": {"point": 15_000.0}, "mem_pss_mb": {"point": 600.0}}},
        "c": {"conformance": {"mismatches": 3}},  # not measured: not projected
    }}
    out = scale_projection(report, rps_levels=(1000,))
    a, b = out["implementations"]["a"]["load"]["1000"], out["implementations"]["b"]["load"]["1000"]
    assert a["vcpus"] == 50.0 and b["vcpus"] == 25.0  # 1000 rps x 30 ms / 0.6 utilisation
    assert a["usd_per_month"] > b["usd_per_month"] > 0
    assert "c" not in out["implementations"] and out["assumptions"]["utilisation"] == 0.6
