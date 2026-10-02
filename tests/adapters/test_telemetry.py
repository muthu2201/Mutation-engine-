"""Telemetry adapter: records round-trip, and caller fields can never break the envelope.

Regression: the Shapley runner emitted ``emit("epistasis", ..., kind="synergy")`` which raised
``TypeError: got multiple values for argument 'kind'`` mid-run and killed a generation. The
envelope keys are now reserved and a colliding caller field is kept under ``field_<name>``."""

from colloid.adapters.telemetry.jsonl import JsonlTelemetry, read_events


def test_roundtrip_and_sequence(tmp_path):
    t = JsonlTelemetry(tmp_path / "e.jsonl", run_id="r1")
    t.emit("a", x=1)
    with t.span("phase", island="db"):
        t.emit("b", y=[1, 2])
    t.close()
    ev = read_events(tmp_path / "e.jsonl")
    assert [e["kind"] for e in ev] == ["a", "phase.start", "b", "phase.end"]
    assert [e["seq"] for e in ev] == [1, 2, 3, 4]
    assert all(e["run"] == "r1" for e in ev)
    assert ev[3]["island"] == "db" and ev[3]["duration_s"] >= 0


def test_reserved_field_names_are_namespaced_not_fatal(tmp_path):
    t = JsonlTelemetry(tmp_path / "e.jsonl", run_id="r1")
    t.emit("epistasis", gene_a="aa", kind="synergy", seq=99, run="spoof", t=0)
    t.close()
    (e,) = read_events(tmp_path / "e.jsonl")
    assert e["kind"] == "epistasis" and e["seq"] == 1 and e["run"] == "r1" and e["t"] > 0
    assert e["field_kind"] == "synergy" and e["field_seq"] == 99 and e["field_run"] == "spoof" and e["field_t"] == 0
    assert t.counters["epistasis"] == 1


def test_truncated_tail_is_tolerated(tmp_path):
    p = tmp_path / "e.jsonl"
    t = JsonlTelemetry(p, run_id="r1")
    t.emit("ok")
    t.close()
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"kind": "half-writ')
    assert [e["kind"] for e in read_events(p)] == ["ok"]
