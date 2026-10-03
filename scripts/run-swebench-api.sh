#!/usr/bin/env bash
# The API arm (ADR 0012 and its amendment 2), then its evidence export, the memorisation probes and the
# two-arm render. Resumable: re-running continues with the instances not yet in runs/swebench-api/results.jsonl.
#   PROVIDER=nvidia (default; NVIDIA_API_KEY, effort from scripts/pilot-swebench-api.sh) or PROVIDER=openrouter
set -u
cd "$(dirname "$0")/.."
PY=/opt/colloid/venv/bin/python
PROVIDER=${PROVIDER:-nvidia}
ARGS=(--provider "$PROVIDER")
if [ "$PROVIDER" = nvidia ]; then
  : "${NVIDIA_API_KEY:?add NVIDIA_API_KEY in the environment settings and start a new session}"
  EFFORT=$("$PY" -c 'import json; print(json.load(open("docs/results/swebench/api_pilot.json"))["chosen"] or "")' 2>/dev/null)
  [ -n "$EFFORT" ] || { echo "run scripts/pilot-swebench-api.sh first: the arm's reasoning effort is fixed by the pilot"; exit 1; }
  ARGS+=(--reasoning-effort "$EFFORT")
else
  : "${OPENROUTER_API_KEY:?add OPENROUTER_API_KEY in the environment settings and start a new session}"
fi
mkdir -p runs
PYTHONUNBUFFERED=1 "$PY" -m colloid.services.cli swebench run "${ARGS[@]}" --out runs/swebench-api >> runs/swebench-api.log 2>&1
echo "exit $?" >> runs/swebench-api.log
scripts/export-swebench-evidence.sh api runs/swebench-api
# post-hoc memorisation probes for the API model (ADR 0012 amendment): 2 requests per instance
PYTHONUNBUFFERED=1 "$PY" -m colloid.services.cli swebench probe "${ARGS[@]}" --out runs/swebench-api >> runs/swebench-api.log 2>&1
"$PY" stress/render_swebench.py --arm local=docs/results/swebench/local --arm api=docs/results/swebench/api \
  --probes local=docs/results/swebench/contamination_local.json --probes api=docs/results/swebench/contamination_api.json \
  --write docs/SWEBENCH_RESULTS.md
