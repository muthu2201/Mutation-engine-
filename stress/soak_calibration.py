"""Calibrate the L6 soak's leak test: honest programs against real leaks, the old rule against the new.

Each program's soak phase (protocol ``L6-soak``) runs ``--reps`` times, and both rules are
scored on the *same* samples:

- **legacy**: one least-squares slope of service plus database PSS above 0.5 MB/s (the rule
  through experiment 2);
- **current**: the service's own growth, above the threshold and still growing in the second
  half of the soak (``colloid_evaluator.memory``, ADR 0010).

The programs are the baseline; every program of ``--run`` whose L6 holdout cost gain had a CI
lower bound above zero, so its gain was real whatever the soak said; and two builds that leak
a blocked goroutine holding a buffer on every rating lookup. A rule should pass the first two
groups and reject the third.

    python stress/soak_calibration.py --target stackzero-go --run runs/stackzero-go --out docs/results/soak_calibration.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from colloid.adapters.cost.static_prices import StaticPriceCostModel
from colloid.adapters.store.sql_store import open_store
from colloid.adapters.target import open_target, run_target
from colloid.core.genome import Genome
from colloid.core.models import Stage
from colloid_evaluator import memory
from colloid_evaluator.canaries.hacks import GO_RATING, _code_gene, _replace_once
from colloid_evaluator.cascade import Evaluator, fresh_seed
from colloid_evaluator.protocol import PSS_INTERVAL_S, SOAK, Arm


def goroutine_leak(ev: Evaluator, kib: int) -> Genome:
    """Every rating lookup parks a goroutine forever, holding a touched ``kib`` KiB buffer."""
    return Genome.of([_code_gene(ev, GO_RATING, _replace_once(
        "\trow, _, err := fetchRow[ratingRow](",
        f"\tbuf := make([]byte, {kib}<<10)\n"
        "\tfor i := 0; i < len(buf); i += 4096 {\n\t\tbuf[i] = 1\n\t}\n"
        "\tgo func(b []byte) {\n\t\t<-make(chan struct{})\n\t\tb[0]++\n\t}(buf)\n"
        "\trow, _, err := fetchRow[ratingRow](",
    ))])


def honest_programs(run: str, ev: Evaluator) -> list[tuple[str, Genome, dict[str, Any]]]:
    """Programs whose L6 holdout gain was real (CI lower bound > 0), with the old soak verdict."""
    store = open_store(f"sqlite:///{Path(run) / 'colloid.db'}")
    out = []
    try:
        for p in store.programs():
            for e in store.evaluations(p.id, stage=Stage.L6.value):
                details = e.env_fingerprint.get("details") or {}
                ci = (details.get("holdout_cost") or {}).get("ci")
                if not ci or ci[0] <= 0:
                    continue
                soak = details.get("soak") or {}
                meta = {"source_run": run, "l6_verdict": e.verdict.value, "holdout_cost_ci": ci,
                        "recorded_soak_slope": soak.get("slope_mb_per_s"), "recorded_soak_reason": soak.get("reason")}
                out.append((p.id, Genome.of(store.genes(p.gene_ids), ev.atlas), meta))
                break
    finally:
        store.close()
    return out


def soak_once(ev: Evaluator, pid: str, ws: Any) -> dict[str, Any]:
    assert ev.bench is not None
    cmp = ev.bench.compare([Arm("child", pid, ws)], SOAK, seed=fresh_seed())
    if cmp.failures:
        return {"error": cmp.failures.get("child", "failed")}
    ph = cmp.arm_phases("child")[0]
    svc, db = ph.pss_parts.get("service", []), ph.pss_parts.get("db", [])
    ok, info = memory.soak_verdict(svc, db, PSS_INTERVAL_S)
    return {"current_rejects": not ok, "legacy_rejects": bool(info.get("legacy_leaking")), "service": info.get("service"),
            "db": info.get("db"), "total": info.get("total"), "service_series_mb": info.get("service_series_mb"),
            "requests": sum(ph.chunk_requests)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="runs/stackzero-go", help="run store whose L6-evaluated programs are the honest set")
    ap.add_argument("--target", help="defaults to the run's target")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="docs/results/soak_calibration.json")
    args = ap.parse_args()
    store = open_store(f"sqlite:///{Path(args.run) / 'colloid.db'}")
    target = args.target or run_target(store)
    setup = store.kv_get("setup") or {}
    baseline = next(p for p in store.programs(island="baseline"))
    store.close()
    ev = Evaluator(open_target(target), StaticPriceCostModel(), rate=setup.get("rate_rps"))
    ev.setup(baseline.id)
    programs: list[tuple[str, str, Genome, dict[str, Any]]] = [("honest", "baseline", Genome(), {})]
    programs += [("honest", pid, g, meta) for pid, g, meta in honest_programs(args.run, ev)]
    if getattr(ev.target, "language", "") == "go":
        programs += [("leak", f"goroutine_leak_{k}k", goroutine_leak(ev, k), {"leak_kib_per_rating_lookup": k}) for k in (4, 32)]
    rows: list[dict[str, Any]] = []
    t0 = time.monotonic()
    try:
        for group, name, genome, meta in programs:
            pid = genome.program_id(ev.baseline_program_id)
            l1 = ev.l1(pid, genome)
            if not l1.passed or l1.ws is None:
                rows.append({"group": group, "program": name, "error": (l1.evaluation.reasons or ("build failed",))[0][:300]})
                print(f"[{name[:16]}] build failed")
                continue
            reps = [soak_once(ev, pid, l1.ws) for _ in range(args.reps)]
            row = {"group": group, "program": name, "genes": len(genome), **meta, "reps": reps,
                   "legacy_rejections": sum(1 for r in reps if r.get("legacy_rejects")),
                   "current_rejections": sum(1 for r in reps if r.get("current_rejects"))}
            rows.append(row)
            svc = [r["service"]["slope_mb_per_s"] for r in reps if r.get("service")]
            dbs = [r["db"]["slope_mb_per_s"] for r in reps if r.get("db")]
            print(f"[{name[:16]}] {group}: legacy rejects {row['legacy_rejections']}/{args.reps}, current rejects "
                  f"{row['current_rejections']}/{args.reps}; service slopes {svc}, db slopes {dbs}")
    finally:
        ev.shutdown()

    def rate(group: str, key: str) -> dict[str, int]:
        sel = [r for r in rows if r["group"] == group and "reps" in r]
        return {"rejected_soaks": sum(r[key] for r in sel), "soaks": sum(len(r["reps"]) for r in sel)}

    report = {
        "target": target, "run": args.run, "reps": args.reps, "threshold_mb_per_s": memory.LEAK_MB_PER_S,
        "persist_fraction": memory.PERSIST_FRACTION, "elapsed_s": round(time.monotonic() - t0, 1),
        "summary": {rule: {"honest_false_rejections": rate("honest", f"{rule}_rejections"), "leaks_caught": rate("leak", f"{rule}_rejections")}
                    for rule in ("legacy", "current")},
        "programs": rows,
    }
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
