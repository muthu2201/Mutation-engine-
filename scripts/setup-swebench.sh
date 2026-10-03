#!/usr/bin/env bash
# Prepare a fresh session for the SWE-bench track (ADR 0011, ADR 0012). Idempotent; run as root from the repo.
#   1. the Colloid venv (/opt/colloid/venv), via scripts/provision-linux.sh if it is missing
#   2. the grader's own venv with the official swebench package
#   3. the dataset (sha256-checked) and the pre-registered sample (must equal the committed one)
#   4. the Docker daemon
#   5. whether OPENROUTER_API_KEY is set (never printed)
set -euo pipefail
cd "$(dirname "$0")/.."
SWE=/opt/colloid/state/swebench
PARQUET_SHA=030cfd7f2a704c4c0226e7f104c725a3b41230b1d3517f9c915ad7ea5be3fa25
PY=/opt/colloid/venv/bin/python

if [ ! -x "$PY" ]; then
  echo "==> Colloid venv missing: provisioning"
  scripts/provision-linux.sh
fi

echo "==> grader venv"
mkdir -p "$SWE" /opt/colloid/logs
if [ ! -x "$SWE/venv/bin/python" ]; then
  if command -v uv >/dev/null; then
    uv venv -q -p python3.12 "$SWE/venv" && uv pip install -q -p "$SWE/venv/bin/python" swebench==5.0.2 pandas==3.0.6 pyarrow==25.0.1
  else
    python3.12 -m venv "$SWE/venv" && "$SWE/venv/bin/pip" install -q swebench==5.0.2 pandas==3.0.6 pyarrow==25.0.1
  fi
fi

echo "==> dataset and the pre-registered sample"
if ! echo "$PARQUET_SHA  $SWE/verified.parquet" | sha256sum -c --quiet 2>/dev/null; then
  curl -sSL --fail -o "$SWE/verified.parquet" "https://huggingface.co/api/datasets/SWE-bench/SWE-bench_Verified/parquet/default/test/0.parquet"
  echo "$PARQUET_SHA  $SWE/verified.parquet" | sha256sum -c --quiet
fi
"$PY" -m colloid.services.cli swebench prepare
"$PY" - "$SWE/data/sample.json" docs/results/swebench/sample.json <<'PY'
import json, sys
a, b = (json.load(open(p)) for p in sys.argv[1:3])
assert a["instances"] == b["instances"], "the draw does not reproduce the committed pre-registered sample"
print(f"sample reproduces: {len(a['instances'])} instances")
PY

echo "==> docker"
if ! docker info >/dev/null 2>&1; then
  mkdir -p /opt/colloid/state/docker
  setsid nohup dockerd --data-root /opt/colloid/state/docker > /opt/colloid/logs/dockerd.log 2>&1 < /dev/null &
  for _ in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
fi
docker info --format 'docker {{.ServerVersion}} up'

if [ -n "${OPENROUTER_API_KEY:-}" ]; then
  echo "==> OPENROUTER_API_KEY is set"
else
  echo "==> OPENROUTER_API_KEY is NOT set: add it in the environment's settings, then start a new session (needed for the API arm only)"
fi
