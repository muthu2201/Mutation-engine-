"""Post-run verification: give a finished run's best programs a fair L6 and a precise number.

During a run, L6 deep assurance only fires on a *new global best*, and the measurement that
crowned it is a two-cycle L5. After the run, ``colloid verify RUN`` does two things for the top
of the archive:

1. **L6 for every eligible program**, using the run's own A/A noise floor. A program is eligible
   only if it never had an L6, or every L6 it had failed *because the evaluator broke* (an
   ERROR verdict, or a sanitizer runtime that crashed). A program that already failed L6 for a
   candidate reason, such as a holdout gain that did not persist or an oracle mismatch, is
   never re-tested. Re-testing failures until one passes is p-hacking.
2. **A high-replication re-measure vs baseline** (``--cycles`` independent ABAB cycles; the
   between-run noise floor shrinks as 1/sqrt(cycles)), which becomes the headline estimate.

A program is **promoted** only if it passes L6, its holdout gain survives a Holm-Bonferroni
correction across everything tested in this batch (family-wise error at alpha), its replicate
cost CI excludes zero, *and* the run's A/A calibration gate passed. A program that passes L6
but misses one of the other conditions is marked ``verified``. Everything is written back to
the run's store: the L6 evaluations, the status changes, and a ``verification`` record that
the report and the results renderer read.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from colloid.adapters.cost.static_prices import StaticPriceCostModel
from colloid.adapters.store.sql_store import open_store
from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.core.genome import Genome
from colloid.core.models import Evaluation, ProgramStatus, Stage, Verdict
from colloid.core.objectives import gain_percent
from colloid.services.report import _store_url
from colloid_evaluator import oracles
from colloid_evaluator.cascade import OBJECTIVES, Evaluator
from colloid_evaluator.protocol import Protocol

ALPHA = 0.05


def _cost_vs_baseline(ev: Evaluation) -> float | None:
    est = ev.objective("cost", "baseline")
    return est.log_ratio if est is not None else None


def _infra_only(l6s: list[Evaluation]) -> bool:
    """True when every L6 the program had failed through no fault of its own."""
    return all(e.verdict == Verdict.ERROR or any(oracles.sanitizer_infrastructure_failure(r) for r in e.reasons) for e in l6s)


def select(store: Any, top: int, program_ids: list[str] | None) -> list[tuple[str, float]]:
    """Eligible programs ranked by their latest L5 cost gain vs baseline (best first)."""
    rows: list[tuple[str, float]] = []
    for prog in store.programs():
        if program_ids and prog.id not in program_ids:
            continue
        if prog.status in (ProgramStatus.PROMOTED, ProgramStatus.REJECTED, ProgramStatus.FAILED) or not prog.gene_ids:
            continue
        evs = store.evaluations(prog.id)
        l6 = [e for e in evs if e.stage == Stage.L6]
        if l6 and not _infra_only(l6):
            continue
        l5 = [x for x in (_cost_vs_baseline(e) for e in evs if e.stage == Stage.L5) if x is not None]
        if l5 and (program_ids or l5[-1] > 0):
            rows.append((prog.id, l5[-1]))
    rows.sort(key=lambda r: -r[1])
    return rows if program_ids else rows[:top]


def holm(pvalues: dict[str, float], alpha: float = ALPHA) -> dict[str, bool]:
    """Holm-Bonferroni step-down: which hypotheses are rejected at family-wise level alpha."""
    order = sorted(pvalues, key=lambda k: pvalues[k])
    m = len(order)
    out = dict.fromkeys(pvalues, False)
    for i, k in enumerate(order):
        if pvalues[k] > alpha / (m - i):
            break
        out[k] = True
    return out


def verify_run(run: str, *, top: int = 5, cycles: int = 6, program_ids: list[str] | None = None,
               log: Callable[[str], None] = print) -> dict[str, Any]:
    store = open_store(_store_url(run))
    try:
        return _verify(store, run, top, cycles, program_ids, log)
    finally:
        store.close()


def _verify(store: Any, run: str, top: int, cycles: int, program_ids: list[str] | None, log: Callable[[str], None]) -> dict[str, Any]:
    setup = store.kv_get("setup") or {}
    aa = store.kv_get("aa_test") or {}
    baseline = next(p for p in store.programs(island="baseline"))
    chosen = select(store, top, program_ids)
    if not chosen:
        log("no eligible programs (all were promoted, rejected, or already failed L6 for a candidate reason)")
        return {"programs": []}
    replicate = Protocol(f"replicate-x{cycles}", cycles=cycles, measure_s=5.0, chunks=5)
    ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=setup.get("rate_rps"), log=log)
    ev.setup(baseline.id)
    ev.noise_floor = {k: float(v) for k, v in (aa.get("noise_floor_per_cycle") or {}).items()}
    gate_ok = bool(aa.get("promotions_allowed"))
    log(f"verifying {len(chosen)} program(s); noise floor per cycle: "
        + ", ".join(f"{k}={v * 100:.2f}%" for k, v in ev.noise_floor.items()) + f"; A/A gate {'passed' if gate_ok else 'FAILED'}")
    records: dict[str, dict[str, Any]] = {}
    t0 = time.monotonic()
    try:
        for pid, l5_gain in chosen:
            prog = store.get_program(pid)
            genome = Genome.of(store.genes(prog.gene_ids), ev.atlas)
            if genome.program_id(baseline.id) != pid:
                raise RuntimeError(f"{pid}: genes do not reproduce the program id")
            rec: dict[str, Any] = {"program": pid, "island": prog.island, "operator": prog.operator, "genes": len(genome),
                                   "l5_cost_gain_pct": round(gain_percent(l5_gain), 2)}
            l1 = ev.l1(pid, genome)
            if not l1.passed or l1.ws is None:
                rec.update(l6="error", reasons=list(l1.evaluation.reasons)[:2])
                records[pid] = rec
                continue
            log(f"[{pid[:10]}] L6 deep assurance (deep oracle + sanitized fuzz, hidden holdout, soak)")
            r6 = ev.l6(pid, genome, l1.ws)
            store.put_evaluation(r6.evaluation)
            rec["l6"] = r6.evaluation.verdict.value
            rec["l6_reasons"] = list(r6.evaluation.reasons)[:3]
            hc = r6.evaluation.objective("cost", "baseline")  # L6 reports the hidden-holdout estimates
            if hc is not None:
                rec["holdout"] = {"gain_pct": round(gain_percent(hc.log_ratio), 2),
                                  "ci_pct": [round(gain_percent(hc.ci_lo), 2), round(gain_percent(hc.ci_hi), 2)], "p": hc.p_value}
            if r6.passed:
                log(f"[{pid[:10]}] replicate vs baseline, {cycles} cycles")
                rr = ev._comparison_stage(Stage.L6, replicate, pid, genome, l1.ws, [("baseline", baseline.id, Genome())], None)
                rep = {}
                for obj in OBJECTIVES:
                    est = rr.evaluation.objective(obj, "baseline")
                    if est is not None:
                        rep[obj] = {"gain_pct": round(gain_percent(est.log_ratio), 2),
                                    "ci_pct": [round(gain_percent(est.ci_lo), 2), round(gain_percent(est.ci_hi), 2)], "p": round(est.p_value, 5)}
                rec["replicate"] = rep
                rec["replicate_verdict"] = rr.evaluation.verdict.value
                usd = rr.evaluation.metrics.get("usd_per_mreq"), rr.evaluation.metrics.get("baseline.usd_per_mreq")
                if usd[0] and usd[1]:
                    rec["usd_per_mreq"] = {"candidate": round(usd[0].median, 4), "baseline": round(usd[1].median, 4)}
            records[pid] = rec
            log(f"[{pid[:10]}] {rec}")
        # family-wise correction over every program tested in this batch (failures count toward m)
        pvals = {pid: (r["holdout"]["p"] if r.get("holdout") else 1.0) for pid, r in records.items()}
        survives = holm(pvals)
        for pid, rec in records.items():
            rep_cost = rec.get("replicate", {}).get("cost")
            rec["holm_survives"] = survives[pid]
            rec["replicate_confirms"] = bool(rep_cost and rep_cost["ci_pct"][0] > 0)
            if rec.get("l6") != Verdict.PASS.value:
                rec["decision"] = "not promoted (L6 " + str(rec.get("l6")) + ")"
                continue
            if survives[pid] and rec["replicate_confirms"] and gate_ok:
                store.set_status(pid, ProgramStatus.PROMOTED)
                rec["decision"] = "promoted"
            else:
                store.set_status(pid, ProgramStatus.VERIFIED)
                why = [w for w, bad in (("holdout gain does not survive Holm correction", not survives[pid]),
                                        ("replicate CI includes zero", not rec["replicate_confirms"]),
                                        ("A/A gate closed", not gate_ok)) if bad]
                rec["decision"] = "verified, not promoted: " + "; ".join(why)
    finally:
        ev.shutdown()
    out = {"run": run, "cycles": cycles, "alpha": ALPHA, "gate_passed": gate_ok, "elapsed_min": round((time.monotonic() - t0) / 60, 1),
           "noise_floor_per_cycle": ev.noise_floor, "tau_per_replicate": {k: v / math.sqrt(cycles) for k, v in ev.noise_floor.items()},
           "programs": list(records.values())}
    store.kv_set("verification", out)
    return out
