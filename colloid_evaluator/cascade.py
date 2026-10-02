"""The evaluation cascade (blueprint A8): each stage passes only survivors on.

=====  ==========================================================  ====================
Stage  What it does                                                Typical cost
=====  ==========================================================  ====================
L0     static policy and validity (policy.py)                      milliseconds
L1     materialise workspace (apply genes) + sandboxed build        < 1 s (cached)
L2     unit tests + differential oracle vs baseline                ~10 s
       (+ native differential fuzz when C code / flags change)
L3     surrogate rank - lives on the search side (core.surrogate)  milliseconds
L4     micro-benchmark: parent vs child on the touched paths only  ~25 s
L5     macro-benchmark: baseline vs parent vs child, full mix       ~60 s
L6     deep assurance for elites: deep oracle, sanitizer fuzzing,   ~2 min
       hidden holdout workload, soak test
=====  ==========================================================  ====================

Verdicts: ``PASS``; ``FAIL`` (the candidate's fault - never retried); ``ERROR`` (evaluator or
infrastructure problem, e.g. the *baseline* arm failed - the engine may retry);
``SUSPICIOUS`` (passed, but a gain above the suspicion threshold triggers mandatory L6 and an
alert - blueprint D2.7: ">2× on a mature path" is far more often a measurement or evaluator
bug than a real win).

Every random choice inside the evaluator (oracle sequences, request parameters, arrival
schedules, spot-check indices, link orders, environment padding) comes from fresh seeds
drawn from the OS CSPRNG, so nothing about a future evaluation can be predicted from a past
one.
"""

from __future__ import annotations

import contextlib
import math
import secrets
import shutil
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from colloid.adapters.platform import pin_cpus
from colloid.adapters.target.stackzero.adapter import GeneApplyError, StackZeroTarget
from colloid.core.genome import Genome
from colloid.core.ids import content_hash
from colloid.core.models import (
    Evaluation,
    Measured,
    MetricSummary,
    ObjectiveEstimate,
    PayloadKind,
    Stage,
    UnitKind,
    Verdict,
)
from colloid.core.stats import calibrate_aa, combine_effects_se, with_noise_floor
from colloid.ports import CostModel, Workspace
from colloid_evaluator import oracles, policy
from colloid_evaluator.fingerprint import fingerprint
from colloid_evaluator.protocol import (
    HOLDOUT,
    L4,
    L5,
    SHAPLEY,
    SOAK,
    Arm,
    Bench,
    Comparison,
    Protocol,
    calibrate,
    effect,
    summary,
)
from colloid_evaluator.workloads import Universe

OBJECTIVES = ("cost", "cpu", "p50", "p95", "mem")
METRIC_UNITS = {
    "cpu_us_per_req": "µs", "usd_per_mreq": "USD/1M req", "latency_p50_ms": "ms", "latency_p95_ms": "ms",
    "latency_p99_ms": "ms", "mem_pss_mb": "MB", "throughput_rps": "req/s",
}
SUSPICION_LOG_RATIO = math.log(2.0)


def fresh_seed() -> int:
    return secrets.randbits(48)


@dataclass
class StageResult:
    evaluation: Evaluation
    ws: Workspace | None = None
    comparison: Comparison | None = None

    @property
    def passed(self) -> bool:
        return self.evaluation.verdict in (Verdict.PASS, Verdict.SUSPICIOUS)


class Evaluator:
    def __init__(self, target: StackZeroTarget, cost: CostModel, *, rate: float | None = None, log: Callable[[str], None] | None = None,
                 max_workspaces: int = 24) -> None:
        self.target = target
        self.cost = cost
        self.rate = rate
        self.log = log or (lambda msg: None)
        self.universe: Universe | None = None
        self.bench: Bench | None = None
        self.baseline_program_id = "baseline"
        self._baseline_ws: Workspace | None = None
        self._ws_cache: OrderedDict[str, Workspace] = OrderedDict()
        self.max_workspaces = max_workspaces
        self.calibration: dict[str, Any] | None = None
        self.pinned = False  # whether the OS let us pin the evaluator/loadgen CPU (recorded in the fingerprint)
        atlas = target.atlas_seed()
        self.atlas = atlas
        self.knobs = {k.name: k for k in target.knobs()}
        self.knob_of_locus = target.knob_name_of_locus(atlas)
        self.oracle: oracles.DifferentialOracle | None = None
        # Between-run SD of each objective's log-ratio for ONE benchmark cycle, measured by the
        # A/A test. Empty until measured, i.e. CIs are within-run only.
        self.noise_floor: dict[str, float] = {}

    # ------------------------------------------------------------------ setup
    def setup(self, baseline_program_id: str) -> dict[str, Any]:
        # the evaluator and load generator share CPU 0; the target owns 1-3 (recorded if the OS cannot pin)
        self.pinned = pin_cpus({0})
        self.target.prepare()
        with self.target.pg.superuser("shop_template") as conn:
            self.universe = Universe.load(conn)
        self.baseline_program_id = baseline_program_id
        self._baseline_ws = self.target.materialize(Genome(), self.target.work / "ws-baseline")
        build = self.target.build(self._baseline_ws)
        if not build.ok:
            raise RuntimeError(f"baseline does not build: {build.log}")
        self.bench = Bench(self.target, self.universe, self.cost, rate=self.rate or 40.0)
        if self.rate is None:
            self.calibration = calibrate(self.bench, self._baseline_ws)
            self.rate = float(self.calibration["rate_rps"])
            self.bench.rate = self.rate
        self.oracle = oracles.DifferentialOracle(self.target, self.universe, self._baseline_ws)
        return {"rate_rps": self.rate, "calibration": self.calibration, "fingerprint": {**fingerprint(), "evaluator_pinned": self.pinned}}

    @property
    def baseline_ws(self) -> Workspace:
        assert self._baseline_ws is not None, "call setup() first"
        return self._baseline_ws

    def shutdown(self) -> None:
        with contextlib.suppress(Exception):
            self.target.shutdown()

    # ------------------------------------------------------------------ helpers
    def _evaluation(self, program_id: str, stage: Stage, protocol: str, verdict: Verdict, reasons: list[str] | tuple[str, ...] = (),
                    metrics: Mapping[str, MetricSummary] | None = None, objectives: tuple[ObjectiveEstimate, ...] = (),
                    duration: float = 0.0, cost_usd: float = 0.0, raw: dict[str, Any] | None = None) -> Evaluation:
        env = dict(fingerprint())
        if raw:
            env["details"] = raw
        return Evaluation(
            id=content_hash("eval", program_id, stage.value, protocol, time.time_ns()),
            program_id=program_id, stage=stage, protocol_id=protocol, verdict=verdict, reasons=tuple(reasons),
            metrics=dict(metrics or {}), objectives=objectives, env_fingerprint=env, duration_s=duration, cost_usd=cost_usd,
        )

    def workspace(self, program_id: str, genome: Genome) -> Workspace:
        if program_id == self.baseline_program_id or len(genome) == 0:
            return self.baseline_ws
        if program_id in self._ws_cache:
            self._ws_cache.move_to_end(program_id)
            return self._ws_cache[program_id]
        ws = self.target.materialize(genome, self.target.work / f"ws-{program_id}")
        self._ws_cache[program_id] = ws
        while len(self._ws_cache) > self.max_workspaces:
            _, old = self._ws_cache.popitem(last=False)
            shutil.rmtree(old.root, ignore_errors=True)
        return ws

    def touches_native(self, genome: Genome) -> bool:
        for g in genome:
            unit = self.atlas.units[self.atlas.loci[g.locus_id].unit_id]
            if unit.tags.get("language") == "c" or (unit.kind == UnitKind.KNOB and unit.tags.get("mechanism") == "cflag"):
                return True
        return False

    # ------------------------------------------------------------------ L0
    def l0(self, program_id: str, genome: Genome) -> StageResult:
        t0 = time.monotonic()
        verdict = policy.check_genome(genome, self.atlas, self.knobs, self.knob_of_locus)
        v = Verdict.PASS if verdict.ok else Verdict.FAIL
        reasons = list(verdict.reasons) + [f"warning: {w}" for w in verdict.warnings]
        return StageResult(self._evaluation(program_id, Stage.L0, "policy-v1", v, reasons, duration=time.monotonic() - t0))

    # ------------------------------------------------------------------ L1
    def l1(self, program_id: str, genome: Genome) -> StageResult:
        t0 = time.monotonic()
        try:
            ws = self.workspace(program_id, genome)
        except GeneApplyError as exc:
            return StageResult(self._evaluation(program_id, Stage.L1, "build-v1", Verdict.FAIL, [str(exc)], duration=time.monotonic() - t0))
        build = self.target.build(ws, link_seed=fresh_seed())
        if not build.ok:
            self._ws_cache.pop(program_id, None)
            return StageResult(self._evaluation(program_id, Stage.L1, "build-v1", Verdict.FAIL, [build.log[-1500:]], duration=time.monotonic() - t0))
        raw = {"artifact": build.artifact_hash, "cached": build.cached}
        return StageResult(self._evaluation(program_id, Stage.L1, "build-v1", Verdict.PASS, raw=raw, duration=time.monotonic() - t0), ws)

    # ------------------------------------------------------------------ L2
    def l2(self, program_id: str, genome: Genome, ws: Workspace, *, size: str = "quick") -> StageResult:
        t0 = time.monotonic()
        assert self.oracle is not None
        reasons: list[str] = []
        raw: dict[str, Any] = {}
        has_code = any(g.payload_kind == PayloadKind.SOURCE for g in genome)
        if has_code or self.touches_native(genome):
            ok, out = self.target.run_unit_tests(ws)
            raw["unit_tests"] = out.strip().splitlines()[-1] if out.strip() else ""
            if not ok:
                reasons.append(f"unit tests failed: {out[-800:]}")
        if not reasons and self.touches_native(genome):
            ok, out = oracles.native_fuzz(self.target.sandbox, self.baseline_ws, ws, self.target.work, seed=fresh_seed(),
                                          iterations=3000 if size == "quick" else 20000, sanitize=size != "quick")
            raw["native_fuzz"] = out.strip().splitlines()[-1] if out.strip() else ""
            if not ok and oracles.sanitizer_infrastructure_failure(out):
                return StageResult(self._evaluation(program_id, Stage.L2 if size == "quick" else Stage.L6, f"oracle-{size}", Verdict.ERROR,
                                                    [f"fuzz infrastructure (sanitizer runtime failed, not a finding): {out[-600:]}"],
                                                    raw=raw, duration=time.monotonic() - t0), ws)
            if not ok:
                reasons.append(out[-1200:])
        if not reasons:
            try:
                res = self.oracle.run(ws, seed=fresh_seed(), size=size)
            except RuntimeError as exc:
                return StageResult(self._evaluation(program_id, Stage.L2, f"oracle-{size}", Verdict.ERROR, [f"oracle infrastructure: {exc}"],
                                                    duration=time.monotonic() - t0))
            raw["oracle_requests"] = res.requests
            if not res.ok:
                reasons += [f"differential oracle: {m}" for m in res.mismatches]
                if res.log:
                    raw["candidate_log"] = res.log[-1500:]
        verdict = Verdict.PASS if not reasons else Verdict.FAIL
        stage = Stage.L2 if size == "quick" else Stage.L6
        return StageResult(self._evaluation(program_id, stage, f"oracle-{size}", verdict, reasons, raw=raw, duration=time.monotonic() - t0), ws)

    # ------------------------------------------------------------------ L4 / L5
    def noise_tau(self, objective: str, protocol: Protocol) -> float:
        """Between-run SD for a comparison made with ``protocol``: cycles are independent runs of
        the arms, so the per-cycle variance component averages down as 1/cycles."""
        return self.noise_floor.get(objective, 0.0) / math.sqrt(max(1, protocol.cycles))

    def _estimates(self, cmp: Comparison, cand: str, ref: str, ref_program: str, reference: str, protocol: Protocol) -> list[ObjectiveEstimate]:
        assert self.bench is not None
        out = []
        for i, obj in enumerate(OBJECTIVES):
            e = effect(cmp, cand, ref, obj, seed=i, usd_cpu_s=self.bench.usd_cpu_s, usd_gb_s=self.bench.usd_gb_s)
            e = with_noise_floor(e, self.noise_tau(obj, protocol))
            out.append(ObjectiveEstimate(objective=obj, reference=reference, reference_program_id=ref_program, log_ratio=e.log_ratio,
                                         ci_lo=e.ci_lo, ci_hi=e.ci_hi, p_value=e.p_value, n_candidate=e.n_candidate, n_reference=e.n_reference))
        return out

    def _metrics(self, cmp: Comparison, label: str) -> dict[str, MetricSummary]:
        assert self.bench is not None
        out = {}
        for name, (point, lo, hi, n) in summary(cmp, label, self.bench.usd_cpu_s, self.bench.usd_gb_s).items():
            out[name] = MetricSummary(median=point, ci_lo=lo, ci_hi=hi, n=n, unit=METRIC_UNITS.get(name, ""))
        return out

    def _spot_failures(self, cmp: Comparison, cand: str, ref: str) -> list[str]:
        problems = []
        for r, c in cmp.paired(cand, ref):
            problems += oracles.compare_spot_checks(r.spot, c.spot)
        return problems[:5]

    def _comparison_stage(self, stage: Stage, protocol: Protocol, program_id: str, genome: Genome, ws: Workspace,
                          refs: list[tuple[str, str, Genome]], kinds: list[str] | None) -> StageResult:
        """``refs``: (label, program id, genome) reference arms, e.g. [("parent", pid, g)]."""
        assert self.bench is not None
        t0 = time.monotonic()
        arms = [Arm("child", program_id, ws)]
        for label, pid, g in refs:
            if pid != program_id and all(a.program_id != pid for a in arms):
                arms.append(Arm(label, pid, self.workspace(pid, g)))
        cmp = self.bench.compare(arms, protocol, seed=fresh_seed(), kinds=kinds)
        reasons: list[str] = []
        raw: dict[str, Any] = {"rate_rps": cmp.rate, "phases": [
            {"arm": p.arm, "cycle": p.cycle, "ok": p.ok, "warmup_chunks": p.warmup_chunks, "steady": p.steady,
             "cpu_us_per_req": round(p.cpu_us_per_req, 1), "shared_changes": p.shared_changes, "reason": p.reason[:300]}
            for p in cmp.phases], "kinds": kinds}
        for label, reason in cmp.failures.items():
            if label != "child":
                return StageResult(self._evaluation(program_id, stage, protocol.name, Verdict.ERROR, [f"reference arm '{label}' failed: {reason}"],
                                                    raw=raw, duration=time.monotonic() - t0), ws, cmp)
        if "child" in cmp.failures:
            reasons.append(f"candidate failed under load: {cmp.failures['child']}")
            log_tail = next((p.log_tail for p in cmp.phases if p.arm == "child" and not p.ok), "")
            if log_tail:
                raw["candidate_log"] = log_tail
            return StageResult(self._evaluation(program_id, stage, protocol.name, Verdict.FAIL, reasons, raw=raw,
                                                duration=time.monotonic() - t0), ws, cmp)
        objectives: list[ObjectiveEstimate] = []
        for label, pid, _ in refs:
            ref_label = next((a.label for a in arms if a.program_id == pid), None)
            if ref_label is None:  # candidate *is* the reference (e.g. re-measuring the parent)
                continue
            spot = self._spot_failures(cmp, "child", ref_label)
            if spot:
                reasons += [f"spot check vs {label}: {s}" for s in spot]
            objectives += self._estimates(cmp, "child", ref_label, pid, label, protocol)
        if reasons:
            return StageResult(self._evaluation(program_id, stage, protocol.name, Verdict.FAIL, reasons, raw=raw,
                                                duration=time.monotonic() - t0), ws, cmp)
        verdict = Verdict.PASS
        for est in objectives:
            if est.reference == "parent" and est.log_ratio > SUSPICION_LOG_RATIO:
                verdict = Verdict.SUSPICIOUS
                reasons.append(f"suspicion: {est.objective} improved {math.exp(est.log_ratio):.2f}x vs parent (threshold 2x) - deep review required")
        metrics = self._metrics(cmp, "child")
        for label, _, _ in refs:
            for name, m in self._metrics(cmp, next((a.label for a in arms if a.label == label), "")).items():
                metrics[f"{label}.{name}"] = m
        cost_usd = sum(p.cpu_us_per_req * 1e-6 * sum(p.chunk_requests) for p in cmp.phases if p.ok) * self.bench.usd_cpu_s
        return StageResult(self._evaluation(program_id, stage, protocol.name, verdict, reasons, metrics=metrics, objectives=tuple(objectives),
                                            raw=raw, duration=time.monotonic() - t0, cost_usd=cost_usd), ws, cmp)

    def l4(self, program_id: str, genome: Genome, ws: Workspace, parent_id: str, parent: Genome) -> StageResult:
        kinds = policy.touched_endpoints(genome, self.atlas) if len(parent) == 0 else None
        return self._comparison_stage(Stage.L4, L4, program_id, genome, ws, [("parent", parent_id, parent)], kinds)

    def l5(self, program_id: str, genome: Genome, ws: Workspace, parent_id: str, parent: Genome) -> StageResult:
        refs = [("baseline", self.baseline_program_id, Genome())]
        if parent_id != self.baseline_program_id:
            refs.append(("parent", parent_id, parent))
        else:
            refs.append(("parent", parent_id, parent))
        return self._comparison_stage(Stage.L5, L5, program_id, genome, ws, refs, None)

    # ------------------------------------------------------------------ L6
    def l6(self, program_id: str, genome: Genome, ws: Workspace) -> StageResult:
        """Deep assurance: deep oracle (+ sanitizer fuzzing), hidden holdout workload, soak."""
        t0 = time.monotonic()
        reasons: list[str] = []
        raw: dict[str, Any] = {}
        deep = self.l2(program_id, genome, ws, size="deep")
        raw["deep_oracle"] = {"verdict": deep.evaluation.verdict.value, "reasons": list(deep.evaluation.reasons)[:5]}
        if not deep.passed:
            return StageResult(self._evaluation(program_id, Stage.L6, "deep", deep.evaluation.verdict, ["deep oracle: " + r for r in deep.evaluation.reasons],
                                                raw=raw, duration=time.monotonic() - t0), ws)
        holdout = self._comparison_stage(Stage.L6, HOLDOUT, program_id, genome, ws, [("baseline", self.baseline_program_id, Genome())], None)
        raw["holdout"] = {"verdict": holdout.evaluation.verdict.value, "reasons": list(holdout.evaluation.reasons)[:5]}
        if holdout.evaluation.verdict in (Verdict.FAIL, Verdict.ERROR):
            return StageResult(self._evaluation(program_id, Stage.L6, "deep", holdout.evaluation.verdict, ["holdout: " + r for r in holdout.evaluation.reasons],
                                                raw=raw, duration=time.monotonic() - t0), ws)
        cost_est = holdout.evaluation.objective("cost", "baseline")
        persists = cost_est is not None and cost_est.ci_lo > 0
        raw["holdout_cost"] = None if cost_est is None else {"log_ratio": cost_est.log_ratio, "ci": [cost_est.ci_lo, cost_est.ci_hi], "p": cost_est.p_value}
        if not persists:
            reasons.append("holdout: cost gain does not persist on the hidden holdout workload (CI lower bound <= 0)")
        soak_ok, soak_info = self._soak(program_id, ws)
        raw["soak"] = soak_info
        if not soak_ok:
            reasons.append(f"soak: {soak_info.get('reason')}")
        verdict = Verdict.PASS if not reasons else Verdict.FAIL
        return StageResult(self._evaluation(program_id, Stage.L6, "deep", verdict, reasons, metrics=holdout.evaluation.metrics,
                                            objectives=holdout.evaluation.objectives, raw=raw, duration=time.monotonic() - t0), ws)

    def _soak(self, program_id: str, ws: Workspace) -> tuple[bool, dict[str, Any]]:
        assert self.bench is not None
        cmp = self.bench.compare([Arm("child", program_id, ws)], SOAK, seed=fresh_seed())
        if cmp.failures:
            return False, {"reason": cmp.failures.get("child", "failed")}
        ph = cmp.arm_phases("child")[0]
        pss = np.asarray(ph.pss_mb)
        if len(pss) < 10:
            return True, {"samples": len(pss)}
        t = np.arange(len(pss)) * 0.2
        slope = float(np.polyfit(t, pss, 1)[0])  # MB per second
        info = {"pss_start_mb": float(pss[:5].mean()), "pss_end_mb": float(pss[-5:].mean()), "slope_mb_per_s": slope,
                "cpu_us_per_req": ph.cpu_us_per_req, "requests": sum(ph.chunk_requests)}
        if slope > 0.5:
            info["reason"] = f"memory grows {slope:.2f} MB/s under sustained load (leak suspected)"
            return False, info
        return True, info

    # ------------------------------------------------------------------ Shapley subsets
    def measure_vs_baseline(self, program_id: str, genome: Genome) -> tuple[Measured | None, str]:
        """Gain of a sub-genome versus baseline (SHAPLEY protocol), with standard error."""
        l1 = self.l1(program_id, genome)
        if not l1.passed or l1.ws is None:
            return None, "build failed"
        res = self._comparison_stage(Stage.L4, SHAPLEY, program_id, genome, l1.ws, [("baseline", self.baseline_program_id, Genome())], None)
        if not res.passed:
            return None, "; ".join(res.evaluation.reasons)[:300]
        est = res.evaluation.objective("cost", "baseline")
        if est is None:
            return None, "no estimate"
        return Measured(est.log_ratio, combine_effects_se(est.ci_lo, est.ci_hi)), ""

    # ------------------------------------------------------------------ A/A
    def aa_test(self, runs: int, alpha: float = 0.05, on_run: Callable[[int, dict[str, Any]], None] | None = None,
                *, apply: bool = True) -> dict[str, Any]:
        """Identical programs compared with the promotion protocol (L5), ``runs`` times.

        Each run's own p-value gives the *raw* false-positive rate of within-run CIs. The spread
        of the A/A effects across runs then measures the between-run variance component tau^2
        (see :func:`colloid.core.stats.calibrate_aa`); with ``apply`` it becomes this evaluator's
        noise floor, widening every later comparison's CI. Calibration is checked out-of-sample
        (leave-one-out) and promotions are allowed only if an exact binomial test cannot reject
        "calibrated false-positive rate <= alpha" for the primary objective (cost)."""
        assert self.bench is not None
        effects: dict[str, list[float]] = {o: [] for o in OBJECTIVES}
        ses: dict[str, list[float]] = {o: [] for o in OBJECTIVES}
        pvals: dict[str, list[float]] = {o: [] for o in OBJECTIVES}
        failed = 0
        for i in range(runs):
            cmp = self.bench.compare([Arm("A", "baseline", self.baseline_ws), Arm("B", "baseline", self.baseline_ws)], L5, seed=fresh_seed())
            if cmp.failures:
                failed += 1
                continue
            row = {}
            for j, obj in enumerate(OBJECTIVES):
                e = effect(cmp, "B", "A", obj, seed=j, usd_cpu_s=self.bench.usd_cpu_s, usd_gb_s=self.bench.usd_gb_s)
                effects[obj].append(e.log_ratio)
                ses[obj].append(combine_effects_se(e.ci_lo, e.ci_hi))
                pvals[obj].append(e.p_value)
                row[obj] = {"log_ratio": e.log_ratio, "ci": [e.ci_lo, e.ci_hi], "p": e.p_value}
            if on_run:
                on_run(i, row)
        n = len(effects["cost"])
        report: dict[str, Any] = {"runs": n, "failed_runs": failed, "alpha": alpha, "protocol": L5.name, "objectives": {}}
        floor: dict[str, float] = {}
        for obj in OBJECTIVES:
            cal = calibrate_aa(effects[obj], ses[obj], pvals[obj], alpha) if n else {}
            ci_cover = sum(1 for e, se in zip(effects[obj], ses[obj], strict=True) if abs(e) <= 1.959964 * se)
            report["objectives"][obj] = {
                "false_positive_rate": cal.get("raw_fpr", float("nan")),
                "false_positives": int(cal.get("raw_fp", 0)),
                "ci_coverage_of_zero": ci_cover / n if n else float("nan"),
                "effect_sd": float(np.std(effects[obj], ddof=1)) if n > 1 else float("nan"),
                "mean_effect": float(np.mean(effects[obj])) if n else float("nan"),
                "median_within_se": cal.get("median_within_se", float("nan")),
                "tau": cal.get("tau", 0.0),
                "calibrated_false_positive_rate": cal.get("calibrated_fpr", float("nan")),
                "calibrated_false_positives": int(cal.get("calibrated_fp", 0)),
                "binomial_p": cal.get("binomial_p", float("nan")),
            }
            # tau was measured on L5 comparisons (L5.cycles cycles each); store it per cycle.
            floor[obj] = float(cal.get("tau", 0.0)) * math.sqrt(L5.cycles)
        primary = report["objectives"]["cost"]
        report["noise_floor_per_cycle"] = floor
        report["promotions_allowed"] = bool(n >= 5 and primary["binomial_p"] >= alpha)
        report["gate"] = (f"cost: {primary['calibrated_false_positives']}/{n} calibrated false positives "
                          f"(raw {primary['false_positives']}/{n}), binomial p={primary['binomial_p']:.3f} vs alpha={alpha}"
                          if n else "no successful A/A runs")
        if apply:
            self.noise_floor = floor
        return report
