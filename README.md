# XNewsBot

X(Twitter)から毎日その日のニュースを集め、Claude でキュレーションして **LINE Bot** で配信する。

- **大ニュース**は要約付きで即配信。
- **そのほか**は見出しのみ一覧 → タップで詳細を返す。
- 受け取るジャンル(AI / 株 / 経済 / 政治)と配信時刻は、Bot との対話で設定・変更できる。

データ源は twitterapi.io(読み取り専用 / 自分のXアカウントは使わない)。
キュレーションは Claude(anthropic)。XAgent・x-research とは別の独立プロジェクト。

## 構成

```
xnewsbot/
  config.py       設定(pydantic-settings, .env)
  db.py           SQLite エンジン/セッション
  models.py       Subscriber / GenreDigest / NewsItem
  genres.py       4ジャンルの検索キーワード
  xclient.py      twitterapi.io 読み取り(within_time + クライアント側フィルタ + 再試行)
  curator.py      Claude キュレーション(束ね/重複排除/重要度判定/見出し・要約)
  digest.py       収集→キュレーション→DB(日×ジャンルで再利用)
  line_client.py  LINE メッセージ構築(純粋spec) + 送信(SDK v3)
  onboarding.py   LINE 対話の状態機械(ジャンル/時刻 設定・コマンド)
  scheduler.py    配信(catch-up方式) / 今すぐ配信
  api/main.py     FastAPI + lifespan(DB初期化 + BackgroundScheduler)
  api/line_webhook.py  /line/callback(署名検証→正規化→handle_event)
scripts/run_once.py    手動実行(収集+キュレーション表示 / 指定ユーザーへ配信)
ops/com.tomato.xnewsbot.plist  launchd 常駐設定
```

## セットアップ

### 1) 依存インストール
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 2) LINE Messaging API チャネル作成（本人のログインが必要）
1. https://developers.line.biz/console/ にログイン → プロバイダー作成。
2. **Messaging API** チャネルを新規作成。
3. 「チャネル基本設定」→ **Channel secret** を控える。
4. 「Messaging API設定」→ **Channel access token (long-lived)** を発行して控える。
5. 「Messaging API設定」で **応答メッセージ(自動応答)を OFF**、**Webhook を ON**。
6. Webhook URL は下記トンネルの URL + `/line/callback` を設定。
7. 表示の QR / ID から、自分のLINEで Bot を**友だち追加**。

### 3) `.env` を作成
```bash
cp env.example .env
```
最低限、以下を設定する(値はコミットしない):
- `ANTHROPIC_API_KEY` … Claude のキー
- `LINE_CHANNEL_ACCESS_TOKEN` / `LINE_CHANNEL_SECRET` … 上で取得
- `TWITTERAPI_IO_KEY` … 空なら macOS Keychain(`twitterapi_io_key`) を自動使用(x-research と共有)

### 4) トンネル（公開HTTPS）
LINE の Webhook は公開 HTTPS が必要。**ngrok の無料固定ドメイン**を推奨(URL が固定で安定)。
```bash
brew install ngrok
ngrok config add-authtoken <自分のtoken>      # 本人のアカウント
ngrok http --domain=<予約した固定ドメイン> 8010
```
発行された `https://<domain>/line/callback` を LINE の Webhook URL に設定する。
（独自ドメインを Cloudflare に持っているなら cloudflared named tunnel でも可。）

### 5) 起動
```bash
# 開発(フォアグラウンド)
.venv/bin/uvicorn xnewsbot.api.main:app --host 127.0.0.1 --port 8010

# 常駐(launchd)
cp ops/com.tomato.xnewsbot.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.tomato.xnewsbot.plist
curl -s localhost:8010/health
```

## 使い方（LINE）
1. Bot を友だち追加 → ジャンルを選ぶ(複数可) → 「これで決定」。
2. 配信時刻を選ぶ(ボタン or 「7:30」のように入力)。
3. 毎日その時刻に配信。「メニュー」と送れば設定変更・「今すぐ配信」で即試せる。

## 手動実行 / 動作確認
```bash
# 収集+キュレーション結果を表示(push せず・要 ANTHROPIC_API_KEY)
.venv/bin/python scripts/run_once.py --genres AI,株 --dry-run

# 当日ダイジェストを指定ユーザーへ push(要 LINE トークン)
.venv/bin/python scripts/run_once.py --genres AI,株,経済,政治 --user U xxxxxxxx

# テスト
.venv/bin/python -m pytest -q
```

## 既知の制約
- **ローカル運用**: Mac がスリープ/持ち出し/再起動中は応答せず、配信も遅延する。
  スケジューラは catch-up 方式(配信時刻を過ぎ & 当日未配信を検出)なので、復帰後の次回点検で当日分を配信する。
  定刻を厳守したいなら `pmset repeat wake` で配信前に起こすか、将来クラウドへ移行する。
- twitterapi.io の `min_faves` 等のサーバ演算子は best-effort で揺らぐため、いいね下限・返信除外・直近性は
  クライアント側で確定的にフィルタし、空ページには再試行で対応している。
