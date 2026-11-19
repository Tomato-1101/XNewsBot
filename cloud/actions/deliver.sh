#!/bin/bash
# GitHub Actions から配信を1回動かす(cloud/DESIGN.md の deliver.yml)。
#   cloud/actions/deliver.sh <mode> [user]
#   mode: due / user / now / refresh / recover … ops/deliver.sh の同名モード
#         render-mock … 配信はせず、「テスト」で返すサンプル(kv の mock_specs)を描き直すだけ
set -uo pipefail
MODE="${1:-due}"
USER_ID="${2:-}"

case "$MODE" in
  due)     ARGS=() ;;
  user)    [ -n "$USER_ID" ] || { echo "mode=user には user が必要" >&2; exit 2; }
           ARGS=(--user "$USER_ID") ;;
  now|refresh|recover) ARGS=("--$MODE") ;;
  render-mock) ARGS=() ;;
  *) echo "不明な mode: $MODE" >&2; exit 2 ;;
esac

. "$(dirname "$0")/lib.sh"

# 公開フィード: Mac と違い作業場所が毎回空なので、xnews-feed を新しく clone してそこへ書き出す
# (書き出し・commit・push は従来どおり scripts/publish_feed.py)。鍵が無ければ公開だけ省く。
setup_feed() {
  if [ -z "${FEED_DEPLOY_KEY:-}" ]; then
    echo "FEED_DEPLOY_KEY が無いので公開フィードの更新は省略"
    export XNEWS_PUBLISH=0
    return 0
  fi
  local key="$WORK/feed_deploy_key"
  ( umask 077; printf '%s\n' "$FEED_DEPLOY_KEY" > "$key" )
  unset FEED_DEPLOY_KEY
  export XNEWS_FEED_SSH_KEY="$key"
  # publish_state.json は公開リポに入れないよう1つ上に置かれる(d1sync が kv と行き来させる)
  export XNEWS_FEED_DIR="$WORK/feed/public-feed"
  mkdir -p "$HOME/.ssh" "$WORK/feed" && chmod 700 "$HOME/.ssh"
  # 接続先のなりすましを防ぐため、GitHub が公開しているホスト鍵を固定で入れる(実行時に取りに行かない)
  echo "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" >> "$HOME/.ssh/known_hosts"
  rm -rf "$XNEWS_FEED_DIR"
  if ! GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20" \
       git clone --quiet --depth 1 --branch main git@github.com:Tomato-1101/xnews-feed.git "$XNEWS_FEED_DIR"; then
    echo "xnews-feed を clone できないので公開フィードの更新は省略"
    export XNEWS_PUBLISH=0
  fi
}
[ "$MODE" = render-mock ] || setup_feed

pull_or_die

RC=0
if [ "$MODE" != render-mock ]; then
  bash ops/deliver.sh ${ARGS[@]+"${ARGS[@]}"}
  RC=$?
  echo "ops/deliver.sh mode=$MODE 終了コード=$RC"
fi
finish "$RC"
