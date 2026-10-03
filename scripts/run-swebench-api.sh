#!/usr/bin/env bash
# The API arm (ADR 0012), then its evidence export and the two-arm render. Resumable: re-running
# continues with the instances not yet in runs/swebench-api/results.jsonl.
set -u
cd "$(dirname "$0")/.."
PY=/opt/colloid/venv/bin/python
: "${OPENROUTER_API_KEY:?add OPENROUTER_API_KEY in the environment settings and start a new session}"
mkdir -p runs
PYTHONUNBUFFERED=1 "$PY" -m colloid.services.cli swebench run --provider openrouter --out runs/swebench-api >> runs/swebench-api.log 2>&1
echo "exit $?" >> runs/swebench-api.log
scripts/export-swebench-evidence.sh api runs/swebench-api
"$PY" stress/render_swebench.py --arm local=docs/results/swebench/local --arm api=docs/results/swebench/api --write docs/SWEBENCH_RESULTS.md
