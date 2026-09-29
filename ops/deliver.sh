#!/bin/bash
# XNewsBot 配信ジョブ。配信時刻ちょうどに「最新収集 → Claudeキュレーション → LINE送信」を
# 一気通貫で実行する(=リアルタイム。古いDBを送らない)。
#
# 使い方:
#   deliver.sh               定刻配信(launchd)。slotは時刻から判定。対象スロットが有効で
#                            当日未配信の全購読者へ送信し、配信済みに記録する。
#   deliver.sh --user Uxxxx  今すぐ配信(常駐サーバが起動)。そのユーザーへ最新を送る。
#                            定刻枠は消費しない(配信済みにはしない)。
#   deliver.sh --now         今すぐ配信(管理UI)。定刻を待たず、当該スロット未配信の購読者
#                            全員へ即送信する(定刻配信と同じく配信済みに記録)。
#   deliver.sh --refresh     今すぐ更新(管理UI)。収集→キュレーション→DB取り込みだけ行い、
#                            LINE へは送信しない(Web表示の更新のみ・無料)。
#   deliver.sh --recover     取りこぼし救済(launchd の -recover から昼/夕/夜に自動実行)。
#                            当日の朝スロットが未配信のときだけ最新を集め直して送る。
#                            配信済みなら収集も Claude も呼ばずに即終了する。
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

# 実行モード: due(定刻・既定) / user(今すぐ個人) / now(今すぐ全購読者) / refresh(今すぐ更新・配信なし)
#           / recover(取りこぼし救済)
MODE="due"
USER_ID=""
case "${1:-}" in
  --user)    MODE="user"; USER_ID="${2:-}" ;;
  --now)     MODE="now" ;;
  --refresh) MODE="refresh" ;;
  --recover) MODE="recover" ;;
  "")        MODE="due" ;;
  *) echo "不明な引数: $1 (使い方: deliver.sh [--user Uxxxx | --now | --refresh | --recover])" >&2; exit 2 ;;
esac

HOUR=$(date +%H)
if [ "$HOUR" -lt 15 ]; then SLOT=morning; else SLOT=evening; fi
# リカバリは「朝の定時配信を落とした日の取り戻し」なので、実行が何時でも朝スロット固定。
# (時刻から evening と判定させると、停止中の夜スロットとして配信済み記録が付き、
#  肝心の朝スロットは未配信のまま翌日以降もリカバリが空回りする)
[ "$MODE" = recover ] && SLOT=morning
# 中間ファイル: 定刻(due)の収集中(配信5分前〜定刻)に「今すぐ」系が走ると、同一パスでは
# raw/curated を互いに上書きして定刻配信の内容が壊れる。定刻以外はモード+プロセス(PID)ごとに
# 別ファイルにして衝突させない(連打の同士討ちも防ぐ)。
case "$MODE" in
  user)    RAW="/tmp/xnews_${SLOT}_${USER_ID}_$$_raw.json"; CUR="/tmp/xnews_${SLOT}_${USER_ID}_$$_curated.json" ;;
  now)     RAW="/tmp/xnews_${SLOT}_now_$$_raw.json";        CUR="/tmp/xnews_${SLOT}_now_$$_curated.json" ;;
  refresh) RAW="/tmp/xnews_${SLOT}_refresh_$$_raw.json";    CUR="/tmp/xnews_${SLOT}_refresh_$$_curated.json" ;;
  recover) RAW="/tmp/xnews_${SLOT}_recover_$$_raw.json";    CUR="/tmp/xnews_${SLOT}_recover_$$_curated.json" ;;
  *)       RAW="/tmp/xnews_${SLOT}_raw.json";               CUR="/tmp/xnews_${SLOT}_curated.json" ;;
esac

# 定刻(配信時刻)。launchd はこの5分前に起動して先に収集を始め、結果を定刻ちょうどに送る
# (=リアルタイムを保ちつつ届く時刻は8:00/21:00で揃える)。
# ※配信時刻を変えたら、ここと plist の StartCalendarInterval(5分前) の両方を更新する。
MORNING_HHMM="0800"
EVENING_HHMM="2100"

log() { echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# 多重起動の防止。「今すぐ配信」の連打や、定刻/リカバリと重なると deliver.sh が並行実行され、
# 同じ内容が複数回 push されて LINE 無料枠(200通/月)を無駄に消費する。
# mkdir は同名ディレクトリの同時作成に必ず1つしか成功しないのでロックとして使う
# (macOS の bash 3.2 に flock は無い)。異常終了で残ったロックは mtime で回収する。
LOCK_DIR="/tmp/xnewsbot-deliver.lock"
LOCK_STALE_SEC=1800   # 最長の正常実行(15分前起動 + claude 12分 + 送信)より十分長い値
OWN_LOCK=0
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then OWN_LOCK=1; echo $$ > "$LOCK_DIR/pid" 2>/dev/null; return 0; fi
  local mtime age
  mtime=$(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0)
  age=$(( $(date +%s) - mtime ))
  if [ "$age" -ge "$LOCK_STALE_SEC" ]; then
    log "古いロック(${age}s 経過)を回収して続行"
    rm -rf "$LOCK_DIR"
    if mkdir "$LOCK_DIR" 2>/dev/null; then OWN_LOCK=1; echo $$ > "$LOCK_DIR/pid" 2>/dev/null; return 0; fi
  fi
  return 1
}

# リカバリが失敗したまま黙って終わると、当日分が落ちたことに誰も気づけない。
# 当日最後の試行(20時以降=21:00の回)で失敗したときだけ LINE に1通知らせる。
# 途中の回(12:30/17:00)で送らないのは、そのあと自動で再試行して復旧する見込みがあるため
# (無料枠200通/月を、確定した失敗1件につき1通に抑える。宛先は本人=1通・グループ宛は3通課金)。
# macOS 通知は無料なので毎回出す(Mac が起きていればその場で気づける)。
notify_recover_failure() {
  local rc=$?
  [ "$OWN_LOCK" = 1 ] && rm -rf "$LOCK_DIR"   # 自分が取ったロックだけ返す
  [ -n "${CLAUDE_CWD:-}" ] && rm -rf "$CLAUDE_CWD"   # キュレーション用の一時 cwd(mktemp -d)を片付ける
  if [ "$MODE" = recover ] && [ "$rc" -ne 0 ]; then
    osascript -e 'display notification "朝のダイジェストを再送できませんでした。~/Library/Logs/xnewsbot-deliver.log を確認してください。" with title "XNewsBot リカバリ失敗"' >/dev/null 2>&1
    if [ "$(date +%H)" -ge 20 ]; then
      "$PY" scripts/pipeline.py alert --text "XNewsBot: 本日のダイジェストを配信できませんでした。自動リトライ(12:30/17:00/21:00)も全て失敗しています。
直近のエラー: ${LAST_FAIL:-記録なし(起動自体を逃した可能性)}
ログ: ~/Library/Logs/xnewsbot-deliver.log" >> "$LOG" 2>&1
      log "LINE へ失敗を通知(当日最終試行)"
    fi
  fi
  return 0
}
trap notify_recover_failure EXIT

# 先行ジョブが走っていれば何もせず終わる(exit 0: リカバリの失敗通知を誤発火させない。
# 先行ジョブがそのまま配信を完了させるので、この起動でやるべきことは無い)。
if ! acquire_lock; then
  log "他の配信ジョブが実行中(pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '?'))。今回の起動(mode=$MODE)は何もせず終了"
  exit 0
fi

log "==== deliver start slot=$SLOT mode=$MODE target=${USER_ID:--} ===="

# リカバリ: 当日分が既に届いていれば何もしない(収集も Claude も呼ばずに終わる)。
# これがあるので launchd から1日に何度起動されても、失敗した日だけ再試行される。
if [ "$MODE" = recover ]; then
  "$PY" scripts/pipeline.py pending --slot "$SLOT" >> "$LOG" 2>&1
  PEND_RC=$?
  if [ "$PEND_RC" -eq 64 ]; then
    log "リカバリ不要(当日 $SLOT は配信済み)"; exit 0
  elif [ "$PEND_RC" -ne 0 ]; then
    # 判定自体が壊れたときは送信を試みる側に倒す(二重送信は push --due が当日済みを弾くので起きない)
    log "未配信チェックに失敗 (exit=$PEND_RC)。安全側に倒して配信を試みる"
  fi
  # 何が原因で落ちたのかをリカバリのログ行に残す(あとから原因分布を追えるようにする)
  LAST_FAIL=$(grep -E "キュレーション\(claude\)に失敗|collect に失敗|collect 全滅|ingest に失敗|キュレーション結果が空" "$LOG" | tail -1)
  log "リカバリ実行: 当日 $SLOT が未配信。直近の失敗: ${LAST_FAIL:-記録なし(起動自体を逃した可能性)}"
fi

# 当日 HHMM(定刻)まで待ってから送る。
# - 定刻まで時間がある(収集・キュレーションが定刻前に終わった)場合だけ、定刻ちょうどまで待つ。
# - 既に定刻を過ぎている(処理が定刻に間に合わなかった/スリープ復帰)場合は待たず即送信する。
#   = 「8時に終わってなくても、終わったらすぐ送る」。8時を過ぎたら諦める、はしない。
# 上限(1200s=20分)は、異常に大きな待ち(手動で変な時刻に起動した等)を保険で弾くだけ。
# 15分前起動でも定刻待ちが効くよう、15分より大きく取る。
wait_until() {
  local hhmm="$1" today target_epoch now_epoch wait
  today=$(date +%Y-%m-%d)
  target_epoch=$(date -j -f "%Y-%m-%d %H%M%S" "${today} ${hhmm}00" +%s 2>/dev/null) || return 0
  now_epoch=$(date +%s)
  wait=$((target_epoch - now_epoch))
  if [ "$wait" -gt 0 ] && [ "$wait" -le 1200 ]; then
    log "定刻 ${hhmm} まで ${wait}s 待機してから送信"
    sleep "$wait"
  elif [ "$wait" -le 0 ]; then
    log "定刻 ${hhmm} を過ぎているため待たずに即送信(超過 $((-wait))s)"
  fi
}

# 1) 収集(購読者の有効ジャンル + 常時ジャンル[特大]。対象が無ければ正常スキップ)
if [ -n "$USER_ID" ]; then
  COLLECT=("$PY" scripts/pipeline.py collect --user "$USER_ID" --slot "$SLOT" --out "$RAW")
else
  COLLECT=("$PY" scripts/pipeline.py collect --due --slot "$SLOT" --out "$RAW")
fi
# exit 64 = 対象ジャンルなし(正常スキップ)。65 = 全ジャンル収集0件(API全滅)。
# それ以外の非0は本物の失敗。空ダイジェストを「成功」配信しないよう区別する
# (以前は全失敗を「対象ジャンルなし」扱いで exit 0 にしており、障害が黙殺されていた)。
"${COLLECT[@]}" >> "$LOG" 2>&1
COLLECT_RC=$?
if [ "$COLLECT_RC" -eq 64 ]; then
  log "collect をスキップ(対象ジャンルなし)"; exit 0
elif [ "$COLLECT_RC" -eq 65 ]; then
  log "collect 全滅(全ジャンル0件・API一時障害の可能性)"
  if [ -n "$USER_ID" ]; then
    # 今すぐ配信: 収集できなかったので、空振りさせず DB にある当日分を送る(あれば最新の既存分)。
    log "今すぐ: 収集失敗 → DBの当日分にフォールバックして送信"
    "$PY" scripts/pipeline.py push --user "$USER_ID" --slot "$SLOT" >> "$LOG" 2>&1
  fi
  exit 1
elif [ "$COLLECT_RC" -ne 0 ]; then
  log "collect に失敗 (exit=$COLLECT_RC)"; exit 1
fi

# 2) キュレーション(ヘッドレス Claude Code, Read/Write のみ)
# claude -p が稀にハングすると定刻配信が無限ブロックするため、timeout が在れば被せる
# (GNU coreutils。macOS は未導入なら gtimeout。どちらも無ければ従来どおり無制限実行)。
# 実測: Opus 4.8 のキュレーションは約8分(498s/300KB raw)。旧540s上限は実測の92%で、
# ニュースが多い日に延びると打ち切られ配信失敗しうる。15分前起動(収集~2分)でも収まる720sへ。
TIMEOUT_BIN=""
if command -v timeout >/dev/null 2>&1; then TIMEOUT_BIN="timeout 720"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT_BIN="gtimeout 720"; fi
PROMPT="$(sed -e "s#__RAW__#$RAW#g" -e "s#__CUR__#$CUR#g" ops/curate_prompt.md)"
# モデルを明示する。未指定だと settings.json 既定(Fable 5・1M)を継承して 1 実行 ~12 分かかる。
# 定刻配信は 15 分前起動で余裕が薄いので、品質を保ちつつ速い Opus 4.8 を使う。
# raw のツイートは信用できない外部テキスト。仕込まれた指示で任意ファイルを触られないよう権限を絞る:
#   --tools Read,Write … Bash/WebFetch 等を無効化 / --setting-sources "" … ユーザー設定の広い allow と hooks を読まない
#   allow は RAW の Read と CUR の Edit(Write はこれで判定)だけ / dontAsk … 許可外は確認待ちにせず即拒否
#   cwd は空の専用ディレクトリ(作業ディレクトリ内の読み取りは無条件で通るため .env のある $PROJ で動かさない)。
#   固定パスだと先置き・シンボリックリンク差し替えを許すので実行ごとに mktemp -d で作り、EXIT trap で消す。
#   --safe-mode … CLAUDE.md/skills/plugins/hooks 等を読まない。--restricted はファイル系ツールを cwd 内に
#   閉じ込め、cwd 外の RAW(/tmp)が allow ルールがあっても拒否されるため使わない(2026-09-30 実測)。
if ! CLAUDE_CWD="$(mktemp -d)"; then
  log "キュレーション用の一時ディレクトリ(mktemp -d)を作れず中止"; exit 1
fi
# 権限拒否等で claude が CUR を書けなかったとき、前回の curated を当日分として ingest しないよう先に消す
rm -f "$CUR"
if ! ( cd "$CLAUDE_CWD" && $TIMEOUT_BIN "$CLAUDE" --model claude-opus-4-8 -p "$PROMPT" \
       --tools Read,Write --permission-mode dontAsk --setting-sources "" --strict-mcp-config \
       --no-session-persistence --safe-mode --allowedTools "Read(/$RAW)" "Edit(/$CUR)" ) >> "$LOG" 2>&1; then
  log "キュレーション(claude)に失敗 or タイムアウト"; exit 1
fi
# claude が exit 0 でも __CUR__ を書かない/空のことがある。空のまま ingest すると
# 既存ダイジェストは保持されるが当該実行は無意味なので、ここで止めて原因を切り分けやすくする。
if [ ! -s "$CUR" ]; then
  log "キュレーション結果が空($CUR)。配信を中止"; exit 1
fi

# 3) 取り込み(slot は raw から自動)
if ! "$PY" scripts/pipeline.py ingest --raw "$RAW" --curated "$CUR" >> "$LOG" 2>&1; then
  log "ingest に失敗"; exit 1
fi

# 4) 送信
# 収集〜送信が深夜0時を跨ぐと現在日にはダイジェストが無く空配信になるため、
# raw に記録された収集日を --date で渡す(ingest と同じ日付で読み出す)。
# (DATE_OPT は文字列展開。macOS の bash 3.2 は set -u 下で空配列の展開がエラーになる。
#  日付は YYYY-MM-DD で空白を含まないためクォートなし展開で安全)
DDATE=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['date'])" "$RAW" 2>/dev/null)
DATE_OPT=""; [ -n "$DDATE" ] && DATE_OPT="--date $DDATE"
case "$MODE" in
  user)
    # 今すぐ配信(個人): 待たずに即送信(その瞬間の最新を届ける)
    "$PY" scripts/pipeline.py push --user "$USER_ID" --slot "$SLOT" $DATE_OPT >> "$LOG" 2>&1
    ;;
  now)
    # 今すぐ配信(全購読者): 定刻を待たず、当該スロット未配信の購読者全員へ即送信(配信済みに記録)
    "$PY" scripts/pipeline.py push --due --slot "$SLOT" $DATE_OPT >> "$LOG" 2>&1
    ;;
  refresh)
    # 今すぐ更新: LINE へは送らない(ingest 済み=DB/Web表示は最新になっている)
    log "今すぐ更新: 取り込みのみ完了(LINE送信なし)"
    ;;
  recover)
    # リカバリ: 定刻はとうに過ぎているので待たずに即送信(定刻配信と同じく配信済みに記録)
    "$PY" scripts/pipeline.py push --due --slot "$SLOT" $DATE_OPT >> "$LOG" 2>&1
    ;;
  *)
    # 定刻配信: 5分前に収集を始めているので、定刻ちょうどまで待ってから送信
    if [ "$SLOT" = morning ]; then wait_until "$MORNING_HHMM"; else wait_until "$EVENING_HHMM"; fi
    "$PY" scripts/pipeline.py push --due --slot "$SLOT" $DATE_OPT >> "$LOG" 2>&1
    ;;
esac
log "==== deliver done slot=$SLOT mode=$MODE ===="
