#!/usr/bin/env bash
# Copy a SWE-bench run's evidence into the repository (runs/ is not committed):
# results.jsonl and, per instance, record.json, submission.diff and the official report.json.
#   scripts/export-swebench-evidence.sh local runs/swebench
set -euo pipefail
cd "$(dirname "$0")/.."
ARM=$1 RUN=$2 DEST=docs/results/swebench/$1
mkdir -p "$DEST"
cp "$RUN/results.jsonl" "$DEST/"
for d in "$RUN"/*/; do
  id=$(basename "$d")
  [ -f "$d/record.json" ] || continue
  mkdir -p "$DEST/$id"
  for f in record.json submission.diff report.json; do [ -f "$d/$f" ] && cp "$d/$f" "$DEST/$id/"; done
done
echo "exported $(wc -l < "$DEST/results.jsonl") instances to $DEST"
