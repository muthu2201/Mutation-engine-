"""Choose the API arm's reasoning effort from pilot runs, by the rule declared in ADR 0012 (amendment 2).

The pilot instances are outside the pre-registered sample. Each effort is piloted with the full
engine (``colloid swebench run --instance ...``); every call's latency and finish reason are in
the per-instance records. The rule picks the highest effort that leaves the 16-call budget usable
inside the 20-minute search: median call latency at most 75 s (16 × 75 s = 20 min), and at most
a quarter of calls cut off by the token cap or empty.

    python stress/api_pilot.py --arm high=runs/pilot-nvidia-high --arm low=runs/pilot-nvidia-low \
        --write docs/results/swebench/api_pilot.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

LATENCY_MAX_S = 75.0
UNUSABLE_MAX = 0.25
ORDER = ["max", "high", "medium", "low"]  # highest effort first


def stats(run: Path) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    for rec_path in sorted(run.glob("*/record.json")):
        rec = json.loads(rec_path.read_text())
        calls += rec.get("calls", [])
        if rec.get("error"):
            errors.append(f"{rec_path.parent.name}: {str(rec['error'])[:200]}")
    lat = [c["latency_s"] for c in calls]
    unusable = [c for c in calls if c.get("finish_reason") == "length" or c.get("empty")]
    return {"calls": len(calls), "errors": errors,
            "median_latency_s": round(statistics.median(lat), 1) if lat else None,
            "p90_latency_s": round(statistics.quantiles(lat, n=10)[-1], 1) if len(lat) >= 2 else None,
            "unusable_share": round(len(unusable) / len(calls), 3) if calls else None,
            "median_tokens_out": statistics.median([c["tokens_out"] for c in calls]) if calls else None}


def choose(efforts: dict[str, dict[str, Any]]) -> tuple[str | None, str]:
    for e in sorted(efforts, key=ORDER.index):
        s = efforts[e]
        if s["calls"] and not s["errors"] and s["median_latency_s"] <= LATENCY_MAX_S and s["unusable_share"] <= UNUSABLE_MAX:
            return e, "fits the budget"
    usable = [e for e in efforts if efforts[e]["calls"] and not efforts[e]["errors"]]
    if not usable:
        return None, "no effort completed the pilot without errors: fix the provider before the arm runs"
    return sorted(usable, key=ORDER.index)[-1], "none fits; the lowest effort runs and the time budget binds"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", action="append", required=True, help="EFFORT=RUN_DIR")
    ap.add_argument("--write")
    args = ap.parse_args()
    efforts = {e: stats(Path(d)) for e, d in (a.split("=", 1) for a in args.arm)}
    chosen, why = choose(efforts)
    out = {"rule": {"median_latency_s_max": LATENCY_MAX_S, "unusable_share_max": UNUSABLE_MAX, "order": ORDER},
           "efforts": efforts, "chosen": chosen, "why": why}
    print(json.dumps(out, indent=2))
    if args.write:
        Path(args.write).write_text(json.dumps(out, indent=2) + "\n")
    return 0 if chosen else 1


if __name__ == "__main__":
    sys.exit(main())
