"""Render the polyglot experiment's measured results into docs/POLYGLOT_RESULTS.md.

A pure projection of evidence files and run stores; no hand-entered numbers. Each block lives
between ``<!-- RESULTS:NAME -->`` and ``<!-- /RESULTS:NAME -->`` and is replaced in place.

    python stress/render_polyglot.py --write docs/POLYGLOT_RESULTS.md
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from render_results import aa_block, replace_block, run_block

from colloid.adapters.telemetry.jsonl import read_events

RESULTS = Path("docs/results")


def _load(name: str) -> Any:
    p = RESULTS / name
    return json.loads(p.read_text()) if p.exists() else None


def canary_block(report: dict[str, Any] | None, title: str) -> str:
    if not report:
        return "_not measured yet_"
    out = [f"**{title}: {report['rejected']}/{report['total']} rejected** (`{report.get('target', 'stackzero')}`)\n",
           "| canary | what it does | full cascade | with L0 bypassed |", "|---|---|---|---|"]
    for row in report["canaries"]:
        full, dyn = row.get("full", {}), row.get("dynamic_only", {})
        f = f"**{full.get('stage')}**" if full.get("rejected") else "PASSED (hole)"
        d = (dyn.get("stage") if dyn.get("rejected") else "passes: only L0 sees it") if dyn else "—"
        out.append(f"| {row['canary']} | {row.get('description', '')} | {f} | {d} |")
    return "\n".join(out)


def llm_block(run: str) -> str:
    events = read_events(Path(run) / "events.jsonl")
    calls = Counter()
    reasons = Counter()
    stages = Counter()
    for e in events:
        arm = e.get("arm") or []
        if e.get("kind") == "propose.reject" and arm and arm[0] == "llm_rewrite":
            r = str(e.get("reason", ""))
            reasons["no usable function (parse/confinement)" if "rejected" in r else r[:60]] += 1
            calls["rejected at parse"] += 1
        if e.get("kind") == "cascade.reject" and e.get("operator") == "llm_rewrite":
            stages[f"rejected at {e.get('stage')}"] += 1
        if e.get("kind") in ("cascade.l4_pass",) and e.get("operator") == "llm_rewrite":
            stages["passed L4"] += 1
        if e.get("kind") == "admit" and e.get("operator") == "llm_rewrite":
            stages["admitted (passed L5)"] += 1
    fine: Counter[str] = Counter()
    for e in events:
        if e.get("kind") == "propose.reject" and (e.get("arm") or [""])[0] == "llm_rewrite":
            r = str(e.get("reason", ""))
            key = ("extra top-level code" if "extra top-level" in r else "missing / duplicate function" if "does not define" in r else
                   "syntax error" if "syntax error" in r else "signature changed" if "signature" in r else "identical to original" if "identical" in r
                   else "other: " + r[:50])
            fine[key] += 1
    out = [f"**LLM rewrites in `{Path(run).name}`** (local Qwen2.5-Coder, Go):\n", "| outcome | count |", "|---|---|"]
    for k, v in fine.most_common():
        out.append(f"| response rejected: {k} | {v} |")
    for k, v in sorted(stages.items()):
        out.append(f"| {k} | {v} |")
    return "\n".join(out)


def transfer_block(cold: str, primed: str) -> str:
    cmp = _load("m1_transfer.json")
    if not cmp:
        return "_not measured yet_"
    out = ["| arm | best verified cost gain (L6 holdout) | hours to it | VGPH (%/h) | first verified after | verified / evaluated |",
           "|---|---|---|---|---|---|"]
    for label in ("cold", "primed"):
        a = cmp[label]
        vgph = (a["best_gain_pct"] / a["hours_to_best"]) if a.get("best_gain_pct") is not None and a.get("hours_to_best") else None
        out.append(f"| {label} (`{a['run']}`) | {a['best_gain_pct']}% | {a['hours_to_best']} | {vgph and round(vgph, 1)} | "
                   f"{a['hours_to_first']} h | {a['verified']} / {a['evaluated']} |")
    out.append(f"\nConfiguration differences besides the lake: {cmp['config_differences_besides_the_lake'] or 'none'}.")
    seeds = [e for e in read_events(Path(primed) / "events.jsonl") if e.get("kind") in ("lake.seed", "lake.skip")]
    if seeds:
        out.append("\n**What the primed run received from the lake (generation 1):**\n")
        for e in seeds:
            if e["kind"] == "lake.seed":
                out.append(f"- seed `{e['program'][:10]}` from record `{e['record'][:12]}` ({e['genes']} genes, {e.get('lake_cost_gain_pct')}% on its source)")
            else:
                out.append(f"- skipped: {e['reason']}")
    return "\n".join(out)


def bakeoff_blocks() -> dict[str, str]:
    rep = _load("bakeoff.json")
    if not rep:
        return {k: "_not measured yet_" for k in ("BAKEOFF_CONFORMANCE", "BAKEOFF_LOAD", "BAKEOFF_FOOTPRINT", "BAKEOFF_CAPACITY", "BAKEOFF_SCALE")}
    impls = rep["implementations"]
    conf = ["| implementation | language | requests | mismatches |", "|---|---|---|---|"]
    for name, e in impls.items():
        c = e.get("conformance")
        conf.append(f"| `{name}` | {e['language']} | {c['requests'] if c else 'reference'} | {c['mismatches'] if c else '—'} |")
    load = [f"Offered load {rep['rate_rps']} req/s for every arm (half the Python reference's knee), protocol `{rep['protocol']}`.\n",
            "| implementation | $ / 1M req | CPU ms / req | p50 ms | p95 ms | stack PSS MB | cost vs Python (95% CI) |", "|---|---|---|---|---|---|---|"]
    for name, e in impls.items():
        m = e.get("at_equal_load")
        if not m:
            continue
        vs = e.get("vs_reference", {}).get("cost")
        cost = f"{vs['gain_pct']:+.1f}% [{vs['ci_pct'][0]:+.1f}, {vs['ci_pct'][1]:+.1f}]" if vs else "reference"
        load.append(f"| `{name}` | {m['usd_per_mreq']['point']:.4f} | {m['cpu_us_per_req']['point'] / 1000:.2f} | {m['latency_p50_ms']['point']:.1f} | "
                    f"{m['latency_p95_ms']['point']:.1f} | {m['mem_pss_mb']['point']:.0f} | {cost} |")
    foot = ["| implementation | runtime | runtime MB | third-party packages | deps MB | app MB | start-up ms | service PSS MB |", "|---|---|---|---|---|---|---|---|"]
    for name, e in impls.items():
        f = e["footprint"]
        foot.append(f"| `{name}` | {f['runtime']} | {f['runtime_bytes'] / 1e6:.1f} | {len(f['dependencies'])} | {f['dependency_bytes'] / 1e6:.1f} | "
                    f"{f['app_bytes'] / 1e6:.2f} | {f['startup_ms_median']} | {f['service_pss_mb_after_warmup']} |")
    cap = ["| implementation | knee: the highest tested rate with p99 within 8x of light load (req/s) | next tested rate |", "|---|---|---|"]
    for name, e in impls.items():
        if e.get("capacity"):
            offered = [pt["offered"] for pt in e["capacity"].get("curve", [])]
            nxt = next((r for r in offered if r > e["capacity"]["knee_rps"]), None)
            cap.append(f"| `{name}` | {e['capacity']['knee_rps']} | {nxt if nxt is not None else '—'} |")
    scale = rep.get("scale_projection") or {}
    sc = [f"Assumptions: {json.dumps(scale.get('assumptions', {}))}\n", "| implementation | CPU ms/req | 100 req/s | 1k req/s | 10k req/s |", "|---|---|---|---|---|"]
    for name, e in (scale.get("implementations") or {}).items():
        cells = [f"${e['load'][k]['usd_per_month']:,.0f}/mo ({e['load'][k]['vcpus']} vCPU)" for k in ("100", "1000", "10000")]
        sc.append(f"| `{name}` | {e['cpu_ms_per_req']} | " + " | ".join(cells) + " |")
    return {"BAKEOFF_CONFORMANCE": "\n".join(conf), "BAKEOFF_LOAD": "\n".join(load), "BAKEOFF_FOOTPRINT": "\n".join(foot),
            "BAKEOFF_CAPACITY": "\n".join(cap), "BAKEOFF_SCALE": "\n".join(sc)}


def rules_block() -> str:
    crl = RESULTS / "rules.crl"
    applied = _load("rules_applied.json")
    if not crl.exists():
        return "_not mined yet_"
    out = ["```", crl.read_text().rstrip(), "```"]
    if applied:
        out.append("\n**Applied to each implementation** (proposal → the target's locus):\n")
        out.append("| target | rule | proposed index | locus | queries |")
        out.append("|---|---|---|---|---|")
        for row in applied:
            out.append(f"| `{row['target']}` | {row['rule']} | `{row['index']}` | {row['knob'] or 'no locus'} | {row['queries']} |")
    return "\n".join(out)


def soak_block() -> str:
    rep = _load("soak_calibration.json")
    if not rep:
        return "_not measured yet_"
    out = [f"`{rep['target']}`, {rep['reps']} soaks per program, threshold {rep['threshold_mb_per_s']} MB/s.\n",
           "| rule | honest soaks rejected | leak soaks caught |", "|---|---|---|"]
    names = {"legacy": "legacy: one slope of service + database PSS", "current": "current: the service's growth, persisting into the second half"}
    for rule, s in rep["summary"].items():
        h, lk = s["honest_false_rejections"], s["leaks_caught"]
        out.append(f"| {names.get(rule, rule)} | {h['rejected_soaks']}/{h['soaks']} | {lk['rejected_soaks']}/{lk['soaks']} |")
    out += ["", "| program | group | holdout cost CI (log) | soak verdict in the run | legacy rejects | current rejects | service MB/s | database MB/s |",
            "|---|---|---|---|---|---|---|---|"]

    def med(xs: list[float]) -> str:
        xs = sorted(xs)
        return f"{xs[len(xs) // 2]:.2f}" if xs else "—"

    def label(name: str) -> str:
        return name[:12] if all(c in "0123456789abcdef" for c in name) else name

    for r in rep["programs"]:
        if "reps" not in r:
            out.append(f"| `{label(r['program'])}` | {r['group']} | — | — | build failed | | | |")
            continue
        ci = r.get("holdout_cost_ci")
        cis = f"[{ci[0]:+.3f}, {ci[1]:+.3f}]" if ci else "—"
        rec = ("leak" if r.get("recorded_soak_reason") else "pass") if "recorded_soak_slope" in r else "—"
        svc = [x["service"]["slope_mb_per_s"] for x in r["reps"] if x.get("service")]
        db = [x["db"]["slope_mb_per_s"] for x in r["reps"] if x.get("db")]
        n = len(r["reps"])
        out.append(f"| `{label(r['program'])}` | {r['group']} | {cis} | {rec} | {r['legacy_rejections']}/{n} | {r['current_rejections']}/{n} | {med(svc)} | {med(db)} |")
    return "\n".join(out)


def ladder_block() -> str:
    gates = _load("ladder.json")
    if not gates:
        return "_not evaluated yet_"
    out = ["| rung | gate | status | evidence | needs |", "|---|---|---|---|---|"]
    for g in gates:
        out.append(f"| {g['rung']} | {g['title']} | **{g['status']}** | {'<br>'.join(g['evidence']) or '—'} | {'<br>'.join(g['needs']) or '—'} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold", default="runs/stackzero-go")
    ap.add_argument("--primed", default="runs/stackzero-go-primed")
    ap.add_argument("--write")
    args = ap.parse_args()
    blocks: dict[str, str] = {
        "CANARIES_GO": canary_block(_load("canaries_go.json"), "Go canary suite"),
        "CANARIES_PY": canary_block(_load("canaries.json"), "Python canary suite"),
        "M1_TRANSFER": transfer_block(args.cold, args.primed),
        "RULES": rules_block(),
        "LADDER": ladder_block(),
        "SOAK": soak_block(),
        **bakeoff_blocks(),
    }
    for label, run in (("COLD", args.cold), ("PRIMED", args.primed)):
        if (Path(run) / "colloid.db").exists():
            from colloid.adapters.store.sql_store import open_store

            store = open_store(f"sqlite:///{Path(run) / 'colloid.db'}")
            aa = store.kv_get("aa_test")
            store.close()
            blocks[f"M1_{label}"] = (aa_block(aa, "A/A noise floor") if aa else "") + "\n" + run_block(run)
            blocks[f"LLM_{label}"] = llm_block(run)
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
