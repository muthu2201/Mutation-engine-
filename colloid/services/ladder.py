"""Inputs for the evidence ladder (``colloid.core.ladder``) from the lake, run stores and
canary reports, and the transfer A/B of two runs (``colloid compare``)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from colloid.adapters.store.sql_store import open_store
from colloid.adapters.target import TARGETS, open_target, run_target, target_class
from colloid.core.ids import sha256_hex
from colloid.core.ladder import ArmResult, Gate, LadderInput, evaluate
from colloid.core.lake import verify_chain
from colloid.core.models import ProgramStatus, Stage, Verdict
from colloid.core.objectives import gain_percent
from colloid.ports import LakeStore
from colloid.services.report import _store_url

RESULTS = Path("docs/results")


def arm_result(run: str) -> ArmResult:
    """Best verified gain and when it was verified, from a run's own store.

    Only L6 passes made by the run itself count: ``colloid verify`` adds L6 evaluations after
    the run ends, which say nothing about how fast the search found anything (ADR 0008 defines
    VGPH from run start to the run's own L6 pass)."""
    store = open_store(_store_url(run))
    try:
        baseline = next(p for p in store.programs(island="baseline"))
        start = baseline.created_at
        elapsed_min = (store.kv_get("result") or {}).get("elapsed_min")
        end = start + 60.0 * float(elapsed_min) + 300.0 if elapsed_min is not None else float("inf")
        evaluated = sum(1 for p in store.programs() if store.evaluations(p.id, stage=Stage.L5.value) or store.evaluations(p.id, stage=Stage.L4.value))
        verified = [*store.programs(status=ProgramStatus.PROMOTED), *store.programs(status=ProgramStatus.VERIFIED)]
        points: list[tuple[float, float]] = []  # (gain %, hours)
        for prog in verified:
            l6 = sorted((e for e in store.evaluations(prog.id, stage=Stage.L6.value) if e.verdict == Verdict.PASS and e.created_at <= end),
                        key=lambda e: e.created_at)
            est = next((o for o in l6[0].objectives if o.objective == "cost" and o.reference == "baseline"), None) if l6 else None
            if est is None:
                continue
            points.append((round(gain_percent(est.log_ratio), 2), round((l6[0].created_at - start) / 3600.0, 3)))
        best = max(points, key=lambda p: (p[0], -p[1])) if points else None
        first = min(points, key=lambda p: p[1]) if points else None
        return ArmResult(Path(run).name, best[0] if best else None, best[1] if best else None, first[1] if first else None,
                         len(points), evaluated)
    finally:
        store.close()


def same_experiment(cold: str, primed: str) -> list[str]:
    """Differences between the two runs' configurations other than the lake (should be none)."""
    cfgs = []
    for run in (cold, primed):
        store = open_store(_store_url(run))
        try:
            cfgs.append(store.kv_get("config") or {})
            cfgs[-1]["_target"] = run_target(store)
        finally:
            store.close()
    ignore = {"name", "lake", "lake_transfer", "lake_priors", "lake_seed_top", "lake_prior_weight", "store_url", "telemetry_path", "rate_rps", "rules"}
    return sorted(k for k in set(cfgs[0]) | set(cfgs[1]) if k not in ignore and cfgs[0].get(k) != cfgs[1].get(k))


def canary_reports(directory: Path = RESULTS) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("canaries*.json")):
        data = json.loads(path.read_text())
        out[str(data.get("target") or "stackzero")] = data
    return out


def ladder_input(lake: LakeStore, *, transfer: tuple[str, str] | None = None, results: Path = RESULTS) -> LadderInput:
    records, entries = lake.records(), lake.entries()
    verify_chain(entries, records)
    language_of, schema_of = {}, {}
    for name in TARGETS:
        language_of[name] = str(getattr(target_class(name), "language", "?"))
        try:
            schema_of[name] = sha256_hex(open_target(name, observe_system=False).schema_sql())[:16]
        except Exception:  # a target whose toolchain is missing here still has a language
            schema_of[name] = "unknown"
    return LadderInput(
        programs=[r.content for r in records.values() if r.kind == "program"],
        rules=[r.content for r in records.values() if r.kind == "rule"],
        genes={rid: r.content for rid, r in records.items() if r.kind == "gene"},
        language_of=language_of, schema_of=schema_of, canaries=canary_reports(results),
        transfer=(arm_result(transfer[0]), arm_result(transfer[1])) if transfer else None,
    )


def ladder(lake: LakeStore, *, transfer: tuple[str, str] | None = None, results: Path = RESULTS) -> list[Gate]:
    return evaluate(ladder_input(lake, transfer=transfer, results=results))
