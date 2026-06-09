#!/bin/bash
# XNewsBot 定期キュレーション(launchd から朝07:40/夜20:40に起動)。
# 流れ: collect(購読者の有効ジャンル) → ヘッドレス Claude Code がキュレーション → ingest。
# slot は現在時刻から判定(15時より前=morning, 以降=evening)。
# 配信(push)自体は常駐サーバ(com.tomato.xnewsbot)の tick が各購読者の時刻に行う。
set -uo pipefail

PROJ="/Users/tomato/Project/XNewsBot"
PY="$PROJ/.venv/bin/python"
CLAUDE="/Users/tomato/.local/bin/claude"
LOG="$HOME/Library/Logs/xnewsbot-curate.log"
export PATH="/Users/tomato/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$PROJ" || exit 1

HOUR=$(date +%H)
if [ "$HOUR" -lt 15 ]; then SLOT=morning; else SLOT=evening; fi
RAW="/tmp/xnews_${SLOT}_raw.json"
CUR="/tmp/xnews_${SLOT}_curated.json"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
log "==== curate start slot=$SLOT ===="

# 1) 収集。購読者ゼロ等で対象ジャンルが無ければ pipeline が非0終了 → 正常スキップ。
if ! "$PY" scripts/pipeline.py collect --due --slot "$SLOT" --out "$RAW" >> "$LOG" 2>&1; then
  log "collect をスキップ(購読者/対象ジャンルなし)"; exit 0
fi

# 2) キュレーション(ヘッドレス Claude Code)。入出力パスをプロンプトへ埋め込む。
#    安全のため許可ツールは Read/Write のみ(Bash や任意操作は不可)。
#    入力 raw を読み、curated JSON を書くだけに能力を限定する。
PROMPT="$(sed -e "s#__RAW__#$RAW#g" -e "s#__CUR__#$CUR#g" ops/curate_prompt.md)"
if ! "$CLAUDE" -p "$PROMPT" --allowedTools Read Write >> "$LOG" 2>&1; then
  log "キュレーション(claude)に失敗"; exit 1
fi

# 3) 取り込み(slot は raw から自動判定)
if ! "$PY" scripts/pipeline.py ingest --raw "$RAW" --curated "$CUR" >> "$LOG" 2>&1; then
  log "ingest に失敗"; exit 1
fi
log "==== curate done slot=$SLOT ===="
