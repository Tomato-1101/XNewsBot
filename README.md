# XNewsBot

X(Twitter)から1日2回(朝・夜)その時点のニュースを集め、**Claude Code(サブスク)**でキュレーションして
**LINE Bot** で配信する。

- **大ニュース**は要約付きで即配信。
- **そのほか**は見出しのみ一覧 → タップで詳細を返す。
- 受け取るジャンルと朝/夜の配信時刻は、Bot との対話で設定・変更できる。
- 既定ジャンルは **AI / 株 / 経済 / 政治 / RPA / 世界**。`config/genres.toml` で編集する。

データ源は twitterapi.io(読み取り専用 / 自分のXアカウントは使わない)。
**キュレーションは Claude Code が定期実行で行う(Anthropic API キーは使わない=従量課金なし)。**
XAgent・x-research とは別の独立プロジェクト。

## アーキテクチャ（重要）

2つの動く部品に分かれる:

1. **常駐サーバ(FastAPI + APScheduler)** — APIキー不要のルーティングだけ。
   - LINE Webhook(オンボーディング・設定変更・見出しタップで詳細・今すぐ配信)。
   - 60秒ごとの tick が「配信時刻を過ぎ & 当日そのスロット未配信」の購読者へ、DB の既存ダイジェストを push(catch-up方式)。
2. **キュレーションの定期実行(Claude Code)** — 朝・夜の各配信前に走る。
   - `pipeline.py collect` でXから収集 → **Claude Code が raw を読みキュレーション** → `pipeline.py ingest` で DB 取り込み。
   - 「日×スロット(朝/夜)×ジャンル」で1ダイジェスト。複数購読者で再利用する。

```
[朝 07:40 / 夜 20:40 ごろ: Claude Code 定期実行]      [常駐サーバ]
  collect(--slot morning|evening) ──┐                  ├ LINE Webhook /line/callback
  Claude Code がキュレーション         │ 取り込み           │   follow/message/postback
  ingest(--slot ...) → GenreDigest ──┘                  └ tick(60s): 時刻到来&未配信を push
                                                            (朝=おはよう / 夜=こんばんは)
```

## 構成

```
config/genres.toml      配信ジャンルとX検索キーワード(★編集する一次ファイル)
xnewsbot/
  config.py       設定(pydantic-settings, .env)
  db.py           SQLite エンジン/セッション
  models.py       Subscriber / GenreDigest(slot付) / NewsItem、SLOTS定義
  genres.py       config/genres.toml を読み込み(keywords/exclude/min_faves)
  xclient.py      twitterapi.io 読み取り(within_time + クライアント側フィルタ + 再試行)
  curator.py      キュレーションの入出力契約(プロンプト生成 / curated JSON のパース)
  digest.py       ingest(取り込み) / assemble(組み立て)。日×スロット×ジャンル
  line_client.py  LINE メッセージ構築(純粋spec) + 送信(SDK v3)
  onboarding.py   LINE 対話の状態機械(ジャンル → 朝時刻 → 夜時刻 / 設定変更)
  scheduler.py    配信(slot別 catch-up) / 今すぐ配信
  api/main.py     FastAPI + lifespan(DB初期化 + BackgroundScheduler)
  api/line_webhook.py  /line/callback(署名検証→正規化→handle_event)
scripts/pipeline.py    collect / ingest / push のCLI(定期実行と手動テスト)
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
設定する(値はコミットしない。**Anthropic キーは不要**):
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
2. **朝**の配信時刻を選ぶ → **夜**の配信時刻を選ぶ(ボタン or 「7:30」のように入力)。
3. 毎日 朝と夜に配信。「メニュー」で ジャンル/朝の時刻/夜の時刻 を変更、「今すぐ配信」で即試せる。

## キュレーションの定期実行（1日2回）

朝・夜の各配信時刻の少し前に、Claude Code が次を実行する(手動でも同じ):
```bash
# 1) 収集(朝)
.venv/bin/python scripts/pipeline.py collect --due --slot morning --out /tmp/xnews_raw.json
# 2) Claude Code が /tmp/xnews_raw.json を読み、各ジャンルを
#    [{"title","summary","importance":"big|small","score","source_idxs":[..]}] にして
#    /tmp/xnews_curated.json に書く(大ニュースはジャンルあたり最大3件)
# 3) 取り込み(slot は raw から自動)
.venv/bin/python scripts/pipeline.py ingest --raw /tmp/xnews_raw.json --curated /tmp/xnews_curated.json
```
夜は `--slot evening` で同様に。配信(push)は常駐サーバの tick が各購読者の時刻に行う。
特定ユーザーへ手動 push してレンダリングを確認するには:
```bash
.venv/bin/python scripts/pipeline.py push --user U×××× [--slot morning|evening]
```

## ジャンルの編集
`config/genres.toml` を編集する(再起動や再収集で反映)。
- `key` … ジャンル識別子(LINEボタン・DB)
- `keywords` … X検索キーワード(OR検索)
- `exclude` … 任意。この語を含む投稿を除外(例: AIの株スパム対策)
- `min_faves` … 任意。ジャンル別の最低いいね数(RPAなどニッチは下げる。既定は `.env` の `COLLECT_MIN_FAVES`)

## テスト
```bash
.venv/bin/python -m pytest -q
```

## 既知の制約
- **ローカル運用**: Mac がスリープ/持ち出し/再起動中は応答せず、配信も遅延する。
  tick は catch-up 方式(配信時刻を過ぎ & 当日そのスロット未配信を検出)なので、復帰後の次回点検で当日分を配信する。
  定刻を厳守したいなら `pmset repeat wake` で配信前に起こすか、将来クラウドへ移行する。
- キュレーションは Claude Code セッションが前提。サーバ単体ではダイジェストを生成できないため、
  定期実行が止まると tick は「準備中」を返す(または配信を持ち越す)。
- twitterapi.io はサーバ演算子(min_faves等)が best-effort で揺らぐため、いいね下限・返信除外・直近性は
  クライアント側で確定的にフィルタし、空ページには再試行で対応している。
</content>
