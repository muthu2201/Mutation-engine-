#!/usr/bin/env bash
# After scripts/post-m1.sh and the soak fix (ADR 0010): both canary suites, the soak calibration,
# the bake-off, the ladder, the results doc. One Colloid process at a time.
set -eu
cd "$(dirname "$0")/.."
PY=${PY:-/opt/colloid/venv/bin/python}
C="$PY -m colloid.services.cli"
R=docs/results

$C canaries --out $R/canaries.json > runs/canaries_py.log 2>&1 || true
$C canaries --target stackzero-go --out $R/canaries_go.json > runs/canaries_go.log 2>&1 || true
$PY stress/soak_calibration.py --run runs/stackzero-go --out $R/soak_calibration.json > runs/soak_calibration.log 2>&1
$C bakeoff --out $R/bakeoff.json > runs/bakeoff.log 2>&1
$C ladder --cold runs/stackzero-go --primed runs/stackzero-go-primed --out $R/ladder.json
$PY stress/render_polyglot.py --write docs/POLYGLOT_RESULTS.md
echo "post-M1 B done" > runs/post-m1b.done
