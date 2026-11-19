# クラウド移行の設計（2026-11-19 決定）

目的: PC を閉じていても、朝の配信と LINE の操作（詳細・評価・設定・AI 解説・今すぐ配信）が動くようにする。月額0円。

## 構成

| 役割 | 置き場 | 備考 |
|---|---|---|
| LINE webhook（全イベント） | Cloudflare Worker `xnewsbot`（TypeScript, `cloud/worker/`） | 無料枠: 10万リクエスト/日・CPU 10ms/リクエスト |
| データ | Cloudflare D1 `xnewsbot`（id `c88fa117-97bd-4119-8bf7-7ba48b79911f`, APAC） | スキーマは `cloud/d1/schema.sql`（本番 SQLite と同じ＋`kv`） |
| 定刻の起動 | Worker の Cron Trigger → GitHub `workflow_dispatch` | GitHub の schedule は遅延・欠落があるので使わない |
| 朝の収集・キュレーション・レビュー・送信 | GitHub Actions（非公開リポ `Tomato-1101/xnewsbot-cloud`） | ワークフローは公開リポ `Tomato-1101/XNewsBot` の master を checkout して `cloud/actions/*.sh` を呼ぶだけ |
| AI 解説・今すぐ配信 | 同上（Worker が dispatch） | |
| 管理画面 | 後回し（Mac 側は停止、コードは残す） | |

## 決まりごと

- D1 が唯一の正。Actions は毎回 `d1sync.py pull` で D1 → 一時 SQLite に落とし、既存の Python（SQLModel）をそのまま動かし、`d1sync.py push` で差分だけ D1 に戻す。
- 書き手の分担（衝突を避ける）:
  - Worker だけが書く: subscriber（last_morning_on/last_evening_on 以外の全列）、newsfeedback、watchedaccount、newsexplanation の行作成（claim）
  - Actions だけが書く: genredigest、newsitem、marketsnapshot、schedulesnapshot、xusagesnapshot、deliveryalert、subscriber.last_morning_on/last_evening_on、newsexplanation の結果列（status/text/error/updated_at/push_cost）、kv
- D1 は外部キーを強制する。差分 SQL は先頭に `PRAGMA defer_foreign_keys = true;` を入れる。
- `kv` のキー: `watch_state`（watch_state.json の中身）、`seen_models`（seen_models.json）、`publish_state`（publish_state.json）、`mock_specs`（「テスト」で返す LINE メッセージ配列の JSON。Python の digest_specs で描画したもの）。
- Claude はサブスクの CI 用トークン（`claude setup-token`, Actions secret `CLAUDE_CODE_OAUTH_TOKEN`）。配信前レビューはクラウドでは Codex ではなく Claude（`XNEWS_REVIEWER=claude`）。
- 時刻: Actions は `TZ=Asia/Tokyo`。

## Worker → Actions の約束（workflow_dispatch）

リポ `Tomato-1101/xnewsbot-cloud`、ref `main`。

| ワークフロー | inputs | 中身 |
|---|---|---|
| `deliver.yml` | `mode`: `due` / `user` / `now` / `refresh` / `recover` / `render-mock`、`user`: LINE の user/group id（mode=user のとき） | `cloud/actions/deliver.sh` → `ops/deliver.sh` の各モード。`render-mock` は mock_specs を描き直して kv に入れるだけ |
| `explain.yml` | `explanation_id`: newsexplanation.id | Worker が claim 済みの行を Actions が実行し、結果を書き戻して push_to へ push |

Worker の Cron（UTC）:
- `25 22 * * *`（07:25 JST）: deliver mode=due
- `30 3 * * *`, `0 8 * * *`, `0 12 * * *`（12:30/17:00/21:00 JST）: D1 を見て当日朝が未配信の購読者がいる時だけ deliver mode=recover

## 秘密情報（値はどこにも書かない）

| 名前 | 置き場 | 元（Keychain） |
|---|---|---|
| LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN | Worker secret・Actions secret | `shared` |
| TWITTERAPI_IO_KEY | Actions secret | `shared` |
| CLAUDE_CODE_OAUTH_TOKEN | Actions secret | `XNEWSBOT_CLAUDE_OAUTH_TOKEN` / `XNewsBot` |
| GITHUB_DISPATCH_TOKEN | Worker secret | `XNEWSBOT_GITHUB_DISPATCH_TOKEN` / `XNewsBot` |
| CLOUDFLARE_API_TOKEN（D1 Edit）/ CLOUDFLARE_ACCOUNT_ID | Actions secret | `XNEWSBOT_CLOUDFLARE_D1_TOKEN` / `XNewsBot` |
| FEED_DEPLOY_KEY | Actions secret | 新しく作る xnews-feed 用デプロイ鍵 |
