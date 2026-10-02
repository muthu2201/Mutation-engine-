"""Stress test: evaluator robustness against adversarial and pathological genomes.

The evaluator must *never* crash, hang, or (worst of all) pass an incorrect candidate, no
matter what the search side throws at it. This harness fires a battery of pathological
genomes at the live cascade and checks each is handled by the right stage with a clean
verdict:

* infinite-loop handlers (must be killed by the wall clock, FAIL, not hang)
* handlers that raise, return the wrong type, or emit malformed JSON (oracle FAIL)
* huge diffs over the cap and syntactically invalid payloads (L0 FAIL)
* every red-team hack again, now interleaved with legitimate winners in one batch
* the full canary suite (must stay 100% rejected)
* an A/A stability check over many runs (the false-positive rate must stay at/below alpha)

It reuses the real Evaluator, so it also exercises concurrency between the benchmark load
generator, Postgres, and candidate services under sustained pressure. Run as root with
COLLOID integration deps available.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from colloid.adapters.cost.static_prices import StaticPriceCostModel
from colloid.adapters.target.stackzero.adapter import StackZeroTarget
from colloid.core.genome import Genome
from colloid.core.ids import sha256_hex
from colloid.core.models import Gene, PayloadKind, Provenance, Surface
from colloid_evaluator.canaries.hacks import run_canaries
from colloid_evaluator.cascade import Evaluator

PROV = Provenance(operator="stress")
SUMMARY = "py:service/shop/handlers.py::customer_summary"
RATING = "py:service/shop/search.py::rating_summary"


def code_gene(ev, path, new_body):
    u = ev.atlas.unit_by_path(path)
    base = str(u.tags["baseline_source"])
    loc = ev.atlas.locus_for(u.id, Surface.CODE_REGION)
    return Gene.make(loc.id, PayloadKind.SOURCE, {"source": new_body, "base_hash": sha256_hex(base)[:16], "language": "python"}, PROV)


def pathologies(ev):
    return {
        "infinite_loop": code_gene(ev, RATING, "async def rating_summary(db, product_id):\n    while True:\n        pass\n"),
        "raises": code_gene(ev, RATING, "async def rating_summary(db, product_id):\n    raise RuntimeError('boom')\n"),
        "wrong_type": code_gene(ev, RATING, "async def rating_summary(db, product_id):\n    return 'not a tuple'\n"),
        "syntax_error": code_gene(ev, RATING, "async def rating_summary(db, product_id)\n    return (0, None)\n"),
        "huge_diff": code_gene(ev, SUMMARY, "async def customer_summary(db, customer_id):\n" + "    x = 1\n" * 400 + "    return {}\n"),
        "wrong_result": code_gene(ev, RATING, "async def rating_summary(db, product_id):\n    return (0, 0.0)\n"),
    }


def evaluate(ev, name, genome):
    pid = genome.program_id(ev.baseline_program_id)
    t0 = time.monotonic()
    r0 = ev.l0(pid, genome)
    if not r0.passed:
        return {"name": name, "stage": "L0", "verdict": "FAIL", "wall": round(time.monotonic() - t0, 1), "reason": (r0.evaluation.reasons or ("",))[0][:80]}
    try:
        r1 = ev.l1(pid, genome)
    except Exception as e:
        return {"name": name, "stage": "L1", "verdict": "ERROR", "wall": round(time.monotonic() - t0, 1), "reason": f"{type(e).__name__}: {e}"[:80]}
    if not r1.passed:
        return {"name": name, "stage": "L1", "verdict": "FAIL", "wall": round(time.monotonic() - t0, 1), "reason": (r1.evaluation.reasons or ("",))[0][:80]}
    r2 = ev.l2(pid, genome, r1.ws)
    return {"name": name, "stage": "L2", "verdict": r2.evaluation.verdict.value.upper(), "wall": round(time.monotonic() - t0, 1),
            "reason": (r2.evaluation.reasons or ("",))[0][:80]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=45.0)
    ap.add_argument("--aa-runs", type=int, default=20)
    args = ap.parse_args()
    ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=args.rate)
    ev.setup("baseline")
    out = {"pathologies": [], "canaries": {}, "aa": {}}
    try:
        t0 = time.monotonic()
        for name, gene in pathologies(ev).items():
            res = evaluate(ev, name, Genome.of([gene], ev.atlas))
            out["pathologies"].append(res)
            print(f"[patho] {name:16} -> {res['stage']} {res['verdict']} ({res['wall']}s) {res['reason']}")
        # none of the pathologies may PASS
        out["pathologies_all_rejected"] = all(r["verdict"] in ("FAIL", "ERROR") for r in out["pathologies"])
        out["infinite_loop_killed_fast"] = next(r["wall"] for r in out["pathologies"] if r["name"] == "infinite_loop") < 120

        print("\n[canaries] running full suite under stress...")
        can = run_canaries(ev, dynamic_only=False)
        out["canaries"] = {k: v for k, v in can.items() if k != "canaries"}
        # keep where and why each canary was rejected, so an unexpected stage is diagnosable
        out["canary_rows"] = [{"canary": r["canary"], **{k: r["full"][k] for k in ("stage", "reason")}} for r in can["canaries"] if "full" in r]

        print(f"\n[aa] {args.aa_runs} A/A runs...")
        out["aa"] = ev.aa_test(args.aa_runs)
        out["elapsed_s"] = round(time.monotonic() - t0, 1)
        out["ok"] = (out["pathologies_all_rejected"] and out["infinite_loop_killed_fast"]
                     and out["canaries"]["all_rejected"] and out["aa"].get("promotions_allowed", False))
    finally:
        ev.shutdown()
    print("\n" + json.dumps(out, indent=2))
    Path("/opt/colloid/state/stress_evaluator.json").write_text(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
