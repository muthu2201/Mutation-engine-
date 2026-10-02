"""Render the measured results of a run (and the stress JSON) into the markdown blocks that
replace the RESULTS placeholders in docs/INITIAL_RESULTS.md. Pure projection of the run's own
store + telemetry + stress reports — no hand-entered numbers.

    python stress/render_results.py runs/stackzero > /tmp/blocks.md
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from colloid.adapters.telemetry.jsonl import read_events
from colloid.services.report import build_report


def _fmt_ci(g: dict) -> str:
    return f"{g['pct']:+.1f}% (CI [{g['ci_pct'][1]:+.1f}%, {g['ci_pct'][0]:+.1f}%], p={g['p']:.3f})"


def run_block(run: str) -> str:
    rep = build_report(run)
    events = read_events(Path(run) / "events.jsonl")
    kinds = Counter(e.get("kind") for e in events)
    res = rep.get("result", {})
    out = []
    setup = rep["setup"]
    out.append(f"**Run `{rep['run']}`** — rate {setup.get('rate_rps')} rps (knee ≈ {setup.get('calibration_knee')} rps), "
               f"{res.get('generations', '?')} generations, {res.get('programs_evaluated', 0)} programs evaluated of "
               f"{res.get('programs_total', 0)} proposed, {len(rep.get('promoted', []))} promoted, "
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
        out.append("**A/A noise floor** (identical program vs itself, false-positive rate must be ≤ α=5%):\n")
        out.append("| objective | false-positive rate | CI covers 0 | effect SD |")
        out.append("|---|---|---|---|")
        for obj, s in aa.get("objectives", {}).items():
            out.append(f"| {obj} | {s['false_positive_rate']:.0%} | {s['ci_coverage_of_zero']:.0%} | {s['effect_sd']:.4f} |")
        out.append(f"\nPromotions allowed: **{aa.get('promotions_allowed')}** (primary-objective false-positive rate within α).\n")
    # alerts / red-team
    breaches = [a for a in rep.get("alerts", []) if a["kind"] == "redteam_breach"]
    out.append(f"**Red-team island**: {kinds.get('redteam.caught', 0)} attacks generated and caught by the oracle, "
               f"**{len(breaches)} evaluator breaches** (a breach would be a candidate that fooled the evaluator).\n")
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
    return "\n".join(out) if out else "_(stress reports not yet generated — run the harnesses in `stress/`.)_"


def main() -> int:
    run = sys.argv[1] if len(sys.argv) > 1 else "runs/stackzero"
    print("<!-- RESULTS:RUN -->\n")
    print(run_block(run))
    print("\n<!-- RESULTS:STRESS -->\n")
    print(stress_block())
    return 0


if __name__ == "__main__":
    sys.exit(main())
