#!/bin/bash
# XNewsBot 配信ジョブ。配信時刻ちょうどに「最新収集 → Claudeキュレーション → LINE送信」を
# 一気通貫で実行する(=リアルタイム。古いDBを送らない)。
#
# 使い方:
#   deliver.sh               定刻配信(launchd)。slotは時刻から判定。対象スロットが有効で
#                            当日未配信の全購読者へ送信し、配信済みに記録する。
#   deliver.sh --user Uxxxx  今すぐ配信(常駐サーバが起動)。そのユーザーへ最新を送る。
#                            定刻枠は消費しない(配信済みにはしない)。
#
# 役割分担(分業): 収集(twitterapi.io)と送信(LINE)はこのスクリプト=プログラムが行い、
#   記事の選別・日本語見出し・要約の生成だけをヘッドレス Claude Code が担う。
#   安全のため Claude に許すツールは Read / Write のみ(Bash や任意操作は不可)。
set -uo pipefail

PROJ="/Users/tomato/Project/XNewsBot"
PY="$PROJ/.venv/bin/python"
CLAUDE="/Users/tomato/.local/bin/claude"
LOG="$HOME/Library/Logs/xnewsbot-deliver.log"
export PATH="/Users/tomato/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$PROJ" || exit 1

USER_ID=""
if [ "${1:-}" = "--user" ]; then USER_ID="${2:-}"; fi

HOUR=$(date +%H)
if [ "$HOUR" -lt 15 ]; then SLOT=morning; else SLOT=evening; fi
RAW="/tmp/xnews_${SLOT}_raw.json"
CUR="/tmp/xnews_${SLOT}_curated.json"

# 定刻(配信時刻)。launchd はこの5分前に起動して先に収集を始め、結果を定刻ちょうどに送る
# (=リアルタイムを保ちつつ届く時刻は8:00/21:00で揃える)。
# ※配信時刻を変えたら、ここと plist の StartCalendarInterval(5分前) の両方を更新する。
MORNING_HHMM="0800"
EVENING_HHMM="2100"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }
log "==== deliver start slot=$SLOT target=${USER_ID:-<due>} ===="

# 当日 HHMM まで待つ(最大15分。既に過ぎていれば待たずに即送信=スリープ復帰時のcatch-up)。
wait_until() {
  local hhmm="$1" today target_epoch now_epoch wait
  today=$(date +%Y-%m-%d)
  target_epoch=$(date -j -f "%Y-%m-%d %H%M%S" "${today} ${hhmm}00" +%s 2>/dev/null) || return 0
  now_epoch=$(date +%s)
  wait=$((target_epoch - now_epoch))
  if [ "$wait" -gt 0 ] && [ "$wait" -le 900 ]; then
    log "定刻 ${hhmm} まで ${wait}s 待機してから送信"
    sleep "$wait"
  fi
}

# 1) 収集(購読者の有効ジャンル + 常時ジャンル[特大]。対象が無ければ正常スキップ)
if [ -n "$USER_ID" ]; then
  COLLECT=("$PY" scripts/pipeline.py collect --user "$USER_ID" --slot "$SLOT" --out "$RAW")
else
  COLLECT=("$PY" scripts/pipeline.py collect --due --slot "$SLOT" --out "$RAW")
fi
if ! "${COLLECT[@]}" >> "$LOG" 2>&1; then
  log "collect をスキップ(対象ジャンルなし)"; exit 0
fi

# 2) キュレーション(ヘッドレス Claude Code, Read/Write のみ)
PROMPT="$(sed -e "s#__RAW__#$RAW#g" -e "s#__CUR__#$CUR#g" ops/curate_prompt.md)"
if ! "$CLAUDE" -p "$PROMPT" --allowedTools Read Write >> "$LOG" 2>&1; then
  log "キュレーション(claude)に失敗"; exit 1
fi

# 3) 取り込み(slot は raw から自動)
if ! "$PY" scripts/pipeline.py ingest --raw "$RAW" --curated "$CUR" >> "$LOG" 2>&1; then
  log "ingest に失敗"; exit 1
fi

# 4) 送信
if [ -n "$USER_ID" ]; then
  # 今すぐ配信: 待たずに即送信(その瞬間の最新を届ける)
  "$PY" scripts/pipeline.py push --user "$USER_ID" --slot "$SLOT" >> "$LOG" 2>&1
else
  # 定刻配信: 5分前に収集を始めているので、定刻ちょうどまで待ってから送信
  if [ "$SLOT" = morning ]; then wait_until "$MORNING_HHMM"; else wait_until "$EVENING_HHMM"; fi
  "$PY" scripts/pipeline.py push --due --slot "$SLOT" >> "$LOG" 2>&1
fi
log "==== deliver done slot=$SLOT ===="
