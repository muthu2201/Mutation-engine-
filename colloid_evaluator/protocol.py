"""Benchmark protocol (blueprint D3): interleaved, warmed-up, randomised, whole-tree measurement.

One *comparison* measures several arms (e.g. baseline, parent, child) on the same machine:

1. A fresh benchmark database is cloned from the template (identical starting state).
2. For each **cycle**, a fresh request sequence and Poisson arrival schedule are drawn from
   a hidden seed; every arm in the cycle replays exactly the same sequence (pairing), and
   the *order* of arms is shuffled per cycle (ABAB/ABC interleaving with randomised order
   cancels drift such as thermal state or a noisy neighbour).
3. Each **phase** (one arm, one cycle):
   a. builds the arm with a random link order (setup randomisation, Mytkowicz et al.),
   b. reconciles shared state (kernel knobs, Postgres restart for postmaster GUCs, CPU
      placement, index set) for the arm,
   c. starts the service with a random environment padding (0-4096 bytes, shifts stack
      alignment) and a random hash seed,
   d. **warms up until steady state is detected**, not for a fixed time: one broad chunk of
      fresh requests touches the data, then a fixed probe chunk is replayed until three
      consecutive replays agree within tolerance on CPU per request and median latency (or
      the budget runs out, which is recorded). Replaying the *same* probe is what makes the
      check meaningful: chunks with different request mixes differ by ±30% in CPU per
      request even on a perfectly warm system.
   e. **measures** with the open-loop load generator pinned to CPU 0 while the service and
      database run on CPUs 1-3: per-request latency from the *scheduled* send time
      (coordinated-omission corrected), whole-process-tree CPU from cgroup accounting
      (service + every Postgres backend), peak PSS memory sampled at 5 Hz, and response
      bodies of a hidden subset of reads for spot checks. The measurement is split into
      ``chunks`` that are each *drained* (the load generator returns only when every
      request of the chunk has completed), so the CPU consumed by a chunk is exactly the CPU
      of its requests - no in-flight work leaks across chunk boundaries.
4. Effects are estimated per objective as log-ratios with **paired** statistics: chunk k of
   every arm in a cycle replayed the same requests, so CPU and cost compare chunk-by-chunk
   (ratio of totals, pair-bootstrap CI, exact sign-flip permutation p-value), and latency
   quantiles use a paired bootstrap over request indices. Pairing removes the dominant
   noise source - the random mix of cheap and expensive requests - which an unpaired
   per-window analysis leaves in (±25% CIs instead of a few percent on this target).

Anything that fails during a phase (startup failure, crash, non-2xx responses, timeouts)
fails the arm; failures of the *reference* arm are infrastructure errors, not verdicts.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import random
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from colloid.adapters.platform import capabilities, pin_cpus, pss_mb
from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.adapters.target.stackzero.catalog import LOADGEN_CPUS
from colloid.core.stats import (
    Effect,
    bootstrap_ci,
    paired_quantile_effect,
    paired_ratio_effect,
    steady,
    stratified_bootstrap_ci,
    stratified_bootstrap_log_ratio,
)
from colloid.ports import CostModel, Workspace
from colloid_evaluator.workloads import Generator, Request, Universe, poisson_schedule

LOADGEN = "/opt/colloid/bin/colloid-loadgen"
OK_STATUSES = (200, 201)


class LoadgenTimeout(RuntimeError):
    """The load generator could not finish in time: the candidate served requests slower
    than they arrived (open-loop pile-up). Treated as a measurement failure, not a crash."""


@dataclass(frozen=True)
class Protocol:
    name: str
    cycles: int
    measure_s: float
    warmup_chunk_s: float = 1.0
    warmup_min_chunks: int = 2
    warmup_max_chunks: int = 7
    warmup_tol: float = 0.12  # measured noise of identical probe replays on this host is ~±5% CPU, ±15% p50
    window_ms: int = 500
    chunks: int = 4
    spot_fraction: float = 0.03
    workload: str = "train"
    rate_scale: float = 1.0
    conns: int = 32


L4 = Protocol("L4", cycles=1, measure_s=4.0, chunks=4, warmup_max_chunks=5)
L5 = Protocol("L5", cycles=2, measure_s=5.0, chunks=5)
HOLDOUT = Protocol("L6-holdout", cycles=2, measure_s=5.0, chunks=5, workload="holdout")
SOAK = Protocol("L6-soak", cycles=1, measure_s=20.0, chunks=10, rate_scale=1.3)
SHAPLEY = Protocol("shapley", cycles=1, measure_s=4.0, chunks=4, warmup_max_chunks=5)


@dataclass
class Arm:
    label: str
    program_id: str
    ws: Workspace


@dataclass
class LoadRun:
    latency_ms: np.ndarray
    status: np.ndarray
    end_us: np.ndarray
    cpu_t_us: np.ndarray
    cpu_ns: np.ndarray  # (samples, files)
    bodies: dict[int, bytes]
    summary: dict[str, Any]

    @property
    def errors(self) -> int:
        return int(np.sum(~np.isin(self.status, OK_STATUSES)))

    def cpu_windows(self, min_requests: int = 5) -> tuple[list[float], list[int], list[float]]:
        """Per-window CPU µs per request, completed requests and window seconds."""
        per_req, counts, secs = [], [], []
        total = self.cpu_ns.sum(axis=1)
        for i in range(len(self.cpu_t_us) - 1):
            t0, t1 = self.cpu_t_us[i], self.cpu_t_us[i + 1]
            n = int(np.sum((self.end_us >= t0) & (self.end_us < t1)))
            if n >= min_requests:
                per_req.append(float(total[i + 1] - total[i]) / 1000.0 / n)
                counts.append(n)
                secs.append((t1 - t0) / 1e6)
        return per_req, counts, secs

    @property
    def total_cpu_us_per_req(self) -> float:
        total = self.cpu_ns.sum(axis=1)
        return float(total[-1] - total[0]) / 1000.0 / max(1, len(self.status))


@dataclass
class PhaseResult:
    arm: str
    cycle: int
    ok: bool
    reason: str = ""
    latency_ms: np.ndarray = field(default_factory=lambda: np.zeros(0))
    chunk_cpu_us: list[float] = field(default_factory=list)  # total CPU (svc + db) per chunk
    chunk_requests: list[int] = field(default_factory=list)
    chunk_sched_s: list[float] = field(default_factory=list)  # scheduled duration of each chunk
    pss_mb: list[float] = field(default_factory=list)
    cpu_us_per_req: float = float("nan")
    throughput: float = float("nan")
    warmup_chunks: int = 0
    steady: bool = False
    shared_changes: dict[str, Any] = field(default_factory=dict)
    spot: dict[int, bytes] = field(default_factory=dict)
    duration_s: float = 0.0
    log_tail: str = ""


@dataclass
class Comparison:
    protocol: Protocol
    rate: float
    phases: list[PhaseResult]
    failures: dict[str, str]
    duration_s: float

    def arm_phases(self, label: str) -> list[PhaseResult]:
        return [p for p in self.phases if p.arm == label and p.ok]

    def paired(self, candidate: str, reference: str) -> list[tuple[PhaseResult, PhaseResult]]:
        """Phases of the two arms matched by cycle (same requests, same schedule)."""
        ref = {p.cycle: p for p in self.arm_phases(reference)}
        return [(ref[p.cycle], p) for p in self.arm_phases(candidate) if p.cycle in ref]

    def mem_gb(self, ph: PhaseResult) -> float:
        return (float(np.percentile(ph.pss_mb, 95)) if ph.pss_mb else 0.0) / 1024.0

    def chunk_values(self, ph: PhaseResult, metric: str, usd_cpu_s: float, usd_gb_s: float) -> list[float]:
        if metric == "cpu":
            return list(ph.chunk_cpu_us)
        if metric == "cost":
            mem = self.mem_gb(ph)
            return [c * 1e-6 * usd_cpu_s + mem * s * usd_gb_s for c, s in zip(ph.chunk_cpu_us, ph.chunk_sched_s, strict=True)]
        raise KeyError(metric)


STATISTIC = {"p50": "median", "p95": "p95", "p99": "p99"}


def effect(cmp: Comparison, candidate: str, reference: str, metric: str, seed: int = 0, usd_cpu_s: float = 1.24e-5, usd_gb_s: float = 1.56e-6) -> Effect:
    pairs = cmp.paired(candidate, reference)
    if metric in ("cpu", "cost"):
        ref_v: list[float] = []
        cand_v: list[float] = []
        for r, c in pairs:
            if len(r.chunk_cpu_us) == len(c.chunk_cpu_us):
                ref_v += cmp.chunk_values(r, metric, usd_cpu_s, usd_gb_s)
                cand_v += cmp.chunk_values(c, metric, usd_cpu_s, usd_gb_s)
        return paired_ratio_effect(ref_v, cand_v, seed=seed)
    if metric in ("p50", "p95", "p99"):
        return paired_quantile_effect([r.latency_ms for r, _ in pairs], [c.latency_ms for _, c in pairs], STATISTIC[metric], seed=seed)
    if metric == "mem":
        return stratified_bootstrap_log_ratio([r.pss_mb for r, _ in pairs], [c.pss_mb for _, c in pairs], "p95", seed=seed)
    raise KeyError(metric)


def summary(cmp: Comparison, label: str, usd_cpu_s: float, usd_gb_s: float) -> dict[str, tuple[float, float, float, int]]:
    """Per-arm metric summaries (point, CI lo, CI hi, n) in natural units."""
    ph = cmp.arm_phases(label)
    out: dict[str, tuple[float, float, float, int]] = {}
    if not ph:
        return out
    per_req = [c / n for p in ph for c, n in zip(p.chunk_cpu_us, p.chunk_requests, strict=True) if n]
    cost = [cmp.chunk_values(p, "cost", usd_cpu_s, usd_gb_s)[i] / n * 1e6 for p in ph for i, n in enumerate(p.chunk_requests) if n]
    out["cpu_us_per_req"] = bootstrap_ci(per_req, np.mean)[:3] + (len(per_req),)
    out["usd_per_mreq"] = bootstrap_ci(cost, np.mean)[:3] + (len(cost),)
    p50 = stratified_bootstrap_ci([list(p.latency_ms) for p in ph], "median")
    p95 = stratified_bootstrap_ci([list(p.latency_ms) for p in ph], "p95")
    p99 = stratified_bootstrap_ci([list(p.latency_ms) for p in ph], "p99")
    mem = stratified_bootstrap_ci([p.pss_mb for p in ph], "p95")
    out["latency_p50_ms"], out["latency_p95_ms"], out["latency_p99_ms"], out["mem_pss_mb"] = p50, p95, p99, mem
    out["throughput_rps"] = (float(np.mean([p.throughput for p in ph])), float(min(p.throughput for p in ph)), float(max(p.throughput for p in ph)), len(ph))
    return out


def _pss_mb(pids: Sequence[int]) -> float:
    return pss_mb(pids)  # smaps_rollup PSS on Linux; psutil PSS/USS elsewhere (colloid.adapters.platform)


def _read_cpu(files: Sequence[str]) -> int:
    total = 0
    for f in files:
        with open(f) as fh:
            total += int(fh.read().strip())
    return total


class PssSampler:
    def __init__(self, pid_sources: Sequence[Callable[[], list[int]]], interval: float = 0.2) -> None:
        self.sources = pid_sources
        self.interval = interval
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            pids: list[int] = []
            for src in self.sources:
                with contextlib.suppress(Exception):
                    pids += src()
            self.samples.append(_pss_mb(pids))
            self._stop.wait(self.interval)

    def __enter__(self) -> PssSampler:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def run_loadgen(socket: Path, requests: Sequence[Request], schedule: Sequence[int], sample: set[int], cpu_files: Sequence[str],
                *, window_ms: int, conns: int, timeout_s: float = 15.0, work: Path) -> LoadRun:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", dir=work, delete=False) as fh:
        for i, (req, t) in enumerate(zip(requests, schedule, strict=True)):
            fh.write(req.to_wire(t, i in sample) + "\n")
        req_path = fh.name
    out_path = req_path + ".out"
    try:
        argv = [LOADGEN, "-socket", str(socket), "-requests", req_path, "-conns", str(conns), "-timeout", f"{timeout_s}s",
                "-window-ms", str(window_ms), "-cpu-files", ",".join(cpu_files), "-out", out_path]

        span = (schedule[-1] / 1e6 if schedule else 0) + timeout_s * 4 + 30
        exact_pin = capabilities().cpu_affinity == "sched"  # pin before exec; elsewhere pin right after spawn

        def pin() -> None:
            pin_cpus({int(LOADGEN_CPUS)})

        popen = subprocess.Popen(argv, preexec_fn=pin if exact_pin else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not exact_pin:
            pin_cpus({int(LOADGEN_CPUS)}, popen.pid)
        try:
            stdout, stderr = popen.communicate(timeout=span)
        except subprocess.TimeoutExpired as exc:
            popen.kill()
            popen.communicate()
            raise LoadgenTimeout(f"loadgen exceeded {span:.0f}s (requests queued faster than served)") from exc
        proc = subprocess.CompletedProcess(argv, popen.returncode, stdout, stderr)
        if proc.returncode != 0:
            raise RuntimeError(f"loadgen failed: {proc.stderr[-500:]}")
        rows, cpu_t, cpu_v, bodies, summary = [], [], [], {}, {}
        with open(out_path) as fh:
            for line in fh:
                kind, rest = line[0], line[2:].rstrip("\n")
                if kind == "r":
                    rows.append([int(x) for x in rest.split(",")])
                elif kind == "c":
                    parts = [int(x) for x in rest.split(",")]
                    cpu_t.append(parts[0])
                    cpu_v.append(parts[1:])
                elif kind == "b":
                    idx, b64 = rest.split(",", 1)
                    bodies[int(idx)] = base64.b64decode(b64)
                elif kind == "s":
                    summary = json.loads(rest)
        arr = np.asarray(rows, dtype=np.int64).reshape(-1, 6)
        lat = (arr[:, 3] - arr[:, 1]) / 1000.0
        return LoadRun(lat, arr[:, 4], arr[:, 3], np.asarray(cpu_t, dtype=np.int64), np.asarray(cpu_v, dtype=np.int64).reshape(len(cpu_t), -1), bodies, summary)
    finally:
        for p in (req_path, out_path):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(p)


class Bench:
    """Runs comparisons. Holds no candidate state between comparisons."""

    def __init__(self, target: StackZeroTarget, universe: Universe, cost: CostModel, rate: float) -> None:
        self.target = target
        self.universe = universe
        self.cost = cost
        self.rate = rate
        self.work = target.work
        cm = cost.describe()
        self.usd_cpu_s = float(cm["usd_per_cpu_second"])
        self.usd_gb_s = float(cm["usd_per_gb_second"])

    def _requests(self, gen: Generator, n: int, kinds: list[str] | None) -> list[Request]:
        return gen.restricted(n, kinds) if kinds else gen.mixed(n)

    def compare(self, arms: Sequence[Arm], protocol: Protocol, seed: int, kinds: list[str] | None = None) -> Comparison:
        t0 = time.monotonic()
        rng = random.Random(seed)
        gen = Generator(self.universe, random.Random(seed ^ 0x5EED), holdout=protocol.workload == "holdout")
        rate = self.rate * protocol.rate_scale
        n_measure = max(50, int(rate * protocol.measure_s))
        n_chunk = max(10, int(rate * protocol.warmup_chunk_s))
        bench_db = self.target.fresh_db(f"b{protocol.name.lower().replace('-', '')}")
        phases: list[PhaseResult] = []
        failures: dict[str, str] = {}
        try:
            for cycle in range(protocol.cycles):
                reqs = self._requests(gen, n_measure, kinds)
                sched = poisson_schedule(n_measure, rate, rng)
                reads = [i for i, r in enumerate(reqs) if not r.write]
                spot = set(rng.sample(reads, max(1, int(len(reads) * protocol.spot_fraction)))) if reads else set()
                warm = [self._requests(gen, n_chunk, kinds), [r for r in self._requests(gen, n_chunk, kinds) if not r.write]]
                warm_sched = poisson_schedule(n_chunk, rate, rng)
                order = list(arms)
                rng.shuffle(order)
                for arm in order:
                    if arm.label in failures:
                        continue
                    ph = self._phase(arm, cycle, protocol, bench_db, reqs, sched, spot, warm, warm_sched, rng)
                    phases.append(ph)
                    if not ph.ok:
                        failures[arm.label] = ph.reason
        finally:
            self.target.drop_db(bench_db)
            with contextlib.suppress(Exception):
                self.target.os_layer.restore()
        return Comparison(protocol, rate, phases, failures, time.monotonic() - t0)

    def _phase(self, arm: Arm, cycle: int, protocol: Protocol, db: str, reqs: list[Request], sched: list[int], spot: set[int],
               warm: list[list[Request]], warm_sched: list[int], rng: random.Random) -> PhaseResult:
        t0 = time.monotonic()
        res = PhaseResult(arm=arm.label, cycle=cycle, ok=False)
        build = self.target.build(arm.ws, link_seed=rng.randrange(1_000_000))
        if not build.ok:
            res.reason = f"build failed: {build.log[-500:]}"
            return res
        res.shared_changes = self.target.apply_shared_state(arm.ws.launch, db)
        try:
            svc = self.target.start_service(arm.ws, db, env_pad=rng.randrange(4097), hash_seed=rng.randrange(1 << 16))
        except RuntimeError as exc:
            res.reason = f"service failed to start: {str(exc)[:600]}"
            return res
        cpu_files = [svc.cpuacct_path(), self.target.pg.cpuacct_path()]
        try:
            cpu_hist, lat_hist = [], []
            for i in range(protocol.warmup_max_chunks):
                chunk = warm[0] if i == 0 else warm[1]  # broad pass, then replay the fixed probe
                run = run_loadgen(svc.socket, chunk, warm_sched[: len(chunk)], set(), cpu_files, window_ms=10_000, conns=protocol.conns, work=self.work)
                res.warmup_chunks = i + 1
                if run.errors:
                    res.reason = f"{run.errors} failed requests during warm-up (statuses {sorted(set(run.status.tolist()) - set(OK_STATUSES))[:5]})"
                    return res
                if i == 0:
                    continue
                cpu_hist.append(run.total_cpu_us_per_req)
                lat_hist.append(float(np.median(run.latency_ms)))
                if len(cpu_hist) >= max(3, protocol.warmup_min_chunks) and steady(cpu_hist, 3, protocol.warmup_tol) and steady(lat_hist, 3, protocol.warmup_tol * 2.5):
                    res.steady = True
                    break
            n = len(reqs)
            bounds = [round(k * n / protocol.chunks) for k in range(protocol.chunks + 1)]
            latency = np.zeros(n)
            bodies: dict[int, bytes] = {}
            elapsed = 0.0
            with PssSampler([svc.pids, self.target.pg.pids]) as pss:
                for k in range(protocol.chunks):
                    a, b = bounds[k], bounds[k + 1]
                    if b <= a:
                        continue
                    sub_sched = [t - sched[a] + 1000 for t in sched[a:b]]
                    sub_spot = {i - a for i in spot if a <= i < b}
                    cpu0 = _read_cpu(cpu_files)
                    t_chunk = time.monotonic()
                    run = run_loadgen(svc.socket, reqs[a:b], sub_sched, sub_spot, cpu_files, window_ms=protocol.window_ms,
                                      conns=protocol.conns, work=self.work)
                    elapsed += time.monotonic() - t_chunk
                    cpu1 = _read_cpu(cpu_files)
                    if run.errors:
                        bad = sorted(set(run.status.tolist()) - set(OK_STATUSES))
                        res.reason = f"{run.errors}/{b - a} failed requests under load (statuses {bad[:5]})"
                        return res
                    latency[a:b] = run.latency_ms
                    bodies.update({i + a: body for i, body in run.bodies.items()})
                    res.chunk_cpu_us.append((cpu1 - cpu0) / 1000.0)
                    res.chunk_requests.append(b - a)
                    res.chunk_sched_s.append((b - a) / self.rate / protocol.rate_scale)
            if not svc.proc.alive():
                res.reason = "service died during measurement"
                return res
            res.pss_mb = list(pss.samples)
            res.latency_ms = latency
            res.cpu_us_per_req = sum(res.chunk_cpu_us) / max(1, sum(res.chunk_requests))
            res.throughput = n / max(elapsed, 1e-9)
            res.spot = bodies
            res.ok = len(res.chunk_cpu_us) == protocol.chunks
            if not res.ok:
                res.reason = "incomplete measurement"
            return res
        finally:
            _out, err = self.target.stop_service(svc)
            res.log_tail = err[-1500:]
            res.duration_s = time.monotonic() - t0


def calibrate(bench: Bench, ws: Workspace, rates: Sequence[float] = (20, 40, 60, 90, 130, 180, 240, 320), seconds: float = 4.0) -> dict[str, Any]:
    """Offered-load sweep on the baseline to find the knee; the benchmark rate is half of it.
    Returns the curve so the stress report can show the saturation behaviour."""
    target = bench.target
    db = target.fresh_db("calib")
    curve = []
    try:
        target.build(ws)
        target.apply_shared_state(ws.launch, db)
        svc = target.start_service(ws, db)
        gen = Generator(bench.universe, random.Random(1234))
        rng = random.Random(99)
        try:
            # warm-up
            reqs = gen.mixed(100)
            run_loadgen(svc.socket, reqs, poisson_schedule(100, 30, rng), set(), [svc.cpuacct_path()], window_ms=10_000, conns=32, work=bench.work)
            base_p99 = None
            for r in rates:
                n = int(r * seconds)
                reqs = gen.mixed(n)
                run = run_loadgen(svc.socket, reqs, poisson_schedule(n, r, rng), set(), [svc.cpuacct_path(), target.pg.cpuacct_path()],
                                  window_ms=500, conns=64, work=bench.work)
                elapsed = run.summary.get("elapsed_us", 1) / 1e6
                p50, p99 = float(np.median(run.latency_ms)), float(np.percentile(run.latency_ms, 99))
                base_p99 = base_p99 or p99
                point = {"offered": r, "achieved": n / elapsed, "p50_ms": p50, "p99_ms": p99, "errors": run.errors,
                         "cpu_us_per_req": run.total_cpu_us_per_req}
                curve.append(point)
                if run.errors or p99 > 8 * base_p99 or n / elapsed < 0.9 * r:
                    break
        finally:
            target.stop_service(svc)
    finally:
        target.drop_db(db)
    good = [p for p in curve if not p["errors"] and p["p99_ms"] <= 8 * curve[0]["p99_ms"] and p["achieved"] >= 0.9 * p["offered"]]
    knee = good[-1]["offered"] if good else rates[0]
    return {"knee_rps": knee, "rate_rps": max(10.0, round(knee * 0.5, 1)), "curve": curve}
