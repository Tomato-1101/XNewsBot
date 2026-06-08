# 引き継ぎ (XNewsBot)

新セッションの Claude はまずこれを読む。

## 目的
X(Twitter)発のニュースを Claude でキュレーションし **LINE Bot** で毎日配信する。
大ニュースは要約付きで即配信、そのほかは見出しのみ→タップで詳細。ジャンル(AI/株/経済/政治)と
配信時刻は Bot 対話で設定/変更。詳細は README.md。

## 決定事項
- 稼働: **ローカル Mac 常駐(launchd) + トンネル(ngrok 推奨)**。
- 加工: **Claude でキュレーション**(claude-sonnet-4-6)。
- 対応は **LINE のみ**、ニュース源は **X のみ**。
- XAgent(自動投稿) / x-research(調査) とは別物。それらのコードは触らない。

## 実装状況（2026-06-08）
- コア実装・ユニットテストは完了。**25 tests passing**（`.venv/bin/python -m pytest -q`）。
- **X 収集はライブ確認済み**（4ジャンルとも取得・直近24h・viewCount降順）。
- twitterapi 鍵は macOS Keychain(`twitterapi_io_key`) にあり、x-research と共有(コードは自己完結)。
- **未実施(鍵/チャネル待ち)**:
  - Claude キュレーションのライブ実行 … `ANTHROPIC_API_KEY` を `.env` に設定後 `scripts/run_once.py --dry-run` で確認。
  - LINE E2E … LINE チャネル作成 + トークン + ngrok + 友だち追加が必要(本人作業)。

## ハマりどころ（先に知る）
- **twitterapi.io は非決定的**: 同じクエリでも空ページを返す。`since_time/until_time`(epoch秒)は
  0件になる。→ `xclient` は `within_time:<N>h` を使い、いいね下限/返信除外/直近性は **クライアント側**で確定的に
  フィルタ、空なら `fetch_with_retry` で再試行する。サーバ演算子(min_faves等)は信用しない。
- **SQLModel のフィールド名と型の衝突**: `date: date` は不可 → `GenreDigest.digest_date` にしている。
- **LINE follow イベント**には `follow` オブジェクト(`{"isUnblocked":false}`)が必要。無いと SDK が UnknownEvent にする。
- **配信は別スレッド**: webhook の「今すぐ配信」と launchd の tick は別スレッド/別セッションで重い処理を回す。
  in-memory SQLite を跨ぐテストは StaticPool が必要(tests/test_webhook.py 参照)。
- **メッセージ構築は spec(素のdict)** に集約し、SDK 依存は `line_client.LineMessenger` だけ。テストは spec を検証する。

## データモデル
- `Subscriber`: line_user_id / enabled_genres / pending_genres / deliver_hour,minute / tz /
  onboarding_step("genres"→"time"→"done") / is_onboarded / last_delivered_on。
- `GenreDigest`: digest_date × genre(日×ジャンルでキュレーションを1回だけ→複数購読者で再利用)。
- `NewsItem`: genre / importance("big"|"small") / title / summary / source_urls / source_tweets / top_view_count。

## 次にやること
1. `.env` に `ANTHROPIC_API_KEY` を設定 → `scripts/run_once.py --genres AI --dry-run` でキュレーション実走確認。
2. LINE チャネル作成・トークン取得(README手順) → `.env` に設定。
3. ngrok 起動 → LINE の Webhook URL を `https://<domain>/line/callback` に設定 → 友だち追加。
4. オンボーディング → 「今すぐ配信」で大/小ニュース push、小ニュースのタップ→詳細 を実機確認。
5. 問題なければ launchd 常駐化(ops/com.tomato.xnewsbot.plist)。
6. ジャンルのキーワード(genres.py)・大ニュース上限(curator.MAX_BIG_PER_GENRE)・収集パラメータ(.env)は運用しながら調整。
