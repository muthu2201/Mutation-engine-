#!/usr/bin/env bash
# Arm B of M1 alone (lake-primed). Used when arm A finished but arm B was interrupted:
# attempt 1 of arm B died with the container at generation 4 and is kept as
# runs/stackzero-go-primed-attempt1-killed. Same config, budget, seed and judge as arm A.
set -u
cd "$(dirname "$0")/.."
PY=${PY:-/opt/colloid/venv/bin/python}
rm -rf runs/stackzero-go-primed
PYTHONUNBUFFERED=1 timeout 12000 "$PY" -m colloid.services.cli run experiments/stackzero-go-primed.yaml > runs/stackzero-go-primed.console.log 2>&1
echo "exit $?" >> runs/stackzero-go-primed.console.log
echo "M1 done" > runs/m1.done
