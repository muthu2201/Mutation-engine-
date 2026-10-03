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


def arms_block(rows: list[dict[str, Any]]) -> str:
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


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: a binomial test of b against b + c at p = 0.5."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired(arms: list[tuple[str, list[dict[str, Any]]]]) -> str:
    """The pre-registered comparison of two arms on the same instances (ADR 0012)."""
    (la, _), (lb, _) = arms[0], arms[1]
    res = {lab: {r["instance_id"]: bool((r.get("grade") or {}).get("resolved")) for r in rows} for lab, rows in arms}
    common = sorted(set(res[la]) & set(res[lb]))
    b = sum(1 for i in common if res[lb][i] and not res[la][i])
    c = sum(1 for i in common if res[la][i] and not res[lb][i])
    out = [f"{len(common)} instances ran in both arms.\n", "| arm | resolved | Wilson 95% CI |", "|---|---|---|"]
    for lab in (la, lb):
        k = sum(1 for i in common if res[lab][i])
        lo, hi = wilson(k, len(common))
        out.append(f"| {lab} | {k} / {len(common)} | {100 * lo:.1f}–{100 * hi:.1f}% |")
    out += ["", f"Discordant pairs: {b} resolved only by `{lb}`, {c} only by `{la}`; "
            f"paired difference {b - c:+d} ({lb} − {la}), exact McNemar p = {mcnemar_exact(b, c):.3f}.", "",
            f"| instance | {la} | {lb} |", "|---|---|---|"]
    for i in common:
        out.append(f"| `{i}` | {'**yes**' if res[la][i] else 'no'} | {'**yes**' if res[lb][i] else 'no'} |")
    return "\n".join(out)


def load_probes(path: Path) -> dict[str, Any]:
    """A ``colloid swebench probe`` report: verdicts keyed by (instance, model), plus submission overlaps."""
    rep = json.loads(path.read_text())
    return {"models": rep["models"], "verdict": {(p["instance_id"], p["model"]): p for p in rep["probes"]},
            "submissions": {s["instance_id"]: s for s in rep.get("submissions", [])}}


def clean_set(probes: dict[str, Any], ids: list[str], allow_path_only: bool = False) -> set[str]:
    """Instances on which every probed model of the arm is ``clean`` (or, looser, not ``suspect``)."""
    ok = {"clean", "path-only"} if allow_path_only else {"clean"}
    return {i for i in ids if all((i, m) in probes["verdict"] and probes["verdict"][(i, m)]["verdict"] in ok for m in probes["models"])}


def issue_fix_block(rows: list[dict[str, Any]], data: Path) -> str:
    """Post hoc, no model: which issues already contain their fix verbatim (reads the gold patches)."""
    from colloid_evaluator.swebench import contamination as cm

    tasks, gold = cm.load_jsonl(data / "tasks.jsonl"), cm.load_jsonl(data / "gold.jsonl")
    out = ["| instance | resolved | non-trivial gold lines verbatim in the issue | gold 5-grams present in the issue |", "|---|---|---|---|"]
    stated = []
    for r in rows:
        i = r["instance_id"]
        f = cm.issue_states_fix(tasks[i]["problem_statement"], gold[i]["patch"])
        if f["gold_lines_in_issue"] or f["issue_overlap5"] >= 0.5 or (r.get("grade") or {}).get("resolved"):
            out.append(f"| `{i}` | {'**yes**' if (r.get('grade') or {}).get('resolved') else 'no'} | "
                       f"{f['gold_lines_in_issue']} / {f['gold_nontrivial_lines']} | {f['issue_overlap5']:.2f} |")
        stated.append(f["gold_lines_in_issue"] > 0)
    return "\n".join([f"{sum(stated)} of {len(rows)} issues contain a non-trivial line of their gold fix verbatim. Listed: those, "
                      "the issues with at least half of the gold's added 5-grams, and every resolved instance.", ""] + out)


def contamination(rows: list[dict[str, Any]], probes: dict[str, Any]) -> str:
    """Post-hoc memorisation probes (ADR 0011 addendum). Diagnosis next to the headline, never instead of it."""
    ids = [r["instance_id"] for r in rows]
    resolved = {r["instance_id"] for r in rows if (r.get("grade") or {}).get("resolved")}
    out = ["| model | instances probed | file named, not in issue | `suspect` | `path-only` | `clean` |", "|---|---|---|---|---|---|"]
    for m in probes["models"]:
        ps = [probes["verdict"][(i, m)] for i in ids if (i, m) in probes["verdict"]]
        v = Counter(p["verdict"] for p in ps)
        hits = sum(1 for p in ps if p["path_hit"] and not p["path_mentioned_in_issue"])
        out.append(f"| {m} | {len(ps)} | {hits} | {v['suspect']} | {v['path-only']} | {v['clean']} |")
    out += ["", "Every resolved instance, probed with the model that solved it:", "",
            "| instance | solved by | verdict | file probe | task-ID 5-gram overlap | gold lines recalled | submission ∩ gold (5-gram) | submission = gold's added lines |",
            "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["instance_id"] not in resolved:
            continue
        i, m = r["instance_id"], (r.get("submission") or {}).get("model")
        p, s = probes["verdict"].get((i, m)), probes["submissions"].get(i, {})
        if p is None:
            out.append(f"| `{i}` | {m} | not probed | | | | | |")
            continue
        fp = ("named" + (" (in issue)" if p["path_mentioned_in_issue"] else "")) if p["path_hit"] else "missed"
        out.append(f"| `{i}` | {m} | **{p['verdict']}** | {fp} | {p['task_id_overlap']:.2f} | {p['task_id_exact_lines']} | "
                   f"{s.get('overlap5', 0):.2f} | {'yes' if s.get('identical_added_lines') else 'no'} |")
    out += ["", "| restricted to instances where every model of the arm is ... | instances | resolved | Wilson 95% CI |", "|---|---|---|---|"]
    for label, allow in (("`clean` (the pre-declared rule)", False), ("not `suspect` (`path-only` allowed)", True)):
        keep = clean_set(probes, ids, allow)
        k = len(keep & resolved)
        lo, hi = wilson(k, len(keep))
        out.append(f"| {label} | {len(keep)} | {k} | {100 * lo:.1f}–{100 * hi:.1f}% |")
    return "\n".join(out)


def paired_clean(arms: list[tuple[str, list[dict[str, Any]]]], probes: dict[str, dict[str, Any]]) -> str:
    """ADR 0012 amendment: the paired comparison again, on instances clean for both arms."""
    keep = None
    for lab, rows in arms:
        ids = [r["instance_id"] for r in rows]
        c = clean_set(probes[lab], ids)
        keep = c if keep is None else keep & c
    sub = [(lab, [r for r in rows if r["instance_id"] in (keep or set())]) for lab, rows in arms]
    return paired(sub)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", help="a single arm (same as --arm local=RUN)")
    ap.add_argument("--arm", action="append", default=[], help="LABEL=RUN; the first arm fills the unsuffixed blocks, others NAME_LABEL")
    ap.add_argument("--probes", action="append", default=[], help="LABEL=PROBE_JSON from `colloid swebench probe`")
    ap.add_argument("--data", help="the SWE-bench data dir (tasks.jsonl, gold.jsonl): adds the post-hoc 'fix stated in the issue' block")
    ap.add_argument("--planned", type=int, default=30)
    ap.add_argument("--write")
    args = ap.parse_args()
    specs = [tuple(a.split("=", 1)) for a in args.arm] or [("local", args.run or "runs/swebench")]
    arms = [(label, load(Path(run))) for label, run in specs]
    blocks: dict[str, str] = {}
    for n, (label, rows) in enumerate(arms):
        suffix = "" if n == 0 else f"_{label.upper()}"
        blocks.update({f"SWE_SUMMARY{suffix}": summary(rows, args.planned), f"SWE_FUNNEL{suffix}": funnel(rows),
                       f"SWE_LOCALISATION{suffix}": localisation(rows), f"SWE_INSTANCES{suffix}": per_instance(rows),
                       f"SWE_ARMS{suffix}": arms_block(rows)})
    probes = {label: load_probes(Path(path)) for label, path in (a.split("=", 1) for a in args.probes)}
    for n, (label, rows) in enumerate(arms):
        if label in probes:
            blocks["SWE_CONTAMINATION" + ("" if n == 0 else f"_{label.upper()}")] = contamination(rows, probes[label])
    if args.data:
        blocks["SWE_ISSUE_FIX"] = issue_fix_block(arms[0][1], Path(args.data))
    if len(arms) >= 2:
        blocks["SWE_PAIRED"] = paired(arms[:2])
        if all(lab in probes for lab, _ in arms[:2]):
            blocks["SWE_PAIRED_CLEAN"] = paired_clean(arms[:2], probes)
    if args.write:
        doc = Path(args.write).read_text()
        for name, body in blocks.items():
            if f"<!-- RESULTS:{name} -->" in doc:
                doc = replace_block(doc, name, body)
            else:
                print(f"note: {args.write} has no RESULTS:{name} block; skipped")
        Path(args.write).write_text(doc)
    else:
        for name, body in blocks.items():
            print(f"===== {name}\n{body}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
