#!/usr/bin/env bash
# M1 transfer A/B on the Go implementation (ADR 0008): arm A cold, then arm B lake-primed.
# Same budget, seed and judge; the only difference is the lake. Logs in runs/<name>.console.log.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-/opt/colloid/venv/bin/python}
run_arm() {
  local name=$1 cfg=$2
  rm -rf "runs/$name"
  PYTHONUNBUFFERED=1 timeout 12000 "$PY" -m colloid.services.cli run "$cfg" > "runs/$name.console.log" 2>&1
  echo "exit $?" >> "runs/$name.console.log"
}
run_arm stackzero-go experiments/stackzero-go.yaml
run_arm stackzero-go-primed experiments/stackzero-go-primed.yaml
echo "M1 done" > runs/m1.done
