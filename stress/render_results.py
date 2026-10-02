"""Render the measured results of a run (and the stress JSON) into docs/INITIAL_RESULTS.md.

A pure projection of the run's own store + telemetry + stress reports — no hand-entered
numbers. Each generated block lives between ``<!-- RESULTS:NAME -->`` and
``<!-- /RESULTS:NAME -->`` markers and is replaced in place, so re-rendering is idempotent.
With ``--evidence DIR`` the full machine-readable reports are also written next to the doc
(``runs/`` is not committed; the evidence is).

    python stress/render_results.py runs/stackzero                       # print the blocks
    python stress/render_results.py runs/stackzero --write docs/INITIAL_RESULTS.md --evidence docs/results
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from colloid.adapters.telemetry.jsonl import read_events
from colloid.services.report import build_report


def _fmt_ci(g: dict) -> str:
    return f"{g['pct']:+.1f}% (CI [{g['ci_pct'][1]:+.1f}%, {g['ci_pct'][0]:+.1f}%], p={g['p']:.3f})"


def aa_block(aa: dict, title: str) -> str:
    out = [f"**{title}** ({aa.get('runs')} runs of the L5 protocol, identical program vs itself, α={aa.get('alpha')}):\n",
           "| objective | raw FPR (within-run CI) | effect SD across runs | median within-run SE | τ (between-run SD) | calibrated FPR (leave-one-out) | binomial p |",
           "|---|---|---|---|---|---|---|"]
    for obj, s in aa.get("objectives", {}).items():
        out.append(f"| {obj} | {s['false_positive_rate']:.0%} ({s['false_positives']}) | {s['effect_sd'] * 100:.2f}% | "
                   f"{s.get('median_within_se', float('nan')) * 100:.2f}% | {s.get('tau', 0) * 100:.2f}% | "
                   f"{s.get('calibrated_false_positive_rate', float('nan')):.0%} ({s.get('calibrated_false_positives', '?')}) | "
                   f"{s.get('binomial_p', float('nan')):.3f} |")
    out.append(f"\nGate: {aa.get('gate', '—')} → promotions allowed: **{aa.get('promotions_allowed')}**.\n")
    return "\n".join(out)


def run_block(run: str) -> str:
    rep = build_report(run)
    events = read_events(Path(run) / "events.jsonl")
    kinds = Counter(e.get("kind") for e in events)
    res = rep.get("result", {})
    out = []
    setup = rep["setup"]
    out.append(f"**Run `{rep['run']}`** — rate {setup.get('rate_rps')} rps (knee ≈ {setup.get('calibration_knee')} rps), "
               f"{res.get('generations', '?')} generations, {res.get('programs_evaluated', 0)} programs evaluated of "
               f"{res.get('programs_total', 0)} proposed, {len(rep.get('promoted', []))} promoted, {len(rep.get('verified', []))} L6-verified (promotion held), "
               f"{res.get('elapsed_min', 0):.0f} min wall.\n")
    # cascade funnel
    out.append("**Cascade funnel** (how many candidates each stage saw / passed):\n")
    out.append("| stage | pass | fail | suspicious | error |")
    out.append("|---|---|---|---|---|")
    for stage in ("L0", "L1", "L2", "L4", "L5", "L6"):
        c = rep["cascade_funnel"].get(stage, {})
        out.append(f"| {stage} | {c.get('pass', 0)} | {c.get('fail', 0)} | {c.get('suspicious', 0)} | {c.get('error', 0)} |")
    out.append("")
    # best programs
    out.append("**Best programs found** (measured vs baseline):\n")
    out.append("| program | island | operator | cost | p50 | mem | genes |")
    out.append("|---|---|---|---|---|---|---|")
    for p in rep["best_programs"][:10]:
        g = p.get("gains_pct", {})
        cost = _fmt_ci(g["cost"]) if g.get("cost") else "—"
        p50 = f"{g['p50']['pct']:+.1f}%" if g.get("p50") else "—"
        mem = f"{g['mem']['pct']:+.1f}%" if g.get("mem") else "—"
        genes = "<br>".join(p.get("genes", [])) or "—"
        out.append(f"| `{p['id'][:10]}` ({p['status']}) | {p['island']} | {p['operator']} | {cost} | {p50} | {mem} | {genes} |")
    out.append("")
    ver = rep.get("verification")
    if ver and ver.get("programs"):
        out.append(f"**Post-run verification** (`colloid verify`: L6 deep assurance for every eligible top program, then a "
                   f"{ver['cycles']}-cycle replicate vs baseline; promotion needs L6 pass + holdout surviving Holm at "
                   f"α={ver['alpha']} + replicate CI > 0 + A/A gate):\n")
        out.append("| program | island | L5 cost (in-run) | L6 | holdout cost | replicate cost | replicate p50 | replicate mem | decision |")
        out.append("|---|---|---|---|---|---|---|---|---|")

        def _g(d: dict | None) -> str:  # verify stores ci_pct ascending: [lower, upper]
            return f"{d['gain_pct']:+.1f}% [{d['ci_pct'][0]:+.1f}, {d['ci_pct'][1]:+.1f}]" if d else "—"

        for r in ver["programs"]:
            rep_ = r.get("replicate", {})
            hold = r.get("holdout")
            hold_s = f"{hold['gain_pct']:+.1f}% [{hold['ci_pct'][0]:+.1f}, {hold['ci_pct'][1]:+.1f}] p={hold['p']:.3f}" if hold else "—"
            out.append(f"| `{r['program'][:10]}` | {r['island']} | {r['l5_cost_gain_pct']:+.1f}% | {r.get('l6')} | {hold_s} | "
                       f"{_g(rep_.get('cost'))} | {_g(rep_.get('p50'))} | {_g(rep_.get('mem'))} | {r.get('decision', '—')} |")
        out.append("")
    # attribution / epistasis
    if rep.get("epistasis"):
        out.append("**Measured epistasis** (ε = gain(a+b) − gain(a) − gain(b); + synergy, − interference):\n")
        for e in rep["epistasis"][:8]:
            out.append(f"- `{e['gene_a']}` + `{e['gene_b']}`: ε = {e['epsilon']:+.4f} ({e['kind']})")
        out.append("")
    # bandit arm credit
    arms = sorted([a for a in res.get("arms", []) if a.get("pulls", 0) > 0], key=lambda a: -(a.get("mean_reward") or 0))
    if arms:
        out.append("**Operator credit** (bandit, mean reward per proposal):\n")
        out.append("| operator | model / template | pulls | mean reward |")
        out.append("|---|---|---|---|")
        for a in arms[:12]:
            mr = f"{a['mean_reward']*100:.1f}%" if a.get("mean_reward") is not None else "—"
            out.append(f"| {a['operator']} | {a.get('model') or ''} {a.get('template') or ''} | {a['pulls']} | {mr} |")
        out.append("")
    # LLM usage
    llm = rep.get("llm", {})
    if llm.get("calls"):
        out.append(f"**LLM usage**: {llm['calls']} calls ({llm['tokens_in']:,} in / {llm['tokens_out']:,} out tokens), "
                   f"local models {dict(llm.get('by_model', {}))}, ${llm.get('cost_usd', 0):.3f} compute-priced.\n")
    # A/A
    aa = rep.get("aa_test")
    if aa:
        out.append(aa_block(aa, "A/A noise floor"))
    # alerts / red-team
    breaches = [a for a in rep.get("alerts", []) if a["kind"] == "redteam_breach"]
    rt_l0 = sum(1 for e in events if e.get("kind") == "cascade.reject" and e.get("island") == "redteam")
    out.append(f"**Red-team island**: {rt_l0} attacks rejected at L0/L1, {kinds.get('redteam.caught', 0)} caught by the L2 oracle, "
               f"{kinds.get('redteam.inert', 0)} classified inert in-run, **{len(breaches)} breach alert(s)** raised in-run.\n")
    recheck = rep.get("redteam_recheck") or []
    if recheck:
        out.append("Re-adjudication of the breach alerts (`colloid redteam-recheck`: the maximal version of the same hack on the "
                   "same unit is sent through the oracle; caught = live channel = genuine breach, passes = inert attack):\n")
        out.append("| flagged program | unit | on a request path | hack | verdict |")
        out.append("|---|---|---|---|---|")
        for r in recheck:
            out.append(f"| `{r['program'][:10]}` | {r.get('unit', '—')} | {r.get('on_request_path', '—')} | {r.get('hack', '—')} | **{r.get('verdict')}** |")
        genuine = sum(1 for r in recheck if r.get("verdict") == "genuine breach")
        out.append(f"\n**Genuine evaluator breaches after re-adjudication: {genuine}.**\n")
    susp = [a for a in rep.get("alerts", []) if a["kind"] == "suspicion"]
    if susp:
        out.append(f"**Suspicion triggers**: {len(susp)} gains exceeded the 2× threshold and were sent to mandatory deep review.\n")
    return "\n".join(out)


def stress_block() -> str:
    out = []
    sb = Path("/opt/colloid/state/stress_sandbox.json")
    ev = Path("/opt/colloid/state/stress_evaluator.json")
    if sb.exists():
        d = json.loads(sb.read_text())
        out.append(f"**Concurrent sandbox stress** ({d['total_runs']} adversarial payloads across rounds, "
                   f"{d['elapsed_s']}s): {d['contained']}/{d['total_runs']} contained, "
                   f"cgroup leak {d['cgroup_leak']}, fd leak {d['fd_leak']}, filesystem escape {d['escaped_filesystem']}. "
                   f"**Overall: {'PASS' if d['ok'] else 'FAIL'}.**\n")
    if ev.exists():
        d = json.loads(ev.read_text())
        can = d.get("canaries", {})
        aa = d.get("aa", {})
        out.append(f"**Evaluator robustness stress** ({d.get('elapsed_s', 0)}s): "
                   f"all pathological genomes rejected = {d.get('pathologies_all_rejected')} "
                   f"(infinite loop killed fast = {d.get('infinite_loop_killed_fast')}); "
                   f"canary suite {can.get('rejected')}/{can.get('total')} rejected under stress; "
                   f"A/A promotions allowed = {aa.get('promotions_allowed')}. "
                   f"**Overall: {'PASS' if d.get('ok') else 'FAIL'}.**\n")
        if d.get("pathologies"):
            out.append("| pathological genome | rejected at | verdict |")
            out.append("|---|---|---|")
            for p in d["pathologies"]:
                out.append(f"| {p['name']} | {p['stage']} | {p['verdict']} |")
            out.append("")
        if aa.get("objectives"):
            out.append(aa_block(aa, "Stress A/A (independent of the run's own A/A)"))
    return "\n".join(out) if out else "_(stress reports not yet generated — run the harnesses in `stress/`.)_"


def replace_block(doc: str, name: str, body: str) -> str:
    pat = re.compile(rf"(<!-- RESULTS:{name} -->\n).*?(\n<!-- /RESULTS:{name} -->)", re.S)
    if not pat.search(doc):
        raise SystemExit(f"marker pair for RESULTS:{name} not found")
    return pat.sub(lambda m: m.group(1) + "\n" + body.strip() + "\n" + m.group(2), doc)


def write_evidence(run: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = Path(run).name
    (out_dir / f"{name}.report.json").write_text(json.dumps(build_report(run), indent=1, default=str))
    for f in ("stress_sandbox.json", "stress_evaluator.json"):
        src = Path("/opt/colloid/state") / f
        if src.exists():
            shutil.copy(src, out_dir / f)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run", nargs="?", default="runs/stackzero")
    ap.add_argument("--write", help="markdown file whose RESULTS blocks are replaced in place")
    ap.add_argument("--evidence", help="directory for the machine-readable reports")
    args = ap.parse_args()
    blocks = {"RUN": run_block(args.run), "STRESS": stress_block()}
    if args.write:
        doc = Path(args.write).read_text()
        for name, body in blocks.items():
            doc = replace_block(doc, name, body)
        Path(args.write).write_text(doc)
    else:
        for name, body in blocks.items():
            print(f"<!-- RESULTS:{name} -->\n\n{body}\n\n<!-- /RESULTS:{name} -->\n")
    if args.evidence:
        write_evidence(args.run, Path(args.evidence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
