"""Causal profiling (blueprint A1 "Causal leverage", Recommendation 3).

Hotness misleads: perf attributed 0.15% of SQLite runtime to the functions Coz's causal
profiler showed were worth 25.6%. Colloid therefore decorates the Atlas with *causal
leverage* - "if this unit were x% faster, how much faster would the end-to-end objective
get?" - and uses it, not hotness, to allocate mutation budget.

We measure it the way Coz does, with a **virtual speedup** realised as its dual, a real
*slowdown*. For a code unit we inject a controlled delay into the unit (a busy-wait of
``d`` × its own baseline time on every call) and measure how much end-to-end CPU-per-request
rises. The local slowdown fraction vs the end-to-end change traces a line through the
origin whose slope is the leverage: slope 1.0 means the unit is on the critical path and
fully serial; slope ~0 means speeding it up would not help. Injection is done by a
*profiling gene* the evaluator applies to a throwaway workspace - the production code is
never touched, and the delay is calibrated from the unit's own measured per-call time so the
same fraction is comparable across units.

Latency share per endpoint comes directly from the load generator's per-request timings
grouped by request kind; it seeds the request-path weights. Both decorations are written
onto the Atlas as dynamic tags (``causal_leverage``, ``latency_share``, ``hotness``) and a
``LeverageCurve`` per unit, and persisted so a run can reuse them.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.core.atlas import StackAtlas
from colloid.core.genome import Genome
from colloid.core.ids import sha256_hex
from colloid.core.models import Gene, LeverageCurve, PayloadKind, Provenance, Surface, UnitKind
from colloid_evaluator.protocol import Bench, run_loadgen
from colloid_evaluator.workloads import Generator, poisson_schedule

KIND_TO_ENDPOINT = {
    "search": "endpoint:GET /products/search", "product": "endpoint:GET /products/{id}",
    "summary": "endpoint:GET /customers/{id}/summary", "reco": "endpoint:GET /customers/{id}/recommendations",
    "category_top": "endpoint:GET /categories/{id}/top", "daily": "endpoint:GET /reports/daily",
    "order": "endpoint:POST /orders",
}


@dataclass
class ProfileConfig:
    delays: tuple[float, ...] = (0.0, 0.5, 1.0)
    measure_s: float = 5.0
    repeats: int = 2
    top_units: int = 10


@dataclass
class ProfileResult:
    latency_share: dict[str, float]
    hotness: dict[str, float]
    leverage: dict[str, LeverageCurve]
    endpoint_cpu_us: dict[str, float]
    raw: dict[str, Any] = field(default_factory=dict)


def _inject_delay_source(source: str, d: float) -> str | None:
    """Wrap an async def body so it spins for ``d`` × its own runtime after executing.
    Returns None if the function has no simple body to instrument."""
    import ast
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return None
    fn = tree.body[0]
    if not hasattr(fn, "body"):
        return None
    lines = source.splitlines(keepends=True)
    # find body indentation and the first body line (after a possible docstring)
    import ast as _ast

    body0 = fn.body[1] if (len(fn.body) > 1 and isinstance(fn.body[0], _ast.Expr) and isinstance(getattr(fn.body[0], "value", None), _ast.Constant)) else fn.body[0]
    start = body0.lineno - 1
    indent = lines[start][: len(lines[start]) - len(lines[start].lstrip())]

    def reindent(block: str) -> str:
        return "".join(indent + ln if ln.strip() else ln for ln in block.splitlines(keepends=True))

    # Return statements must set the spin deadline first; simplest robust approach: wrap the
    # whole body in an inner function and time it.
    head = "".join(lines[:start])
    inner = "async def _inner():\n" + textwrap.indent("".join(lines[start:]), "    ")
    wrapped = (
        head
        + reindent("import time as _t\n")
        + reindent(inner)
        + reindent("_t0 = _t.perf_counter()\n")
        + reindent("_r = await _inner()\n")
        + reindent(f"_spin = (_t.perf_counter() - _t0) * {d}\n")
        + reindent("_d = _t.perf_counter() + _spin\n")
        + reindent("while _t.perf_counter() < _d:\n")
        + reindent("    pass\n")
        + reindent("return _r\n")
    )
    try:
        ast.parse(wrapped)
    except SyntaxError:
        return None
    return wrapped


class CausalProfiler:
    def __init__(self, target: StackZeroTarget, bench: Bench, cfg: ProfileConfig | None = None) -> None:
        self.target = target
        self.bench = bench
        self.cfg = cfg or ProfileConfig()
        self.universe = bench.universe

    def _delay_gene(self, unit_id: str, d: float) -> Gene | None:
        atlas = self.target.atlas_seed()
        unit = atlas.units[unit_id]
        base = str(unit.tags["baseline_source"])
        new = _inject_delay_source(base, d)
        if new is None or new == base:
            return None
        loc = atlas.locus_for(unit_id, Surface.CODE_REGION)
        payload = {"source": new, "base_hash": sha256_hex(base)[:16], "language": "python", "diff_lines": 0}
        return Gene.make(loc.id, PayloadKind.VALUE if False else PayloadKind.SOURCE, payload, Provenance(operator="profile_delay"))

    def latency_share(self, ws: Genome | Any, seed: int = 0) -> ProfileResult:
        """Measure per-endpoint latency and CPU share on the baseline."""
        baseline_ws = self.bench_ws()
        db = self.target.fresh_db("prof")
        self.target.apply_shared_state(baseline_ws.launch, db)
        svc = self.target.start_service(baseline_ws, db)
        rng = random.Random(seed)
        gen = Generator(self.universe, random.Random(seed ^ 7))
        try:
            n = int(self.bench.rate * self.cfg.measure_s * 2)
            reqs = gen.mixed(n)
            # warm
            run_loadgen(svc.socket, gen.mixed(80), poisson_schedule(80, 40, rng), set(), [svc.cpuacct_path()], window_ms=10000, conns=32, work=self.target.work)
            run = run_loadgen(svc.socket, reqs, poisson_schedule(n, self.bench.rate, rng), set(), [svc.cpuacct_path(), self.target.pg.cpuacct_path()],
                              window_ms=500, conns=32, work=self.target.work)
            lat = run.latency_ms
            by_kind_lat: dict[str, float] = defaultdict(float)
            by_kind_n: dict[str, int] = defaultdict(int)
            for req, ms in zip(reqs, lat, strict=True):
                by_kind_lat[req.kind] += ms
                by_kind_n[req.kind] += 1
            total = sum(by_kind_lat.values()) or 1.0
            share = {k: v / total for k, v in by_kind_lat.items()}
            total_cpu = run.total_cpu_us_per_req
            endpoint_cpu = {KIND_TO_ENDPOINT[k]: share[k] * total_cpu * len(reqs) / max(1, by_kind_n[k]) for k in share}
        finally:
            self.target.stop_service(svc)
            self.target.drop_db(db)
        return ProfileResult(
            latency_share={KIND_TO_ENDPOINT[k]: v for k, v in share.items() if k in KIND_TO_ENDPOINT},
            hotness={}, leverage={}, endpoint_cpu_us=endpoint_cpu,
            raw={"total_cpu_us_per_req": total_cpu, "kind_counts": dict(by_kind_n)},
        )

    def bench_ws(self) -> Any:
        if not hasattr(self, "_bws"):
            self._bws = self.target.materialize(Genome(), self.target.work / "ws-profile-base")
            self.target.build(self._bws)
        return self._bws

    def causal_leverage(self, unit_ids: list[str], log=lambda m: None) -> ProfileResult:
        """Delay-injection leverage curves for ``unit_ids`` (service code units)."""
        baseline_ws = self.bench_ws()
        rng = random.Random(4242)
        gen = Generator(self.universe, random.Random(999))
        n = int(self.bench.rate * self.cfg.measure_s)
        reqs = gen.mixed(n)
        sched = poisson_schedule(n, self.bench.rate, rng)

        def measure(genome: Genome, tag: str) -> float:
            ws = baseline_ws if len(genome) == 0 else self.target.materialize(genome, self.target.work / f"ws-prof-{tag}")
            if len(genome):
                b = self.target.build(ws)
                if not b.ok:
                    return float("nan")
            db = self.target.fresh_db(f"prof{tag[:6]}")
            self.target.apply_shared_state(ws.launch, db)
            svc = self.target.start_service(ws, db)
            try:
                run_loadgen(svc.socket, gen.mixed(60), poisson_schedule(60, 40, rng), set(), [svc.cpuacct_path()], window_ms=10000, conns=32, work=self.target.work)
                vals = []
                for _ in range(self.cfg.repeats):
                    run = run_loadgen(svc.socket, reqs, sched, set(), [svc.cpuacct_path(), self.target.pg.cpuacct_path()], window_ms=10000, conns=32, work=self.target.work)
                    if run.errors:
                        return float("nan")
                    vals.append(run.total_cpu_us_per_req)
                return float(np.median(vals))
            finally:
                self.target.stop_service(svc)
                self.target.drop_db(db)
                if len(genome):
                    import shutil
                    shutil.rmtree(ws.root, ignore_errors=True)

        base_cpu = measure(Genome(), "base")
        log(f"[profile] baseline cpu/req {base_cpu/1000:.1f} ms")
        curves: dict[str, LeverageCurve] = {}
        hotness: dict[str, float] = {}
        for uid in unit_ids:
            points = [(0.0, 0.0)]
            ok = True
            for d in self.cfg.delays:
                if d == 0.0:
                    continue
                gene = self._delay_gene(uid, d)
                if gene is None:
                    ok = False
                    break
                g = Genome.of([gene])
                cpu = measure(g, f"{uid[:8]}-{d}")
                if not np.isfinite(cpu) or base_cpu <= 0:
                    ok = False
                    break
                points.append((d, (cpu - base_cpu) / base_cpu))
            if not ok or len(points) < 2:
                continue
            xs = np.array([p[0] for p in points])
            ys = np.array([p[1] for p in points])
            slope = float(np.sum(xs * ys) / np.sum(xs * xs)) if np.sum(xs * xs) > 0 else 0.0
            # crude slope CI from residuals
            resid = ys - slope * xs
            se = float(np.sqrt(np.sum(resid**2) / max(1, len(xs) - 1)) / np.sqrt(np.sum(xs * xs))) if len(xs) > 2 else abs(slope) * 0.5
            curves[uid] = LeverageCurve(unit_id=uid, workload_id="train", points=tuple(points), slope=max(0.0, slope),
                                        slope_ci=(max(0.0, slope - 1.96 * se), slope + 1.96 * se))
            name = self.target.atlas_seed().units[uid].name
            log(f"[profile] {name:40} leverage {slope:+.2f}")
        return ProfileResult(latency_share={}, hotness=hotness, leverage=curves, endpoint_cpu_us={}, raw={"base_cpu_us": base_cpu})


def decorate_atlas(atlas: StackAtlas, latency: ProfileResult, leverage: ProfileResult) -> None:
    """Write measured leverage / latency share / hotness onto the Atlas as dynamic tags, and
    propagate endpoint latency share down the request paths to the functions on them."""
    for ep_id, share in latency.latency_share.items():
        atlas.set_dynamic(ep_id, "latency_share", share)
    # propagate: each function's latency share = sum of shares of endpoints whose path includes it
    func_share: dict[str, float] = defaultdict(float)
    for path in atlas.paths:
        if not path.unit_ids:
            continue
        ep = path.unit_ids[0]
        share = latency.latency_share.get(ep, 0.0)
        for uid in path.unit_ids[1:]:
            if atlas.units[uid].kind in (UnitKind.FUNCTION, UnitKind.QUERY):
                func_share[uid] = max(func_share[uid], share)
    for uid, share in func_share.items():
        atlas.set_dynamic(uid, "latency_share", share)
        atlas.set_dynamic(uid, "hotness", share)
    for uid, curve in leverage.leverage.items():
        atlas.leverage[uid] = curve
        atlas.set_dynamic(uid, "causal_leverage", curve.slope)
    # dollar share ~ endpoint cpu share
    tot = sum(latency.endpoint_cpu_us.values()) or 1.0
    for ep_id, cpu in latency.endpoint_cpu_us.items():
        atlas.set_dynamic(ep_id, "dollar_share", cpu / tot)
