#!/usr/bin/env bash
# After scripts/run-m1.sh: verify both arms, the transfer A/B, ablation of the best Go program,
# lake ingest, rule mining, the Python canary re-run, the bake-off, the ladder, the results doc.
# Every step is one Colloid process at a time (the evaluation cluster has a single owner).
set -eu
cd "$(dirname "$0")/.."
PY=${PY:-/opt/colloid/venv/bin/python}
C="$PY -m colloid.services.cli"
R=docs/results
mkdir -p $R

$C verify runs/stackzero-go --top 5 > runs/verify-go.log 2>&1 || true
$C verify runs/stackzero-go-primed --top 5 > runs/verify-go-primed.log 2>&1 || true
$C compare runs/stackzero-go runs/stackzero-go-primed --out $R/m1_transfer.json

# the best verified Go program across both arms (replicate cost gain, else L6 holdout), then its ablation
read -r BEST_RUN BEST_PID < <($PY - <<'EOF'
from colloid.adapters.store.sql_store import open_store
from colloid.core.models import ProgramStatus, Stage, Verdict
from colloid.core.objectives import gain_percent
best = (None, None, -1e9)
for run in ("runs/stackzero-go", "runs/stackzero-go-primed"):
    s = open_store(f"sqlite:///{run}/colloid.db")
    ver = {r["program"]: r for r in (s.kv_get("verification") or {}).get("programs", [])}
    for p in [*s.programs(status=ProgramStatus.PROMOTED), *s.programs(status=ProgramStatus.VERIFIED)]:
        if len(p.gene_ids) < 2:
            continue
        rep = (ver.get(p.id) or {}).get("replicate", {}).get("cost")
        if rep:
            g = rep["gain_pct"]
        else:
            l6 = [o for e in s.evaluations(p.id, stage=Stage.L6.value) if e.verdict == Verdict.PASS for o in e.objectives
                  if o.objective == "cost" and o.reference == "baseline"]
            g = gain_percent(l6[0].log_ratio) if l6 else -1e9
        if g > best[2]:
            best = (run, p.id, g)
    s.close()
print(best[0] or "-", best[1] or "-")
EOF
)
if [ "$BEST_PID" != "-" ]; then
  $C verify "$BEST_RUN" --ablate "$BEST_PID" > runs/ablation-go.log 2>&1 || true
fi

$C lake ingest runs/stackzero-go
$C lake ingest runs/stackzero-go-primed
$C lake verify
$C rules mine --commit --out $R/rules.crl > runs/rules-mine.log
$PY - <<'EOF'
import json
from colloid.adapters.lake import open_lake
from colloid.adapters.target import open_target
from colloid.services import rules
lake = open_lake("git:colloid/datalake")
loaded = rules.load_rules(lake)
rows = []
for name in ("stackzero", "stackzero-go", "stackzero-node"):
    t = open_target(name, observe_system=False)
    for m in rules.apply(loaded, t.atlas_seed(), t.knobs(), t.schema_sql()):
        rows.append({"target": name, "rule": m.proposal.rule, "index": m.proposal.render(), "knob": m.knob, "exact": m.exact,
                     "queries": len(m.proposal.queries)})
json.dump(rows, open("docs/results/rules_applied.json", "w"), indent=2)
EOF

$C canaries --out $R/canaries.json > runs/canaries_py.log 2>&1 || true
$C bakeoff --out $R/bakeoff.json > runs/bakeoff.log 2>&1
$C ladder --cold runs/stackzero-go --primed runs/stackzero-go-primed --out $R/ladder.json
$PY stress/render_polyglot.py --write docs/POLYGLOT_RESULTS.md
echo "post-M1 done" > runs/post-m1.done
