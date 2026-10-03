"""The language bake-off: one contract, several implementations, one judge.

For every implementation of the StackZero contract:

1. **Conformance** - the evaluator's differential oracle sequence (edge cases, write-then-
   read chains, repeats after writes) against the Python reference, side by side, each on
   its own clone of the template database. Only conforming implementations are measured.
2. **Footprint** - what has to ship besides the operating system (runtime + dependencies +
   application), the number of third-party packages, the time from process start to the
   first healthy response, and the service's memory (PSS) after a warm-up.
3. **Cost and speed at equal load** - all implementations in *one* benchmark comparison: the
   same requests, arrival schedule and database for every arm, interleaved in shuffled order
   over several cycles (the evaluator's protocol), with paired confidence intervals of every
   objective against the Python reference.
4. **Capacity** - the offered-load sweep the evaluator uses to calibrate: the knee rate each
   implementation sustains on the same CPUs (service and database together).

The comparison is of first versions *as written*: the same algorithms, the same N+1 queries,
idiomatic code in each language, no tuning. It measures what a language and its runtime
contribute before any optimisation. That is the starting line for the engine, not a verdict
on the languages.
"""

from __future__ import annotations

import json
import os
import random
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from colloid.adapters.cost.static_prices import StaticPriceCostModel
from colloid.adapters.platform import pss_mb
from colloid.adapters.target import open_target
from colloid.core.genome import Genome
from colloid.core.objectives import gain_percent
from colloid_evaluator.oracles import compare_spot_checks, response_diff, send
from colloid_evaluator.protocol import Arm, Bench, Protocol, calibrate, effect, summary
from colloid_evaluator.workloads import Generator, Universe, oracle_sequence, poisson_schedule

IMPLEMENTATIONS = ("stackzero", "stackzero-go", "stackzero-node", "stackzero-bun")
REFERENCE = "stackzero"
BAKEOFF = Protocol("bakeoff", cycles=4, measure_s=6.0, chunks=6)
CAPACITY_RATES = (20, 40, 60, 90, 130, 180, 240, 320, 420, 560, 720)


def _dir_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p):
                total += os.path.getsize(p)
    return total


def _python_closure() -> dict[str, Any]:
    """Third-party distributions the Python service imports, with their dependency closure."""
    from importlib.metadata import PackageNotFoundError, distribution

    seen: dict[str, int] = {}
    todo = ["psycopg", "psycopg-pool", "uvicorn"]
    while todo:
        name = todo.pop()
        key = name.lower().replace("_", "-")
        if key in seen:
            continue
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            continue
        size = sum(int(f.size or 0) for f in dist.files or [])
        seen[key] = size
        for req in dist.requires or []:
            if "extra ==" in req:
                continue
            dep = req.split(";")[0].split("[")[0].split("<")[0].split(">")[0].split("=")[0].split("!")[0].split("~")[0].strip()
            if dep:
                todo.append(dep)
    return {"packages": sorted(seen), "bytes": sum(seen.values())}


def footprint(name: str, target: Any, ws: Any) -> dict[str, Any]:
    root = ws.root
    if name == "stackzero":
        deps = _python_closure()
        interpreter = Path(sys.base_prefix)
        runtime_bytes = _dir_bytes(interpreter / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}") + os.path.getsize(os.path.realpath(sys.executable))
        app = _dir_bytes(root / "service") + _dir_bytes(root / "native")
        return {"runtime": f"CPython {sys.version.split()[0]} + uvicorn", "runtime_bytes": runtime_bytes, "dependencies": deps["packages"],
                "dependency_bytes": deps["bytes"], "app_bytes": app}
    if name == "stackzero-go":
        binary = root / "bin" / "shop"
        go_sum = (root / "service" / "go.sum").read_text().splitlines()
        modules = sorted({line.split()[0] for line in go_sum if line.strip() and not line.split()[1].endswith("/go.mod")})
        return {"runtime": "Go (static binary)", "runtime_bytes": 0, "dependencies": modules, "dependency_bytes": 0,
                "app_bytes": os.path.getsize(binary)}
    lock = json.loads((root / "service" / "package-lock.json").read_text())
    pkgs = sorted(k.split("node_modules/")[-1] for k in lock["packages"] if k)
    binary = target.runtime_binary()
    return {"runtime": f"{target.runtime} {target.runtime_version()}", "runtime_bytes": os.path.getsize(binary), "dependencies": pkgs,
            "dependency_bytes": _dir_bytes(Path(os.path.realpath(root / "service" / "node_modules"))), "app_bytes": _dir_bytes(root / "service" / "src")}


def startup_and_idle(target: Any, ws: Any, universe: Universe, repeats: int = 5) -> dict[str, Any]:
    """Process start to first healthy response (median of ``repeats``), and the service's own
    memory after 200 requests of the mixed workload."""
    from colloid_evaluator.protocol import run_loadgen

    db = target.fresh_db("startup")
    starts, pss = [], []
    try:
        target.apply_shared_state(ws.launch, db)
        for i in range(repeats):
            t0 = time.monotonic()
            svc = target.start_service(ws, db)
            starts.append((time.monotonic() - t0) * 1000.0)
            try:
                if i == 0:
                    gen = Generator(universe, random.Random(5))
                    reqs = [r for r in gen.mixed(240) if not r.write][:200]
                    run_loadgen(svc.socket, reqs, poisson_schedule(len(reqs), 40, random.Random(6)), set(), [svc.cpuacct_path()],
                                window_ms=10_000, conns=8, work=target.work)
                    pss.append(pss_mb(svc.pids()))
            finally:
                target.stop_service(svc)
    finally:
        target.drop_db(db)
    return {"startup_ms_median": round(statistics.median(starts), 1), "startup_ms": [round(s, 1) for s in starts],
            "service_pss_mb_after_warmup": round(pss[0], 1) if pss else None}


def conformance(ref: Any, ref_ws: Any, impl: Any, impl_ws: Any, universe: Universe, *, seed: int, size: str = "deep") -> dict[str, Any]:
    """The differential oracle sequence against the reference implementation, in lock-step."""
    seq = oracle_sequence(universe, random.Random(seed), size=size)
    ref_db, impl_db = ref.fresh_db("cfref"), impl.fresh_db("cfimpl")
    a = b = None
    mismatches: list[str] = []
    try:
        ref.apply_shared_state(ref_ws.launch, ref_db)
        a = ref.start_service(ref_ws, ref_db)
        b = impl.start_service(impl_ws, impl_db)
        with a.client(timeout=60.0) as ca, b.client(timeout=60.0) as cb:
            for i, req in enumerate(seq):
                sa, ba = send(ca, req)
                sb, bb = send(cb, req)
                diff = response_diff(sa, ba, sb, bb)
                if diff:
                    mismatches.append(f"#{i} {req.method} {req.path} [{req.kind}]: {diff}")
    finally:
        for t, h in ((ref, a), (impl, b)):
            if h is not None:
                t.stop_service(h)
        ref.drop_db(ref_db)
        impl.drop_db(impl_db)
    return {"requests": len(seq), "mismatches": len(mismatches), "first": mismatches[:5]}


def run(implementations: Sequence[str] = IMPLEMENTATIONS, *, rate: float | None = None, cycles: int = BAKEOFF.cycles,
        conformance_seeds: Sequence[int] = (101, 202), capacity: bool = True, log: Callable[[str], None] = print) -> dict[str, Any]:
    cost = StaticPriceCostModel()
    targets = {name: open_target(name) for name in implementations}
    ref = targets[REFERENCE]
    ref.prepare()
    for name, t in targets.items():
        if name != REFERENCE:
            t.prepare()
    with ref.pg.superuser("shop_template") as conn:
        universe = Universe.load(conn)
    wss = {}
    for name, t in targets.items():
        ws = t.materialize(Genome(), t.work / f"ws-bakeoff-{name}")
        b = t.build(ws)
        if not b.ok:
            raise RuntimeError(f"{name} does not build: {b.log[-800:]}")
        wss[name] = ws
    report: dict[str, Any] = {"implementations": {}, "reference": REFERENCE, "protocol": BAKEOFF.name}
    conforming = [REFERENCE]
    for name in implementations:
        entry: dict[str, Any] = {"language": targets[name].language}
        if name != REFERENCE:
            checks = [conformance(ref, wss[REFERENCE], targets[name], wss[name], universe, seed=s) for s in conformance_seeds]
            entry["conformance"] = {"requests": sum(c["requests"] for c in checks), "mismatches": sum(c["mismatches"] for c in checks),
                                    "first": [m for c in checks for m in c["first"]][:5]}
            log(f"[bakeoff] {name}: conformance {entry['conformance']['mismatches']} mismatches in {entry['conformance']['requests']} requests")
            if entry["conformance"]["mismatches"] == 0:
                conforming.append(name)
        entry["footprint"] = footprint(name, targets[name], wss[name])
        entry["footprint"].update(startup_and_idle(targets[name], wss[name], universe))
        log(f"[bakeoff] {name}: footprint {entry['footprint']}")
        report["implementations"][name] = entry
    bench = Bench(ref, universe, cost, rate=rate or 40.0)
    if rate is None:
        cal = calibrate(bench, wss[REFERENCE])
        rate = float(cal["rate_rps"])
        report["reference_calibration"] = {"knee_rps": cal["knee_rps"], "rate_rps": rate}
        bench.rate = rate
    report["rate_rps"] = rate
    protocol = Protocol(BAKEOFF.name, cycles=cycles, measure_s=BAKEOFF.measure_s, chunks=BAKEOFF.chunks)
    arms = [Arm(name, name, wss[name], targets[name]) for name in conforming]
    log(f"[bakeoff] measuring {', '.join(conforming)} at {rate} rps, {cycles} interleaved cycles")
    cmp = bench.compare(arms, protocol, seed=20261002)
    report["failures"] = cmp.failures
    for name in conforming:
        if name in cmp.failures:
            continue
        s = summary(cmp, name, bench.usd_cpu_s, bench.usd_gb_s)
        entry = report["implementations"][name]
        entry["at_equal_load"] = {k: {"point": round(v[0], 4), "ci": [round(v[1], 4), round(v[2], 4)], "n": v[3]} for k, v in s.items()}
        if name != REFERENCE:
            vs = {}
            for obj in ("cost", "cpu", "p50", "p95", "mem"):
                e = effect(cmp, name, REFERENCE, obj, seed=7, usd_cpu_s=bench.usd_cpu_s, usd_gb_s=bench.usd_gb_s)
                vs[obj] = {"gain_pct": round(gain_percent(e.log_ratio), 2), "ci_pct": [round(gain_percent(e.ci_lo), 2), round(gain_percent(e.ci_hi), 2)],
                           "p": round(e.p_value, 5)}
            entry["vs_reference"] = vs
        spot = [p for r, c in cmp.paired(name, REFERENCE) for p in compare_spot_checks(r.spot, c.spot)] if name != REFERENCE else []
        entry["spot_checks_under_load"] = {"mismatches": len(spot), "first": spot[:3]}
    if capacity:
        for name in conforming:
            cb = Bench(targets[name], universe, cost, rate=rate)
            cal = calibrate(cb, wss[name], rates=CAPACITY_RATES)
            report["implementations"][name]["capacity"] = {"knee_rps": cal["knee_rps"], "curve": cal["curve"]}
            log(f"[bakeoff] {name}: knee {cal['knee_rps']} rps")
    report["fingerprint"] = {"cpus": os.cpu_count(), "kernel": os.uname().release,
                             "machine": subprocess.run(["uname", "-m"], capture_output=True, text=True).stdout.strip()}
    ref.shutdown()  # one Postgres cluster serves every implementation
    return report


def scale_projection(report: dict[str, Any], rps_levels: Sequence[int] = (100, 1_000, 10_000), utilisation: float = 0.6) -> dict[str, Any]:
    """Monthly compute cost of serving a sustained load, from each implementation's *measured*
    CPU per request (service + database) and memory, priced with the engine's cost model
    (per vCPU-hour and per GB-hour, the same snapshot every cost number in Colloid uses).

    Assumptions, stated rather than hidden: CPU per request stays what was measured (linear
    scaling across cores and machines; the database scales with the service); capacity is
    provisioned for ``utilisation`` average CPU use; memory is the measured stack PSS per
    serving unit of 3 vCPUs (the benchmark's service + database CPU set)."""
    model = StaticPriceCostModel()
    d = model.describe()
    usd_vcpu_h, usd_gb_h = float(d["usd_per_cpu_second"]) * 3600, float(d["usd_per_gb_second"]) * 3600
    hours = 730.0
    out: dict[str, Any] = {"assumptions": {"utilisation": utilisation, "usd_per_vcpu_hour": round(usd_vcpu_h, 5), "usd_per_gb_hour": round(usd_gb_h, 5),
                                           "price_snapshot": d["snapshot"], "hours_per_month": hours, "unit_vcpus": 3}, "implementations": {}}
    for name, entry in report["implementations"].items():
        load = entry.get("at_equal_load")
        if not load:
            continue
        cpu_s = load["cpu_us_per_req"]["point"] / 1e6
        mem_gb = load["mem_pss_mb"]["point"] / 1024.0
        rows = {}
        for rps in rps_levels:
            vcpus = rps * cpu_s / utilisation
            units = max(1, -(-vcpus // 3))
            usd = vcpus * usd_vcpu_h * hours + units * mem_gb * usd_gb_h * hours
            rows[str(rps)] = {"vcpus": round(vcpus, 1), "serving_units": int(units), "usd_per_month": round(usd, 0)}
        out["implementations"][name] = {"cpu_ms_per_req": round(cpu_s * 1000, 2), "stack_pss_mb": round(mem_gb * 1024, 1), "load": rows}
    return out
