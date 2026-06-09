# 引き継ぎ (XNewsBot)

新セッションの Claude はまずこれを読む。

## 目的
X(Twitter)発のニュースを **Claude Code(サブスク)** でキュレーションし **LINE Bot** で配信する。
大ニュースは要約付きで即配信、そのほかは見出しのみ→タップで詳細。**1日2回(朝・夜)**配信。
ジャンルと朝/夜の時刻は Bot 対話で設定/変更。詳細は README.md。

## 決定事項
- 稼働: **ローカル Mac 常駐(launchd 3点) + トンネル(ngrok 固定ドメイン)**。
  - `com.tomato.xnewsbot`(uvicorn:8010 webhook専任) / `com.tomato.xnewsbot-ngrok`(固定ドメイン→:8010, `--log=stdout`必須) /
    `com.tomato.xnewsbot-deliver`(配信時刻の15分前=07:45/20:45 起動 → 定刻08:00/21:00 着)。全て RunAtLoad+KeepAlive=ログイン時自動起動・自動再起動。
- 加工: **Claude Code が定期実行でキュレーション(Anthropic API キーは使わない=従量課金なし)**。
- 配信: **朝(既定 08:00)・夜(既定 21:00 着)の2スロット**。各スロットで最新を収集し直す(=朝夜で別内容)。
  収集+キュレーション(ヘッドレスClaude)に最大10分ほどかかるため**15分前(07:45/20:45)に起動して先に収集・キュレーションし、deliver.sh が定刻まで待ってから送信**(=最新かつ届く時刻を揃える)。定刻に間に合わなくても終わり次第すぐ送る(諦めて打ち切らない)。
- ジャンル: **特大(常時) / AI / 株 / 経済 / 政治 / RPA / 世界情勢 / テクノロジー / ビジネス / 健康 / 暗号資産**(`config/genres.toml` で編集)。
  各ジャンルは海外・国際キーワードも併記し日本だけでなく世界の話題も拾う(lang:jaは維持)。
- 表示(見やすさ優先): **大ニュースはジャンル順(特大→各ジャンル)に全件表示**(1ジャンルが多くても他を押し出さない/各ジャンル最低1件/無いジャンルは出さない)、**小ニュースは見出し行を全件表示**(タップで詳細。旧「ほかN件」隠れバグ解消)。1バブル~7KB超で次メッセージへ自動分割(`digest_specs`/`_pack_bubbles`、件数は削らない)。選択肢に**「キャンセル」**追加。
  通数: LINE無料枠200通/月は **push のみ計上**(reply対話=オンボ/メニュー/テストは無料無制限)。配信は内容が多い時のみ複数通。テスト用途は通数を食う「今すぐ」でなく無料の「テスト」(モック)を使う。
  「今すぐ配信」はボタンに加え**「今すぐ」「最新」等のテキスト送信でも起動**(その瞬間の最新を収集→送信)。
  **明示コマンド(今すぐ/テスト/メニュー/ヘルプ)は1:1でもグループでも応答**する(グループの雑談には無反応=荒らさない。`onboarding._try_command`)。
- レイアウト確認: **「テスト」「サンプル」「モック」等を送ると、収集せず DB の架空サンプル(`xnewsbot/mockdata.py`、センチネル日付2000-01-01)を現行レイアウトで即返す**(冒頭に架空警告)。API も時間も使わずレイアウト調整を回せる。手動投入は `scripts/seed_mock.py`(未投入なら自動投入)。
- 対応は **LINE のみ**、ニュース源は **X のみ**。XAgent / x-research のコードは触らない。

## 2部構成（READMEの「アーキテクチャ」も参照）
1. **常駐サーバ**(`com.tomato.xnewsbot`, FastAPI:8010): **LINE Webhook 受信専任**(オンボーディング/設定変更/
   詳細タップ/今すぐ配信)。定刻配信の tick は既定で無効(`scheduler_enabled=False`)。
2. **リアルタイム配信ジョブ**(`ops/deliver.sh` を launchd `com.tomato.xnewsbot-deliver` が 配信時刻の15分前=07:45/20:45 に起動):
   先に `pipeline.py collect`(--due) → **Claude Code がキュレーション** → `pipeline.py ingest` を済ませ、
   `deliver.sh` の `wait_until` で**定刻(08:00/21:00)まで待ってから** `pipeline.py push --due`(=その時刻までの最新を、届く時刻を揃えて配信/古いDBを送らない)。
   配信時刻を変えたら **plist の StartCalendarInterval(15分前) と deliver.sh の MORNING_HHMM/EVENING_HHMM(定刻) の両方**を更新する。
   「今すぐ配信」は常駐サーバが `deliver.sh --user` を別プロセス起動して同様にリアルタイム配信する。
   **役割分担(分業)**: 収集と送信はプログラム、記事選別・見出し・要約の生成だけヘッドレス Claude(Read/Writeのみ)。

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
  is_onboarded / **last_morning_on / last_evening_on** / **push_to**。スロット用アクセサ(slot_time/slot_enabled/last_on/set_*)あり。
  - **push_to**: 配信先の上書き(LINEグループ/ルームID)。`push_target = push_to or line_user_id`。配信は push_target へ。
    設定=対象グループにbotを入れ、グループ内で「このグループに配信」と送る(解除=「個別に配信」)。
    ※ LINE公式アカウント設定で「グループ・複数人トークへの参加を許可」をONにしておくこと。
  - 既存DBへの列追加は `db._migrate` が冪等ALTERで対応(SQLiteはcreate_allで列を足さない)。
- `GenreDigest`: digest_date × **slot("morning"|"evening")** × genre。
- `NewsItem`: genre / importance("big"|"small") / title / summary / source_urls / source_tweets / top_view_count。

## パイプライン(scripts/pipeline.py)
- `collect --due|--user U××××|--genres A,B --slot morning|evening --out raw.json` … Xから収集して raw を書く。
  `--due`/`--user` は **常時ジャンル(特大)を必ず含める**。`--genres` は指定のみ。
- `ingest --raw raw.json --curated curated.json [--slot ...]` … curated を DB に取り込む(slotは raw から自動)。
- `push --due [--slot ...]` … 当該スロットが有効で当日未配信の **全購読者** へ送信し、配信済みに記録(定刻配信)。
- `push --user U×××× [--slot ...]` … 指定ユーザーへ送信(今すぐ配信。配信済みにしない)。
- curated JSON の形: `{"genres": {genre: [{"title","summary","importance","score","source_idxs":[..]}]}}`。
  `source_idxs` は raw の当該ジャンル配列のインデックス。大ニュースはジャンルあたり最大3件。
  **特大ジャンルは必ず1件**(importance=big)を出すよう curate_prompt.md で指示。

## 次にやること（本人/PC操作エージェント。詳細手順は `ops/AGENT_TASKS.md`）
1. **LINE Webhook URL に `/line/callback` を付ける**(現状これが抜けていて 404＝無反応)。`<ngrok公開URL>/line/callback`。
2. 友だち追加 → オンボーディング(ジャンル→朝→夜)。※「特大」は選択肢に出ない常時枠。
3. `com.tomato.xnewsbot-deliver` を launchd インストール(07:45/20:45 起動→定刻着)。`launchctl kickstart -k` で手動配信テスト(定刻を過ぎた時間に実行すれば待ちは入らず即送信)。
4. 小ニュースのタップ→詳細、Botの「今すぐ配信」(その時の最新を収集して送る)を実機確認。
5. ジャンル/キーワード/exclude/min_faves/selectable は `config/genres.toml`(再インストール不要)、
   収集パラメータ(collect_hours 等)は `.env` で運用しながら調整。配信時刻を変えたら deliver plist の Hour/Minute も更新。

## 既知の注意（launchd 実行時）
- ヘッドレス Claude のログインが必要(`claude -p "ok" --allowedTools Read` で確認)。
- collect が twitterapi の認証で落ちる場合は Keychain が launchd 文脈で読めていない →
  `.env` に `TWITTERAPI_IO_KEY=...` を追記し、常駐サーバを `launchctl kickstart -k` で再起動。
- Mac がスリープで配信時刻を逃しても、launchd は起床時に1回だけ遅れて実行(=起床時点の最新を届ける)。
</content>
