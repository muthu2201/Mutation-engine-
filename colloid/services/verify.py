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


def recheck_breaches(run: str, log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Re-adjudicate a finished run's red-team breach alerts with the liveness probe.

    For each flagged program the hack is identified from its gene, the *maximal* version of the
    same hack is built on the same unit's baseline source, and it is sent through L0-L2. Caught
    means the channel is live and the breach is genuine. Passing means the attack was inert.
    The verdicts are stored under ``redteam_recheck``. The original alerts are left as they
    were, because the store is an audit log."""
    import random

    from colloid.core.models import Provenance
    from colloid.core.operators.base import code_gene
    from colloid.core.operators.redteam import infer_hack, redteam_variant

    store = open_store(_store_url(run))
    out: list[dict[str, Any]] = []
    ev: Evaluator | None = None
    try:
        flagged = [a.program_id for a in store.alerts() if a.kind == "redteam_breach" and a.program_id]
        if not flagged:
            log("no red-team breach alerts in this run")
            return out
        setup = store.kv_get("setup") or {}
        baseline = next(p for p in store.programs(island="baseline"))
        ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=setup.get("rate_rps"), log=log)
        ev.setup(baseline.id)
        rng = random.Random(0)
        for pid in flagged:
            prog = store.get_program(pid)
            attack = [g for g in store.genes(prog.gene_ids) if g.provenance.operator == "redteam"]
            rec: dict[str, Any] = {"program": pid}
            if not attack:
                rec["verdict"] = "unknown (no red-team gene)"
                out.append(rec)
                continue
            gene = attack[0]
            unit = ev.atlas.units[ev.atlas.loci[gene.locus_id].unit_id]
            hack = infer_hack(str(gene.payload.get("source", "")))
            base_src = str(unit.tags["baseline_source"])
            rec.update(unit=unit.name, hack=hack, on_request_path=bool(ev.atlas.paths_through(unit.id)))
            maximal = redteam_variant(base_src, hack, rng, maximal=True) if hack else None
            if maximal is None:
                rec["verdict"] = "unknown (could not build the maximal variant)"
                out.append(rec)
                continue
            g = code_gene(gene.locus_id, unit, base_src, maximal, Provenance(operator="redteam", notes=f"maximal liveness probe: {hack}"))
            genome = Genome.of([g], ev.atlas)
            mpid = genome.program_id(baseline.id)
            r0 = ev.l0(mpid, genome)
            r1 = ev.l1(mpid, genome) if r0.passed else None
            if r1 is None or not r1.passed or r1.ws is None:
                rec["verdict"] = "unknown (maximal variant stopped before the oracle)"
            else:
                r2 = ev.l2(mpid, genome, r1.ws)
                if r2.evaluation.verdict == Verdict.ERROR:
                    rec["verdict"] = "unknown (oracle infrastructure error)"
                elif r2.passed:
                    rec["verdict"] = "inert"
                    rec["detail"] = "the maximal variant also passes the oracle: the tampering never reaches a response"
                else:
                    rec["verdict"] = "genuine breach"
                    rec["detail"] = "maximal variant caught: " + (r2.evaluation.reasons or ("",))[0][:200]
            log(f"[{pid[:10]}] {rec}")
            out.append(rec)
        store.kv_set("redteam_recheck", out)
        return out
    finally:
        if ev is not None:
            ev.shutdown()
        store.close()


def ablate(run: str, program_id: str, *, cycles: int = 6, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Leave-one-gene-out ablation of a verified program: what is its gain made of?

    For each gene g, the program without g is checked for correctness (L1 build + L2 oracle;
    removing a gene must not make the program wrong) and measured against baseline with the
    same replicate protocol and noise floor as ``verify``. The contribution of g *in context* is
    gain(full) - gain(full without g), with independent-measurement SEs combined. A gene whose
    contribution CI includes zero is a hitchhiker: it costs review effort and risk and buys
    nothing measurable. The minimal program keeps only the genes with a positive contribution
    CI, and it is measured too, so the claim "these genes carry the gain" is tested directly
    rather than inferred."""
    from colloid.core.stats import combine_effects_se

    store = open_store(_store_url(run))
    ev: Evaluator | None = None
    try:
        setup = store.kv_get("setup") or {}
        aa = store.kv_get("aa_test") or {}
        ver = {r["program"]: r for r in (store.kv_get("verification") or {}).get("programs", [])}
        baseline = next(p for p in store.programs(island="baseline"))
        prog = store.get_program(program_id)
        if prog is None:
            raise SystemExit(f"unknown program {program_id}")
        ev = evr = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=setup.get("rate_rps"), log=log)
        evr.setup(baseline.id)
        evr.noise_floor = {k: float(v) for k, v in (aa.get("noise_floor_per_cycle") or {}).items()}
        replicate = Protocol(f"replicate-x{cycles}", cycles=cycles, measure_s=5.0, chunks=5)
        genes = store.genes(prog.gene_ids)
        atlas = store.get_atlas()
        from colloid.services.report import explain_gene

        def measure(genome: Genome, label: str) -> dict[str, Any]:
            pid = genome.program_id(baseline.id)
            r1 = evr.l1(pid, genome)
            if not r1.passed or r1.ws is None:
                return {"label": label, "status": "build failed"}
            r2 = evr.l2(pid, genome, r1.ws)
            if not r2.passed:
                return {"label": label, "status": "incorrect without this gene", "reason": (r2.evaluation.reasons or ("",))[0][:160]}
            log(f"[ablate] measuring {label} ({len(genome)} genes), {cycles} cycles")
            rr = evr._comparison_stage(Stage.L6, replicate, pid, genome, r1.ws, [("baseline", baseline.id, Genome())], None)
            est = rr.evaluation.objective("cost", "baseline")
            if est is None:
                return {"label": label, "status": "measurement failed", "reason": (rr.evaluation.reasons or ("",))[0][:160]}
            return {"label": label, "status": "ok", "log_ratio": est.log_ratio, "se": combine_effects_se(est.ci_lo, est.ci_hi),
                    "gain_pct": round(gain_percent(est.log_ratio), 2),
                    "ci_pct": [round(gain_percent(est.ci_lo), 2), round(gain_percent(est.ci_hi), 2)]}

        full_rep = (ver.get(program_id) or {}).get("replicate", {}).get("cost")
        full = measure(Genome.of(genes, evr.atlas), "full program") if not full_rep else None
        if full_rep:  # reuse the verify replicate (same protocol, same noise floor)
            lo, hi = (math.log(1 / (1 - c / 100)) for c in full_rep["ci_pct"])
            lr = math.log(1 / (1 - full_rep["gain_pct"] / 100))
            full = {"label": "full program", "status": "ok (from verify)", "log_ratio": lr, "se": combine_effects_se(lo, hi),
                    "gain_pct": full_rep["gain_pct"], "ci_pct": full_rep["ci_pct"]}
        assert full is not None
        rows = []
        for g in genes:
            rest = Genome.of([x for x in genes if x.id != g.id], evr.atlas)
            m = measure(rest, f"without {g.id[:8]}")
            row: dict[str, Any] = {"gene": g.id[:8], "explain": explain_gene(store, g.id, atlas), "without": m}
            if m["status"] == "ok" and "log_ratio" in full:
                d = full["log_ratio"] - m["log_ratio"]
                se = math.sqrt(full["se"] ** 2 + m["se"] ** 2)
                row["contribution_log"] = round(d, 4)
                row["contribution_ci_log"] = [round(d - 1.959964 * se, 4), round(d + 1.959964 * se, 4)]
                row["carries_gain"] = d - 1.959964 * se > 0
            elif m["status"].startswith("incorrect"):
                row["carries_gain"] = True  # load-bearing for correctness: cannot be dropped
            rows.append(row)
            log(f"[ablate] {row}")
        keep = [g for g, r in zip(genes, rows, strict=True) if r.get("carries_gain")]
        minimal = measure(Genome.of(keep, evr.atlas), f"minimal ({len(keep)} genes)") if keep and len(keep) < len(genes) else None
        out = {"program": program_id, "cycles": cycles, "full": full, "genes": rows, "minimal": minimal,
               "minimal_genes": [explain_gene(store, g.id, atlas) for g in keep]}
        store.kv_set("ablation", out)
        return out
    finally:
        if ev is not None:
            ev.shutdown()
        store.close()
