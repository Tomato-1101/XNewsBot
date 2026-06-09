# XNewsBot セットアップ作業指示（PC操作エージェント向け）

このMacでXNewsBot（X→LINEニュース配信Bot）を本稼働させるための作業手順です。
上から順に実行してください。**各ステップで検証コマンドの結果を確認**してから次へ進みます。

## 前提・厳守事項
- 作業ディレクトリ: `/Users/tomato/Project/XNewsBot`
- **秘密情報を読まない/表示しない/コミットしない**: `.env` の中身（LINEトークン等）は開かない。
  存在確認だけならファイル一覧で可。
- **git に push しない・main へ commit しない**（コード変更は不要。設定とテストだけ）。
- 破壊的操作（既存launchdの停止・削除）はしない。指示にあるもの以外を触らない。
- ターミナルは zsh。`launchctl` は `gui/$(id -u)` ドメインを使う。

## 現状（実装済み・私が確認済み）
- 常駐サーバ `com.tomato.xnewsbot`（uvicorn :8010）は稼働中。`curl -s http://127.0.0.1:8010/health` が `{"status":"ok",...}`。
- ngrok は `8010` を公開中。
- リアルタイム配信スクリプト `ops/deliver.sh` と launchd 定義 `ops/com.tomato.xnewsbot-deliver.plist` は作成済み（**未インストール**）。

---

## ステップ1: LINE Webhook URL を修正する【最優先・これが原因で今は無反応】

現在 LINE の Webhook URL にパス `/line/callback` が抜けており、リクエストがルート `/` に届いて 404 になっている。

1. まず現在の ngrok 公開URLを取得（ターミナル）:
   ```
   curl -s http://127.0.0.1:4040/api/tunnels | python3 -c "import sys,json;print([t['public_url'] for t in json.load(sys.stdin)['tunnels'] if t['public_url'].startswith('https')][0])"
   ```
   → 例: `https://YOUR-STATIC-DOMAIN.ngrok-free.dev`（ここでは仮に `<PUBLIC>` と呼ぶ）。
   ngrok が止まっていて取得できない場合は、別ターミナルで `ngrok http 8010` を起動してから再取得する。

2. ブラウザで LINE Developers Console を開く（https://developers.line.biz/console/ ／ tomato 本人がログイン済みの前提）。
   対象の **Messaging API チャネル → 「Messaging API設定」タブ**:
   - **Webhook URL** を `<PUBLIC>/line/callback` に設定（末尾の `/line/callback` を必ず付ける）。
   - **「検証」ボタン**を押し、`成功 (Success)` を確認。
   - **Webhookの利用 = ON**。
   - 同ページの「LINE公式アカウント機能」→ **応答メッセージ（自動応答）= オフ**、Webhook = オン。

3. ターミナルで疎通確認（署名なしなので 400 が正常＝エンドポイント生存）:
   ```
   curl -s -o /dev/null -w "%{http_code}\n" -X POST <PUBLIC>/line/callback -H "X-Line-Signature: dummy" -d '{}'
   ```
   → `400` ならOK（`404` ならまだURLのパスが違う）。

---

## ステップ2: オンボーディング（友だち追加〜設定）

1. tomato のスマホ/LINEアプリで、このBotを **一度ブロック→解除**（または削除→再追加）して `follow` を再送する。
   → ジャンル選択の対話が始まる。
2. 対話に従って **ジャンルを選択→「これで決定」→ 朝の時刻 → 夜の時刻** を設定（既定 朝8:00 / 夜21:00）。
   ※「特大ニュース」は選択肢に出ない（全員に常時配信される枠）。
3. 設定完了メッセージが返ればOK。

---

## ステップ3: リアルタイム定期配信（launchd）をインストールする

配信時刻ちょうどに「最新収集 → Claudeが要約生成 → LINE送信」を実行する常駐ジョブ。
朝8:00・夜21:00に発火する。

```
cd /Users/tomato/Project/XNewsBot
chmod +x ops/deliver.sh
cp ops/com.tomato.xnewsbot-deliver.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tomato.xnewsbot-deliver.plist
launchctl list | grep xnewsbot-deliver    # 登録確認(行が出ればOK)
```

※ もし旧ジョブ `com.tomato.xnewsbot-curate` が登録されていたら停止する（今は deliver に統合済み）:
```
launchctl list | grep xnewsbot-curate && launchctl bootout gui/$(id -u)/com.tomato.xnewsbot-curate || true
```

---

## ステップ4: 動作前提のチェック（収集・Claudeが動くか）

1. **ヘッドレスClaudeのログイン確認**（キュレーションに必須）:
   ```
   /Users/tomato/.local/bin/claude -p "ok とだけ返して" --allowedTools Read
   ```
   → `ok` 等が返ればログイン済み。エラー（要ログイン）なら tomato 本人に `claude` で対話ログインしてもらう。
2. **収集の鍵（twitterapi.io）確認**: 下のステップ5の手動配信ログで `collect ... 件 収集` が出れば鍵は通っている。
   もし `collect` が認証エラーで落ちる場合は、Keychain がlaunchd文脈で読めていない。
   → tomato に依頼して `.env` に `TWITTERAPI_IO_KEY=...` を1行追記してもらい、サーバを
   `launchctl kickstart -k gui/$(id -u)/com.tomato.xnewsbot` で再起動する（**値は表示しない**）。

---

## ステップ5: 実機テスト（手動で1回だけ配信を走らせる）

launchd の発火を待たず、手動で配信ジョブを起動して LINE に届くか確認する:
```
launchctl kickstart -k gui/$(id -u)/com.tomato.xnewsbot-deliver
tail -n 40 -f ~/Library/Logs/xnewsbot-deliver.log     # 進行を監視(Ctrl-Cで抜ける)
```
ログに `collect ... 件 収集` → `ingest 完了` → `push(due) 完了 ... (1 名)` → `deliver done` が並び、
**tomato の LINE にニュースが届けば成功**（先頭に🚨特大ニュース、続いて選択ジャンルの大/小ニュース）。
小ニュースの「詳細を見る」をタップ → 詳細テキストが返ることも確認する。

※ Botの「今すぐ配信」ボタンでも同じ配信が走る（その時の最新を収集して送る。1〜2分かかる）。

---

## トラブル時の見どころ
- サーバ生存: `curl -s http://127.0.0.1:8010/health`
- Webhook到達: `curl -s "http://127.0.0.1:4040/api/requests/http?limit=5"`（`/line/callback` に 200 が来ているか）
- サーバログ: `~/Library/Logs/xnewsbot.err.log` / `.out.log`
- 配信ログ: `~/Library/Logs/xnewsbot-deliver.log`
- 配信時刻を変えたい: Bot の「朝の時刻 / 夜の時刻」で変更し、加えて
  `~/Library/LaunchAgents/com.tomato.xnewsbot-deliver.plist` の `Hour`/`Minute` も同じ値に直して
  `launchctl bootout` → `launchctl bootstrap` で入れ直す。
- ジャンルやキーワードの調整: `config/genres.toml` を編集（再インストール不要、次回配信から反映）。

完了したら、各ステップの確認結果（health、検証400、launchd登録行、配信ログの要点、LINE着信の有無）を報告すること。
