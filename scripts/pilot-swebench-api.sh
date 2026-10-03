#!/usr/bin/env bash
# The API arm's pilot (ADR 0012, amendment 2): run the engine on the two pilot instances (outside the
# pre-registered sample) at each candidate reasoning effort, then fix the arm's effort by the declared
# rule (stress/api_pilot.py) in docs/results/swebench/api_pilot.json. Run once, before run-swebench-api.sh.
set -u
cd "$(dirname "$0")/.."
PY=/opt/colloid/venv/bin/python
: "${NVIDIA_API_KEY:?add NVIDIA_API_KEY in the environment settings and start a new session}"
PILOT=$("$PY" -c 'import json; print(" ".join("--instance " + i for i in json.load(open("/opt/colloid/state/swebench/data/pilot.json"))["instances"]))')
mkdir -p runs
for effort in high low; do
  # shellcheck disable=SC2086
  PYTHONUNBUFFERED=1 "$PY" -m colloid.services.cli swebench run --provider nvidia --reasoning-effort "$effort" $PILOT \
    --out "runs/pilot-nvidia-$effort" >> runs/pilot-nvidia.log 2>&1
done
"$PY" stress/api_pilot.py --arm high=runs/pilot-nvidia-high --arm low=runs/pilot-nvidia-low --write docs/results/swebench/api_pilot.json
