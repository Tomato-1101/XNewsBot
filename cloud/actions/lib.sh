#!/bin/bash
# cloud/actions/deliver.sh と explain.sh が source する共通部分(GitHub Actions・ubuntu 用)。
# 流れは必ず「D1 → 一時 SQLite(pull)→ 既存の Python → 差分だけ D1(push)」。設計は cloud/DESIGN.md。

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

WORK="${XNEWS_WORK:-${RUNNER_TEMP:-/tmp}/xnewsbot-work}"
mkdir -p "$WORK" && chmod 700 "$WORK" || exit 1

export TZ="${TZ:-Asia/Tokyo}"
export DB_PATH="$WORK/xnewsbot.db"
# ops/deliver.sh と Python に、Mac の既定の代わりに使う場所を教える
export XNEWS_PROJ="$ROOT"
export XNEWS_PY="${XNEWS_PY:-$(command -v python3 || command -v python)}"
export XNEWS_CLAUDE_BIN="${XNEWS_CLAUDE_BIN:-$(command -v claude)}"
export XNEWS_LOG="$WORK/deliver.log"
export XNEWS_LOCK_DIR="$WORK/deliver.lock"
export XNEWS_REVIEWER="${XNEWS_REVIEWER:-claude}"
PY="$XNEWS_PY"

# Cloudflare のトークンは d1sync にだけ渡す(外部の記事を読む claude の子プロセスの環境に置かない)
CF_TOKEN="${CLOUDFLARE_API_TOKEN:-}"
CF_ACCOUNT="${CLOUDFLARE_ACCOUNT_ID:-}"
unset CLOUDFLARE_API_TOKEN CLOUDFLARE_ACCOUNT_ID
d1sync() {
  ( export CLOUDFLARE_API_TOKEN="$CF_TOKEN" CLOUDFLARE_ACCOUNT_ID="$CF_ACCOUNT"
    exec "$PY" scripts/d1sync.py "$@" )
}

# D1 を落とせなければ何もしない(空の DB で配信処理を走らせない)
pull_or_die() {
  if ! d1sync pull; then
    echo "D1 の取得に失敗したため中止"
    exit 1
  fi
}

# 本処理の後始末: サンプルを描き直す → 差分を D1 へ戻す → 本処理の終了コードで終わる。
# 本処理が失敗していても push する(途中まで取り込んだ記事や障害通知の記録を捨てない)。
# $1 = 本処理の終了コード
finish() {
  local rc="$1" n pushed=0
  [ -f "$XNEWS_LOG" ] && cat "$XNEWS_LOG"
  if ! "$PY" scripts/render_mock.py; then
    echo "サンプル(mock_specs)の描画に失敗"
    [ "$rc" -eq 0 ] && rc=1
  fi
  # push を落とすと「LINE には送ったのに配信済みの記録が D1 に無い」状態になり、次のリカバリで二重に届く。
  # 同じ差分を流し直しても結果は変わらない(INSERT OR REPLACE)ので、通信の一時的な失敗は間を置いてやり直す。
  for n in 1 2 3; do
    if d1sync push; then pushed=1; break; fi
    echo "D1 への反映に失敗(${n}回目)"
    [ "$n" -lt 3 ] && sleep 20
  done
  if [ "$pushed" -ne 1 ]; then
    echo "D1 へ反映できなかった。今回の結果は D1 に残っていない"
    [ "$rc" -eq 0 ] && rc=1
  fi
  exit "$rc"
}
