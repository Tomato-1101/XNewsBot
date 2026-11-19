#!/bin/bash
# GitHub Actions から AI解説を1件作る(cloud/DESIGN.md の explain.yml)。
#   cloud/actions/explain.sh <newsexplanation.id>
# 行は Worker が確保済み。生成 → 結果列だけ D1 に戻す → push_to へ1通送る(送信は scripts/run_explain.py)。
set -uo pipefail
ID="${1:-}"
case "$ID" in
  ''|*[!0-9]*) echo "explanation_id は数字で指定する: '$ID'" >&2; exit 2 ;;
esac

. "$(dirname "$0")/lib.sh"

pull_or_die
"$PY" scripts/run_explain.py --id "$ID"
finish $?
