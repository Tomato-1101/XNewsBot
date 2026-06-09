#!/usr/bin/env bash
# XNewsBot ライブ監視盤: 配信セッションが「いま動いているか/いないか」を常時表示する。
# 読み取り専用(ps/stat/tail のみ)。配信プロセスには一切影響しない。何枚開いてもOK。
# 使い方: bash ops/watch.sh [更新間隔秒=2]   終了は Ctrl-C。
# ちらつき対策: clear(全消去)は使わず、カーソルをホームへ戻して上書き描画する。
set -u
LOG="$HOME/Library/Logs/xnewsbot-deliver.log"
INTERVAL="${1:-2}"
ESC=$(printf '\033')

cleanup() { printf '%s[?25h\n' "$ESC"; exit 0; }   # 終了時にカーソルを戻す
trap cleanup INT TERM

render() {
  now=$(date +%s)
  echo "XNewsBot 配信モニタ  $(date '+%F %T')   (${INTERVAL}s 更新 / Ctrl-C で終了)"
  echo "------------------------------------------------------------"

  procs=$(ps -eo pid,etime,command | grep -E "ops/deliver\.sh|claude -p|scripts/pipeline\.py" | grep -v grep || true)
  if [ -n "$procs" ]; then
    echo "状態: ● RUNNING  配信セッション稼働中"
    if   echo "$procs" | grep -q "claude -p";           then step="キュレーション中(claude 実行中。ログ無音は正常)";
    elif echo "$procs" | grep -q "pipeline.py collect";  then step="X 収集中";
    elif echo "$procs" | grep -q "pipeline.py ingest";   then step="DB 取り込み中";
    elif echo "$procs" | grep -q "pipeline.py push";     then step="LINE 送信中";
    else                                                      step="進行中"; fi
    echo "ステップ: $step"
    echo "$procs" | sed -E 's/  +/ /g' | cut -c1-96 | sed 's/^/  /'
  else
    echo "状態: ○ IDLE   いま動いている配信セッションは無し"
  fi

  echo "------------------------------------------------------------"
  if [ -f "$LOG" ]; then
    m=$(stat -f %m "$LOG"); age=$((now - m))
    echo "ログ最終更新: $(date -r "$m" '+%T') (${age}秒前)"
    echo "直近の区切り:"
    grep -E "deliver (start|done)|失敗|スキップ|TimeoutError" "$LOG" | tail -n 3 | sed 's/^/  /'
    echo "ログ末尾:"
    tail -n 3 "$LOG" | cut -c1-96 | sed 's/^/  /'
  else
    echo "(ログ未作成: まだ一度も配信が走っていない)"
  fi
}

printf '%s[2J%s[?25l' "$ESC" "$ESC"   # 初回だけ全消去 + カーソル非表示
while true; do
  frame=$(render)
  printf '%s[H' "$ESC"                                          # カーソルをホームへ(画面は消さない)
  printf '%s\n' "$frame" | while IFS= read -r l; do
    printf '%s%s[K\n' "$l" "$ESC"                               # 行を上書きし行末の残りを消す
  done
  printf '%s[J' "$ESC"                                          # 前フレームの余り行を下方向に消す
  sleep "$INTERVAL"
done
