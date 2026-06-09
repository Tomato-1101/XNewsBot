# 引き継ぎ (XNewsBot)

新セッションの Claude はまずこれを読む。

## 目的
X(Twitter)発のニュースを **Claude Code(サブスク)** でキュレーションし **LINE Bot** で配信する。
大ニュースは要約付きで即配信、そのほかは見出しのみ→タップで詳細。**1日2回(朝・夜)**配信。
ジャンルと朝/夜の時刻は Bot 対話で設定/変更。詳細は README.md。

## 決定事項
- 稼働: **ローカル Mac 常駐(launchd) + トンネル(ngrok 推奨)**。
- 加工: **Claude Code が定期実行でキュレーション(Anthropic API キーは使わない=従量課金なし)**。
- 配信: **朝(既定 08:00)・夜(既定 21:00)の2スロット**。各スロットで最新を収集し直す(=朝夜で別内容)。
- ジャンル: **AI / 株 / 経済 / 政治 / RPA / 世界**(`config/genres.toml` で編集)。
- 対応は **LINE のみ**、ニュース源は **X のみ**。XAgent / x-research のコードは触らない。

## 2部構成（READMEの「アーキテクチャ」も参照）
1. **常駐サーバ**: LINE Webhook + 60秒 tick(時刻到来&未配信スロットを DB から push)。APIキー不要。
2. **キュレーション定期実行(Claude Code)**: `pipeline.py collect`(朝/夜) → Claude Code がキュレーション
   → `pipeline.py ingest`。これが DB にダイジェストを入れ、tick がそれを配信する。

## 実装状況（2026-06-09）
- コア実装・ユニットテスト完了。**29 tests passing**（`.venv/bin/python -m pytest -q`）。
- **X 収集はライブ確認済み**(6ジャンル取得・within_time・viewCount降順)。
- **パイプライン(collect→キュレーション→ingest→assemble→spec生成)をライブ実走で確認済み**
  (朝スロットに AI/RPA/世界 を取り込み、朝/夜スロットが独立、詳細spec生成までOK)。
- twitterapi 鍵は macOS Keychain(`twitterapi_io_key`) にあり、x-research と共有(コードは自己完結)。
- **未実施(本人作業待ち)**:
  - LINE E2E … ngrok 起動 + Webhook URL 設定 + 友だち追加 → オンボーディング/タップ詳細の実機確認。
  - キュレーションの定期実行(cron)の本番スケジュール確定。
  - launchd 常駐化。

## ハマりどころ（先に知る）
- **twitterapi.io は非決定的**: 同じクエリでも空ページを返す。`since_time/until_time`(epoch秒)は
  0件になる。→ `xclient` は `within_time:<N>h` を使い、いいね下限/返信除外/直近性は **クライアント側**で確定的に
  フィルタ、空なら `fetch_with_retry` で再試行する。サーバ演算子(min_faves等)は信用しない。
- **ニッチジャンルは min_faves を下げる**: RPA は高いいねが付きにくく既定200だと0件 → `genres.toml` で `min_faves=10`。
- **AIジャンルは株スパムが混入**しやすい → `genres.toml` の `exclude`(必ず買い/銘柄 等)でサーバ+クライアント両方で除外。
- **SQLModel のフィールド名と型の衝突**: `date: date` は不可 → `GenreDigest.digest_date`。
- **LINE follow イベント**には `follow` オブジェクト(`{"isUnblocked":false}`)が必要。無いと SDK が UnknownEvent にする。
- **配信は別スレッド**: webhook の「今すぐ配信」と tick は別スレッド/別セッション。in-memory SQLite を跨ぐテストは
  StaticPool が必要(tests/test_webhook.py 参照)。
- **メッセージ構築は spec(素のdict)** に集約し、SDK 依存は `line_client.LineMessenger` だけ。テストは spec を検証する。

## データモデル
- `Subscriber`: line_user_id / enabled_genres / pending_genres / **morning_hour,minute / evening_hour,minute /
  morning_enabled / evening_enabled** / tz / onboarding_step("genres"→"morning"→"evening"→"done") /
  is_onboarded / **last_morning_on / last_evening_on**。スロット用アクセサ(slot_time/slot_enabled/last_on/set_*)あり。
- `GenreDigest`: digest_date × **slot("morning"|"evening")** × genre。
- `NewsItem`: genre / importance("big"|"small") / title / summary / source_urls / source_tweets / top_view_count。

## パイプライン(scripts/pipeline.py)
- `collect --due|--genres A,B --slot morning|evening --out raw.json` … Xから収集して raw を書く(slotはタグ)。
- `ingest --raw raw.json --curated curated.json [--slot ...]` … curated を DB に取り込む(slotは raw から自動)。
- `push --user U×××× [--slot ...]` … 当日ダイジェストを手動 push(slot省略時は現在時刻から推定)。
- curated JSON の形: `{"genres": {genre: [{"title","summary","importance","score","source_idxs":[..]}]}}`。
  `source_idxs` は raw の当該ジャンル配列のインデックス。大ニュースはジャンルあたり最大3件(curator.MAX_BIG_PER_GENRE)。

## 次にやること
1. キュレーションの定期実行を朝・夜の2本セットする(各配信時刻の少し前)。collect→(Claude Codeがキュレーション)→ingest。
2. ngrok 起動 → LINE の Webhook URL を `https://<domain>/line/callback` に設定 → 友だち追加。
3. オンボーディング(ジャンル→朝→夜) → 「今すぐ配信」で大/小ニュース push、小ニュースのタップ→詳細 を実機確認。
4. 問題なければ launchd 常駐化(ops/com.tomato.xnewsbot.plist)。
5. ジャンル/キーワード/exclude/min_faves は `config/genres.toml`、大ニュース上限は curator.MAX_BIG_PER_GENRE、
   収集パラメータは `.env` で運用しながら調整。
</content>
