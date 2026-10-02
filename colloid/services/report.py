"""Summarise a finished (or in-flight) run from its program store and telemetry.

Produces a plain dict with: run config and setup (rate, calibration, fingerprint), cascade
funnel counts per stage, the Pareto front of promoted/elite programs with their per-objective
gains and human-readable gene explanations, bandit arm credit, measured epistasis, the A/A
noise floor and canary gate, and any alerts. The dashboard and the results document are both
built from this.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from colloid.adapters.store.sql_store import open_store
from colloid.adapters.telemetry.jsonl import read_events
from colloid.core.objectives import gain_percent


def _store_url(run: str) -> str:
    if run.startswith(("sqlite:", "postgres")):
        return run
    p = Path(run)
    if p.is_dir():
        p = p / "colloid.db"
    if not str(p).endswith(".db"):
        p = Path("runs") / run / "colloid.db"
    return f"sqlite:///{p}"


def explain_gene(store: Any, gene_id: str, atlas: Any) -> str:
    g = store.get_gene(gene_id)
    if g is None:
        return gene_id
    loc = atlas.loci.get(g.locus_id)
    unit = atlas.units.get(loc.unit_id) if loc else None
    name = unit.symbol_path if unit else g.locus_id
    if g.payload_kind.value == "value":
        return f"{unit.name if unit else name} = {g.value!r} [{g.provenance.operator}]"
    note = g.provenance.notes or g.provenance.template or ""
    return f"{unit.name if unit else name}: code rewrite ({g.provenance.operator}{'/' + g.provenance.template if g.provenance.template else ''}) {note}"[:160]


def build_report(run: str) -> dict[str, Any]:
    store = open_store(_store_url(run))
    try:
        return _build(store, run)
    finally:
        store.close()


def _build(store: Any, run: str) -> dict[str, Any]:
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget

    atlas = store.get_atlas() or StackZeroTarget(observe_system=False).atlas_seed()
    counts = store.count_programs()
    evals = store.evaluations()
    by_stage_verdict: dict[str, Counter] = defaultdict(Counter)
    for ev in evals:
        by_stage_verdict[ev.stage.value][ev.verdict.value] += 1
    funnel = {stage: dict(c) for stage, c in sorted(by_stage_verdict.items())}

    promoted = store.programs(status=__import__("colloid.core.models", fromlist=["ProgramStatus"]).ProgramStatus.PROMOTED)
    verified = store.programs(status=__import__("colloid.core.models", fromlist=["ProgramStatus"]).ProgramStatus.VERIFIED)
    elites = store.programs(status=__import__("colloid.core.models", fromlist=["ProgramStatus"]).ProgramStatus.ELITE)

    def program_summary(prog: Any) -> dict[str, Any]:
        best = None
        for ev in store.evaluations(prog.id):
            if ev.stage.value in ("L5", "L6") and ev.objective("cost", "baseline"):
                best = ev
        row: dict[str, Any] = {"id": prog.id, "island": prog.island, "generation": prog.generation, "operator": prog.operator,
                               "status": prog.status.value, "genes": [explain_gene(store, g, atlas) for g in prog.gene_ids]}
        if best is not None:
            row["gains_pct"] = {}
            for e in best.objectives:
                if e.reference == "baseline":
                    row["gains_pct"][e.objective] = {"pct": round(gain_percent(e.log_ratio), 2),
                                                     "ci_pct": [round(gain_percent(e.ci_hi), 2), round(gain_percent(e.ci_lo), 2)], "p": round(e.p_value, 4)}
            row["stage"] = best.stage.value
            if best.metrics.get("usd_per_mreq") and best.metrics.get("baseline.usd_per_mreq"):
                row["usd_per_mreq"] = {"baseline": round(best.metrics["baseline.usd_per_mreq"].median, 4), "candidate": round(best.metrics["usd_per_mreq"].median, 4)}
        return row

    seen: set[str] = set()
    best_programs = []
    for prog in promoted + verified + elites:
        if prog.id in seen:
            continue
        seen.add(prog.id)
        best_programs.append(program_summary(prog))

    def _cost_gain(row: dict[str, Any]) -> float:
        g = row.get("gains_pct", {}).get("cost")
        return g["pct"] if g else -1e9

    # promoted first, then L6-verified (promotion held by the A/A gate), then by measured cost gain
    rank = {"promoted": 0, "verified": 1}
    best_programs.sort(key=lambda r: (rank.get(r["status"], 2), -_cost_gain(r)))

    epistasis = [{"gene_a": r.gene_a[:8], "gene_b": r.gene_b[:8], "epsilon": round(r.epsilon, 4), "ci": [round(r.ci_lo, 4), round(r.ci_hi, 4)],
                  "kind": "synergy" if r.epsilon > 0 else "interference"} for r in store.epistasis()]
    llm = store.llm_calls()
    llm_summary = {"calls": len(llm), "tokens_in": sum(c.tokens_in for c in llm), "tokens_out": sum(c.tokens_out for c in llm),
                   "cost_usd": round(sum(c.cost_usd for c in llm), 4), "by_model": dict(Counter(c.model for c in llm))}
    result = store.kv_get("result") or {}
    setup = store.kv_get("setup") or {}
    aa = store.kv_get("aa_test")
    profile = store.kv_get("profile")
    events = read_events(Path(run if Path(run).suffix == ".jsonl" else (Path("runs") / run / "events.jsonl")))
    event_kinds = dict(Counter(e.get("kind") for e in events)) if events else {}
    return {
        "run": run,
        "setup": {"rate_rps": setup.get("rate_rps"), "calibration_knee": (setup.get("calibration") or {}).get("knee_rps"),
                  "fingerprint": {k: setup.get("fingerprint", {}).get(k) for k in ("kernel", "cpu_model", "cpu_count", "gcc", "python", "postgres")}},
        "program_counts": counts,
        "cascade_funnel": funnel,
        "best_programs": best_programs,
        "promoted": [p.id for p in promoted],
        "verified": [p.id for p in verified],
        "epistasis": epistasis,
        "llm": llm_summary,
        "alerts": [a.model_dump() for a in store.alerts()],
        "aa_test": aa,
        "verification": store.kv_get("verification"),
        "profile": profile,
        "result": result,
        "event_kinds": event_kinds,
    }
