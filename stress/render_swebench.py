"""Render a SWE-bench run (``colloid swebench run``) into docs/SWEBENCH_RESULTS.md.

A pure projection of ``results.jsonl`` and the per-instance records; no hand-entered numbers.

    python stress/render_swebench.py --run runs/swebench --write docs/SWEBENCH_RESULTS.md
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_results import replace_block


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def load(run: Path) -> list[dict[str, Any]]:
    rows = [json.loads(ln) for ln in (run / "results.jsonl").read_text().splitlines()] if (run / "results.jsonl").exists() else []
    for r in rows:  # the per-instance record has the candidates
        rec = run / r["instance_id"] / "record.json"
        r["candidates"] = json.loads(rec.read_text()).get("candidates", []) if rec.exists() else []
    return rows


def summary(rows: list[dict[str, Any]], planned: int) -> str:
    n = len(rows)
    k = sum(1 for r in rows if (r.get("grade") or {}).get("resolved"))
    lo, hi = wilson(k, n)
    sub = sum(1 for r in rows if r.get("submission"))
    err = sum(1 for r in rows if r.get("error"))
    hours = sum(r.get("wall_s", 0) for r in rows) / 3600
    calls = sum(r.get("llm_calls", 0) for r in rows)
    return "\n".join([
        f"**Resolved: {k} / {n}** ({100 * k / max(n, 1):.1f}%, Wilson 95% CI {100 * lo:.1f}–{100 * hi:.1f}%), "
        f"graded by the official SWE-bench harness. {n} of {planned} pre-registered instances ran.\n",
        "| | count |", "|---|---|",
        f"| instances run | {n} |", f"| a patch was submitted (passed L0–L2) | {sub} |", f"| resolved (all FAIL_TO_PASS and PASS_TO_PASS pass) | {k} |",
        f"| infrastructure errors | {err} |", f"| wall clock, all instances | {hours:.1f} h |", f"| LLM calls | {calls} |",
    ])


def funnel(rows: list[dict[str, Any]]) -> str:
    stages: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for r in rows:
        for c in r["candidates"]:
            st = c.get("stage")
            stages["proposals"] += 1
            if st in ("parse", "llm"):
                reasons[f"{st}: {(c.get('reason') or '')[:48]}"] += 1
                continue
            stages["parsed and spliced"] += 1
            if st != "L0":
                stages["passed L0 (patch policy)"] += 1
            if st not in ("L0", "L1"):
                stages["passed L1 (applies, compiles)"] += 1
            if c.get("ok"):
                stages["passed L2 (no regressions)"] += 1
                if c.get("votes"):
                    stages["resolved ≥1 validated reproduction (L3)"] += 1
    repro = Counter(x["outcome"] for r in rows for x in r.get("repro", []))
    out = ["| stage | candidates |", "|---|---|"] + [f"| {k} | {v} |" for k, v in stages.items()]
    out += ["", "| reproduction script at base_commit | scripts |", "|---|---|"] + [f"| {k} | {v} |" for k, v in repro.most_common()]
    out += ["", "| why a response was not a candidate | count |", "|---|---|"] + [f"| {k} | {v} |" for k, v in reasons.most_common(8)]
    return "\n".join(out)


def localisation(rows: list[dict[str, Any]]) -> str:
    d = [r.get("diagnosis") or {} for r in rows]
    n = len(d)
    f = sum(1 for x in d if x.get("localised_file_hit"))
    ln = sum(1 for x in d if x.get("localised_line_hit"))
    s = sum(1 for x in d if x.get("submission_in_gold_file"))
    return "\n".join(["Computed after grading, from the gold patch, for diagnosis only:\n", "| | instances |", "|---|---|",
                      f"| a localised snippet is in a file the gold patch changes | {f} / {n} |",
                      f"| a localised snippet overlaps the gold patch's changed lines | {ln} / {n} |",
                      f"| the submission edits a file the gold patch changes | {s} / {n} |"])


def per_instance(rows: list[dict[str, Any]]) -> str:
    out = ["| instance | localised (file / lines) | validated repro | candidates ok / proposed | submitted | resolved | wall min |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        d = r.get("diagnosis") or {}
        cands = r["candidates"]
        ok = sum(1 for c in cands if c.get("ok"))
        loc = ("✓" if d.get("localised_file_hit") else "✗") + " / " + ("✓" if d.get("localised_line_hit") else "✗")
        g = r.get("grade") or {}
        res = "**yes**" if g.get("resolved") else ("error" if r.get("error") else "no")
        out.append(f"| `{r['instance_id']}` | {loc} | {r.get('validated_repro', 0)}/{len(r.get('repro', []))} | {ok} / {len(cands)} | "
                   f"{'yes' if r.get('submission') else 'no'} | {res} | {r.get('wall_s', 0) / 60:.0f} |")
    return "\n".join(out)


def arms(rows: list[dict[str, Any]]) -> str:
    tally: dict[str, Counter[str]] = {}
    for r in rows:
        for c in r["candidates"]:
            key = f"{c.get('model')} / {c.get('template')}"
            t = tally.setdefault(key, Counter())
            t["proposals"] += 1
            t["ok"] += 1 if c.get("ok") else 0
            t["votes"] += 1 if c.get("votes") else 0
        sub = r.get("submission")
        if sub and (r.get("grade") or {}).get("resolved"):
            tally.setdefault(f"{sub['model']} / {sub['template']}", Counter())["resolved"] += 1
    out = ["| arm (model / prompt) | proposals | passed L0–L2 | resolved a reproduction | resolved instances |", "|---|---|---|---|---|"]
    for k, t in sorted(tally.items()):
        out.append(f"| {k} | {t['proposals']} | {t['ok']} | {t['votes']} | {t['resolved']} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="runs/swebench")
    ap.add_argument("--planned", type=int, default=30)
    ap.add_argument("--write")
    args = ap.parse_args()
    rows = load(Path(args.run))
    blocks = {"SWE_SUMMARY": summary(rows, args.planned), "SWE_FUNNEL": funnel(rows), "SWE_LOCALISATION": localisation(rows),
              "SWE_INSTANCES": per_instance(rows), "SWE_ARMS": arms(rows)}
    if args.write:
        doc = Path(args.write).read_text()
        for name, body in blocks.items():
            doc = replace_block(doc, name, body)
        Path(args.write).write_text(doc)
    else:
        for name, body in blocks.items():
            print(f"===== {name}\n{body}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
