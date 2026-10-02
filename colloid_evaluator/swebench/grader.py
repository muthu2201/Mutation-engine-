"""The holdout side of the SWE-bench track (ADR 0011). Runs in the grader's own virtualenv, where
the official ``swebench`` package is installed; Colloid's environment never imports it.

    python grader.py prepare --parquet verified.parquet --out DIR      # tasks.jsonl, gold.jsonl, sample.json
    python grader.py parse-log --repo R --version V < log              # official per-repo log parser -> JSON
    python grader.py grade --gold gold.jsonl --instance ID --patch P --image IMG --out report.json
    python grader.py diagnose --gold gold.jsonl --record record.json   # localisation vs the gold patch (post hoc)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SEARCH_FIELDS = ("instance_id", "repo", "version", "base_commit", "problem_statement", "created_at", "difficulty")
GOLD_FIELDS = ("instance_id", "repo", "version", "patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "eval_script",
               "log_parser", "eval_type", "environment_setup_commit", "image")
STRATUM = "<15 min fix"
SEED, N = 20261002, 30
GHCR = "ghcr.io/epoch-research/swe-bench.eval.x86_64.{}:latest"


def _listish(v):  # parquet stores the test lists as numpy arrays or JSON strings
    if isinstance(v, str):
        return json.loads(v)
    return [str(x) for x in v]


def cmd_prepare(args: argparse.Namespace) -> int:
    import numpy as np
    import pandas as pd

    from colloid_evaluator.swebench import repos

    raw = Path(args.parquet).read_bytes()
    t = pd.read_parquet(args.parquet).sort_values("instance_id").reset_index(drop=True)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "tasks.jsonl").open("w") as fh:
        for _, r in t.iterrows():
            fh.write(json.dumps({**{k: str(r[k]) for k in SEARCH_FIELDS}, "image": GHCR.format(r["instance_id"])}) + "\n")
    with (out / "gold.jsonl").open("w") as fh:
        for _, r in t.iterrows():
            row = {k: (r[k] if k not in ("FAIL_TO_PASS", "PASS_TO_PASS") else _listish(r[k])) for k in GOLD_FIELDS}
            fh.write(json.dumps({k: (v if isinstance(v, list) else str(v)) for k, v in row.items()}) + "\n")
    stratum = t[t.difficulty == STRATUM].reset_index(drop=True)
    idx = sorted(np.random.default_rng(args.seed).choice(len(stratum), args.n, replace=False).tolist())
    chosen = stratum.iloc[idx]
    checks = []
    for _, r in chosen.iterrows():  # the repo-level test command must be the one the instance is graded with
        lines = r.eval_script.splitlines()
        cmd = lines[next(i for i, ln in enumerate(lines) if "Start Test Output" in ln) + 1]
        checks.append({"instance_id": r.instance_id, "test_cmd_matches": cmd.startswith(repos.spec(r.repo).test_cmd)})
    sample = {"dataset": "SWE-bench/SWE-bench_Verified", "parquet_sha256": hashlib.sha256(raw).hexdigest(), "stratum": STRATUM,
              "stratum_size": len(stratum), "seed": args.seed, "n": args.n,
              "draw": f"numpy.random.default_rng({args.seed}).choice({len(stratum)}, {args.n}, replace=False)",
              "instances": list(chosen.instance_id), "checks": checks}
    (out / "sample.json").write_text(json.dumps(sample, indent=2))
    bad = [c["instance_id"] for c in checks if not c["test_cmd_matches"]]
    print(f"{len(t)} tasks, sample of {args.n} from {len(stratum)}; test command mismatches: {bad or 'none'}")
    return 0


def _spec(repo: str, version: str, **kw):
    from swebench.types import TestSpec

    return TestSpec(instance_id=kw.get("instance_id", "search"), image=kw.get("image", ""), eval_script_list=kw.get("eval_script_list", []),
                    repo=repo, version=version, FAIL_TO_PASS=kw.get("f2p", []), PASS_TO_PASS=kw.get("p2p", []),
                    log_parser=kw.get("log_parser", ""), eval_type=kw.get("eval_type", ""))


def cmd_parse_log(args: argparse.Namespace) -> int:
    from swebench.harness.log_parsers.python import MAP_REPO_TO_PARSER_PY

    log = sys.stdin.read()
    print(json.dumps(MAP_REPO_TO_PARSER_PY[args.repo](log, _spec(args.repo, args.version))))
    return 0


def _gold(path: str, instance_id: str) -> dict:
    with open(path) as fh:
        for line in fh:
            row = json.loads(line)
            if row["instance_id"] == instance_id:
                return row
    raise KeyError(instance_id)


def cmd_grade(args: argparse.Namespace) -> int:
    from swebench.harness.run_evaluation import run_instance

    import docker

    g = _gold(args.gold, args.instance)
    script = [ln for ln in g["eval_script"].splitlines() if ln not in ("#!/bin/bash", "set -uxo pipefail")]
    spec = _spec(g["repo"], g["version"], instance_id=g["instance_id"], image=args.image, eval_script_list=script,
                 f2p=g["FAIL_TO_PASS"], p2p=g["PASS_TO_PASS"], log_parser=g["log_parser"], eval_type=g["eval_type"])
    pred = {"instance_id": g["instance_id"], "model_name_or_path": "colloid", "model_patch": Path(args.patch).read_text()}
    _, report = run_instance(spec, pred, docker.from_env(timeout=args.timeout), args.run_id, timeout=args.timeout)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in (report or {}).get(g["instance_id"], {}).items() if k != "tests_status"}))
    return 0


def _hunks(patch: str) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges (in the original file) of a unified diff, per file."""
    out: dict[str, list[tuple[int, int]]] = {}
    cur = None
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            cur = line[6:]
            out.setdefault(cur, [])
        m = re.match(r"@@ -(\d+)(?:,(\d+))? ", line)
        if m and cur:
            start, n = int(m.group(1)), int(m.group(2) or 1)
            out[cur].append((start, start + max(n, 1) - 1))
    return out


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Post hoc, for the report only: did localisation reach the gold patch's files and lines?"""
    rec = json.loads(Path(args.record).read_text())
    g = _gold(args.gold, rec["instance_id"])
    gold = _hunks(g["patch"])
    snips = rec.get("localised", [])
    file_hit = any(s["file"] in gold for s in snips)
    line_hit = any(s["file"] in gold and any(a <= s["end"] and s["start"] <= b for a, b in gold[s["file"]]) for s in snips)
    sub = rec.get("submission") or {}
    sub_files = sorted({m.group(1) for m in re.finditer(r"^diff --git a/(\S+)", sub.get("diff", ""), re.M)})
    print(json.dumps({"gold_files": sorted(gold), "localised_file_hit": file_hit, "localised_line_hit": line_hit,
                      "submission_files": sub_files, "submission_in_gold_file": bool(set(sub_files) & set(gold)),
                      "gold_changed_lines": sum(1 for ln in g["patch"].splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---")))}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--parquet", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--n", type=int, default=N)
    p = sub.add_parser("parse-log")
    p.add_argument("--repo", required=True)
    p.add_argument("--version", required=True)
    p = sub.add_parser("grade")
    for a in ("--gold", "--instance", "--patch", "--image", "--out"):
        p.add_argument(a, required=True)
    p.add_argument("--run-id", default="colloid")
    p.add_argument("--timeout", type=int, default=1800)
    p = sub.add_parser("diagnose")
    p.add_argument("--gold", required=True)
    p.add_argument("--record", required=True)
    args = ap.parse_args()
    return {"prepare": cmd_prepare, "parse-log": cmd_parse_log, "grade": cmd_grade, "diagnose": cmd_diagnose}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
