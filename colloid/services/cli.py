"""Colloid command-line interface.

    colloid run EXPERIMENT.yaml          run an evolutionary optimisation
    colloid canaries                     run the reward-hacking canary suite (CI gate)
    colloid aa --runs N                  run an A/A noise-floor test on the baseline
    colloid profile                      build + profile the Atlas, print leverage
    colloid atlas                        print the Stack Atlas summary
    colloid report RUN                   summarise a finished run from its store
    colloid dashboard RUN [--port 8080]  serve the live dashboard for a run
    colloid baseline                     measure the baseline and print the SLO/cost

Experiments are YAML files under experiments/ (data, never code).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _providers(cfg: Any) -> dict[str, Any]:
    from colloid.adapters.llm.anthropic_provider import AnthropicProvider
    from colloid.adapters.llm.llama_server import LlamaServer
    from colloid.adapters.llm.openai_compat import OpenAICompatProvider

    providers: dict[str, Any] = {}
    local_models = [a.model for a in cfg.llm_arms if a.provider == "local"]
    if local_models:
        model_files = {
            "qwen2.5-coder-3b": "/opt/colloid/models/qwen2.5-coder-3b-instruct-q4_k_m.gguf",
            "qwen2.5-coder-1.5b": "/opt/colloid/models/qwen2.5-coder-1.5b-instruct-q4_k_m.gguf",
        }
        wanted = {m: model_files[m] for m in local_models if m in model_files}
        server = LlamaServer(wanted)
        server.start()
        providers["local"] = OpenAICompatProvider(server.base_url, list(wanted), name="local")
        providers["_llama_server"] = server  # kept alive; engine ignores non-provider keys via models()
    anthropic_models = [a.model for a in cfg.llm_arms if a.provider == "anthropic"]
    if anthropic_models:
        providers["anthropic"] = AnthropicProvider(tuple(dict.fromkeys(anthropic_models)))
    return providers


def cmd_run(args: argparse.Namespace) -> int:
    from colloid.services.config import EngineConfig
    from colloid.services.engine import Engine

    cfg = EngineConfig.load(args.experiment)
    if args.generations:
        cfg.generations = args.generations
    if args.name:
        cfg.name = args.name
    providers = {k: v for k, v in _providers(cfg).items()}
    engine = Engine(cfg, providers={k: v for k, v in providers.items() if not k.startswith("_")})
    try:
        result = engine.run()
    finally:
        srv = providers.get("_llama_server")
        if srv is not None:
            srv.stop()
    print(json.dumps({k: v for k, v in result.items() if k not in ("arms",)}, indent=2, default=str))
    return 0


def cmd_canaries(args: argparse.Namespace) -> int:
    from colloid.adapters.cost.static_prices import StaticPriceCostModel
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget
    from colloid_evaluator.canaries.hacks import run_canaries
    from colloid_evaluator.cascade import Evaluator

    ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=args.rate)
    ev.setup("baseline")
    try:
        report = run_canaries(ev, dynamic_only=not args.no_dynamic)
    finally:
        ev.shutdown()
    Path(args.out).write_text(json.dumps(report, indent=2)) if args.out else None
    print(f"\n{report['rejected']}/{report['total']} canaries rejected  (all_rejected={report['all_rejected']})")
    return 0 if report["all_rejected"] else 1


def cmd_aa(args: argparse.Namespace) -> int:
    from colloid.adapters.cost.static_prices import StaticPriceCostModel
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget
    from colloid_evaluator.cascade import Evaluator

    ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=args.rate)
    ev.setup("baseline")
    try:
        report = ev.aa_test(args.runs, on_run=lambda i, row: print(f"  run {i}: " + " ".join(f"{k}={v['log_ratio']:+.4f}(p={v['p']:.2f})" for k, v in row.items())))
    finally:
        ev.shutdown()
    print(json.dumps(report, indent=2))
    Path(args.out).write_text(json.dumps(report, indent=2)) if args.out else None
    return 0 if report["promotions_allowed"] else 1


def cmd_profile(args: argparse.Namespace) -> int:
    from colloid.adapters.cost.static_prices import StaticPriceCostModel
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget
    from colloid.core.models import UnitKind
    from colloid_evaluator.profiler import Bench, CausalProfiler, ProfileConfig, decorate_atlas
    from colloid_evaluator.workloads import Universe

    target = StackZeroTarget()
    target.prepare()
    with target.pg.superuser("shop_template") as c:
        universe = Universe.load(c)
    bench = Bench(target, universe, StaticPriceCostModel(), rate=args.rate)
    prof = CausalProfiler(target, bench, ProfileConfig())
    latency = prof.latency_share(None)
    atlas = target.atlas_seed()
    code_units = [u.id for p in atlas.paths for u in [atlas.units[x] for x in p.unit_ids]
                  if u.kind == UnitKind.FUNCTION and u.layer.value == "svc" and u.tags.get("language") == "python"]
    lev = prof.causal_leverage(list(dict.fromkeys(code_units))[: prof.cfg.top_units], log=print)
    decorate_atlas(atlas, latency, lev)
    print("\nlatency share by endpoint:")
    for ep, share in sorted(latency.latency_share.items(), key=lambda kv: -kv[1]):
        name = ep.split(":", 1)[1] if ":" in ep else ep
        print(f"  {name:40} {share:6.1%}")
    print("\ncausal leverage by unit (end-to-end gain per unit local speedup):")
    for uid, curve in sorted(lev.leverage.items(), key=lambda kv: -kv[1].slope):
        print(f"  {atlas.units[uid].name:45} {curve.slope:+.2f}  CI[{curve.slope_ci[0]:+.2f},{curve.slope_ci[1]:+.2f}]")
    target.shutdown()
    return 0


def cmd_atlas(args: argparse.Namespace) -> int:
    from collections import Counter

    from colloid.adapters.target.stackzero.adapter import StackZeroTarget

    target = StackZeroTarget(observe_system=True)
    atlas = target.atlas_seed()
    print(f"baseline id: {target.baseline_id()}")
    print("units:", dict(Counter(u.kind.value for u in atlas.units.values())))
    print("edges:", dict(Counter(e.kind.value for e in atlas.edges)))
    print("loci:", len(atlas.loci), " knobs:", sum(1 for u in atlas.units.values() if u.kind.value == "knob"))
    print("\nregions:")
    for r in target.regions():
        loci = atlas.loci_in(r)
        print(f"  {r.name:14} {len(loci):3} loci  - {r.description}")
    print("\nrequest paths (static):")
    for p in atlas.paths:
        layers = sorted({atlas.units[u].layer.value for u in p.unit_ids})
        print(f"  {atlas.units[p.unit_ids[0]].name:40} {len(p.unit_ids):2} units  layers={layers}")
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    from colloid.adapters.cost.static_prices import StaticPriceCostModel
    from colloid.adapters.target.stackzero.adapter import StackZeroTarget
    from colloid_evaluator.cascade import Evaluator

    ev = Evaluator(StackZeroTarget(), StaticPriceCostModel(), rate=args.rate)
    info = ev.setup("baseline")
    from colloid_evaluator.protocol import L5, Arm, summary

    cmp = ev.bench.compare([Arm("baseline", "baseline", ev.baseline_ws)], L5, seed=1)
    s = summary(cmp, "baseline", ev.bench.usd_cpu_s, ev.bench.usd_gb_s)
    print(json.dumps({"rate_rps": info["rate_rps"], "calibration_knee": info["calibration"]["knee_rps"] if info["calibration"] else None,
                      "metrics": {k: {"point": round(v[0], 4), "ci": [round(v[1], 4), round(v[2], 4)]} for k, v in s.items()}}, indent=2))
    ev.shutdown()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from colloid.services.report import build_report

    print(json.dumps(build_report(args.run), indent=2, default=str))
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from colloid.services.dashboard import serve

    serve(args.run, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="colloid", description="AI-driven cross-layer mutation engine")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("experiment"); r.add_argument("--generations", type=int); r.add_argument("--name"); r.set_defaults(fn=cmd_run)
    c = sub.add_parser("canaries"); c.add_argument("--rate", type=float, default=45.0); c.add_argument("--no-dynamic", action="store_true"); c.add_argument("--out"); c.set_defaults(fn=cmd_canaries)
    a = sub.add_parser("aa"); a.add_argument("--runs", type=int, default=20); a.add_argument("--rate", type=float, default=45.0); a.add_argument("--out"); a.set_defaults(fn=cmd_aa)
    pr = sub.add_parser("profile"); pr.add_argument("--rate", type=float, default=45.0); pr.set_defaults(fn=cmd_profile)
    at = sub.add_parser("atlas"); at.set_defaults(fn=cmd_atlas)
    b = sub.add_parser("baseline"); b.add_argument("--rate", type=float, default=45.0); b.set_defaults(fn=cmd_baseline)
    rp = sub.add_parser("report"); rp.add_argument("run"); rp.set_defaults(fn=cmd_report)
    d = sub.add_parser("dashboard"); d.add_argument("run"); d.add_argument("--port", type=int, default=8080); d.set_defaults(fn=cmd_dashboard)
    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
