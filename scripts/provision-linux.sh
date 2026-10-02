#!/usr/bin/env bash
# Provision a Colloid bench host: Ubuntu 24.04 (x86_64 or arm64), run as root. Idempotent.
#
# Installs exactly what the engine and evaluator expect at their fixed locations:
#   PostgreSQL 16            /usr/lib/postgresql/16/bin   (StackZero's database; cluster lives in $COLLOID_STATE/pg)
#   gcc + clang              the C side of the Atlas (clang -ast-dump=json), native builds, sbx-exec
#   allocators               jemalloc, tcmalloc-minimal, mimalloc (the alloc.* knobs, via LD_PRELOAD)
#   colloid-loadgen          /opt/colloid/bin             (Go open-loop load generator)
#   sbx-exec                 /opt/colloid/bin             (seccomp/namespace sandbox helper)
#   colloid-sbx user         the unprivileged identity candidates run as
#   Python venv              /opt/colloid/venv            (pip install -e ".[$COLLOID_EXTRAS]")
#   models (optional)        /opt/colloid/models          (COLLOID_MODELS=1: local Qwen2.5-Coder GGUFs for LLM arms)
#
# Usage:  sudo scripts/provision-linux.sh
#         sudo COLLOID_EXTRAS=dev,llm-local,anthropic COLLOID_MODELS=1 scripts/provision-linux.sh
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/colloid
EXTRAS="${COLLOID_EXTRAS:-dev}"
export DEBIAN_FRONTEND=noninteractive

if [ "$(id -u)" -ne 0 ]; then
  echo "provision-linux.sh must run as root" >&2
  exit 1
fi

echo "==> system packages"
apt-get update -q
apt-get install -y -q --no-install-recommends \
  ca-certificates curl git sudo procps util-linux \
  build-essential gcc clang golang-go \
  python3.12 python3.12-venv python3.12-dev \
  postgresql-16 \
  libjemalloc2 libtcmalloc-minimal4t64 libmimalloc2.0

echo "==> layout"
mkdir -p "$PREFIX/bin" "$PREFIX/state" "$PREFIX/logs" "$PREFIX/models"

echo "==> load generator"
(cd "$REPO/colloid/adapters/bench/loadgen" && go test ./... && go build -trimpath -o "$PREFIX/bin/colloid-loadgen" .)

echo "==> sandbox helper + user"
gcc -O2 -Wall -Wextra -Werror -o "$PREFIX/bin/sbx-exec" "$REPO/colloid/adapters/sandbox/sbx_exec.c"
id colloid-sbx >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin colloid-sbx

echo "==> python environment ($EXTRAS)"
[ -x "$PREFIX/venv/bin/python" ] || python3.12 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q -e "$REPO[$EXTRAS]"

if [ "${COLLOID_MODELS:-0}" = "1" ]; then
  echo "==> local LLM models"
  base=https://huggingface.co/Qwen
  for m in 3b 1.5b; do
    f="$PREFIX/models/qwen2.5-coder-$m-instruct-q4_k_m.gguf"
    [ -s "$f" ] || curl -fL --retry 3 -o "$f" "$base/Qwen2.5-Coder-${m^^}-Instruct-GGUF/resolve/main/qwen2.5-coder-$m-instruct-q4_k_m.gguf"
  done
fi

echo "==> check"
"$PREFIX/venv/bin/python" - <<'PY'
import json
from colloid.adapters.platform import capabilities
from colloid.adapters.sandbox.linux import cgroup_mode
caps = capabilities().to_dict()
caps["cgroup_mode"] = cgroup_mode()
print(json.dumps(caps, indent=1))
PY
echo "provisioned: run 'sudo COLLOID_INTEGRATION=1 $PREFIX/venv/bin/pytest $REPO/tests' to verify the full stack"
