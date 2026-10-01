# 引き継ぎ (XNewsBot)

新セッションの Claude はまずこれを読む。

## 配信の全面見直し（10-01 19:10 更新）
- 目的: 本人依頼「ジャンル・取得方法・まとめ方・形式を全部見直し、見やすく・質を高く」。中身は好評なので量は削らない。計画は `~/.claude/plans/lucky-riding-umbrella.md`。
- 現状: 実装・試走・Codex レビュー済み。取得(X 3クエリ+直取りRSS+Google ニュース日英+記事本文+市況)/ まとめ方(curate_prompt 書き直し・直近3日の見出しで再掲除外)/ 表示(要点バブル+ジャンル別カルーセル)/ RPA 廃止。
- 試走(10/01 データ): キュレーション 478s・48件(旧34件。テクノロジー 4→20)・再掲0。plist は 07:15 起動に変更済み(登録済み)。本人 1:1 へプレビュー1回 push 済み。
- 未解決: TOPIX は Yahoo で取れず未掲載。`collect_newsfeeds_per_genre` は未使用。(キー#0 は 10-02 に削除、README は 10-02 に更新済み)
- 次にやること: 10/02 07:15 の初回本番を `~/Library/Logs/xnewsbot-deliver.log` で確認(キュレーション時間・件数・push 成功・通数3)。

## 追加改善: 要約常時表示・今日の予定・仮想通貨・話題・残量表示（10-02 03:00 更新・コミット済み）
- 目的: 本人追加依頼。小ニュースも要約を常時表示／AI はマイナーも／今日の経済予定／指標は「予想比＋資産別の矢印(一般に)」／新ジャンル 仮想通貨(key 暗号資産)・話題／毎回の配信に X クレジットと LINE 残り通数。計画 `~/.claude/plans/lucky-riding-umbrella.md`。
- 済: 上記すべて実装・コミット。raw 300KB 以上は2組(特大/AI/株・暗号資産/テクノロジー/話題)に分けて claude 並列→merge(試走5: 940s・153件・圧縮なし・全件が4メッセージに収まる)。Codex レビュー2回分の指摘は全て修正(LINE 残量 API タイムアウト／merge 型検証＋ingest 1トランザクション／上限解除後は成功パートを残し失敗パートだけ再実行／予定はマージ保存)。pytest 308。
- 本番 DB: 購読者1 の enabled_genres に 暗号資産・話題 を追加済み(バックアップ `backups/xnewsbot_20261002_before_genres.db`)。本人 1:1 へプレビュー1通 push 済み。
- 鍵: 旧 Keychain `twitterapi_io_key`(残高切れ …d015)削除、中央 `TWITTERAPI_IO_KEY`/shared を …3e15 に差し替え(XAgent も同じ鍵を読む)。残 約305万クレジット≒1日1万で約300日。
- 仮想通貨の公式アカウント: TheBlock__→TheBlockCo に修正、CoinDeskJapan・neweconomy_jp は実在せず削除。
- xclient.py の tau_log 差分と AGENTS.md は他セッションの作業なので未コミットのまま残している。
- 次: 10/02 07:15 の本番を `~/Library/Logs/xnewsbot-deliver.log` で確認(分割並列・所要時間・push 成功・残量行・通数3)。

## 目的
X(Twitter)発のニュースを **Claude Code(サブスク)** でキュレーションし **LINE Bot** で配信する。
大ニュースは要約付きで即配信、そのほかは見出しのみ→タップで詳細。**1日1回(朝08:00)**配信
(2026-06-24 にコスト節約で夜21:00 を停止。夜のコード/UIは残置し再開可)。
ジャンルと朝/夜の時刻は Bot 対話で設定/変更。詳細は README.md。

## 決定事項
- 稼働: **ローカル Mac 常駐(launchd 3点) + トンネル(ngrok 固定ドメイン)**。
  - `com.tomato.xnewsbot`(uvicorn:8010 webhook専任) / `com.tomato.xnewsbot-ngrok`(固定ドメイン→:8010, `--log=stdout`必須) /
    `com.tomato.xnewsbot-deliver`(配信時刻の45分前=07:15 起動 → 定刻08:00 着。2026-10-01 に15分前から変更)。全て RunAtLoad+KeepAlive=ログイン時自動起動・自動再起動。
- 加工: **Claude Code が定期実行でキュレーション(Anthropic API キーは使わない=従量課金なし)**。
- 配信: **1日1回・朝(既定 08:00)の1スロット**。直近24hの最新を収集してキュレーション。
  収集+キュレーション(ヘッドレスClaude)に約17分(2026-10-01 の新構成の試走)かかるため**45分前(07:15)に起動して先に収集・キュレーションし、deliver.sh が定刻まで待ってから送信**(=最新かつ届く時刻を揃える)。定刻に間に合わなくても終わり次第すぐ送る(諦めて打ち切らない)。
  - 2026-06-24: 唯一の従量課金API(twitterapi.io)・Claude実行・LINE push を約半減させるため夜21:00 スロットを停止し1日1回化。夜は `-deliver` plist の StartCalendarInterval に 20:45 dict を戻し bootout→bootstrap で再開できる(evening_enabled/onboardingの夜時刻設定はコードに残置)。
  - 2026-07-02: 質優先(コスト度外視)へ方針転換。**定時ダイジェストの候補を X + 無料ニュース(Google ニュースRSS)のマージに拡張**(`xnewsbot/newsfeeds.py`, `collect_use_newsfeeds`, 1ジャンル+`collect_newsfeeds_per_genre`件)。lang="any"ジャンルは英語ロケールも収集。ニュース候補は `source:"news"`/エンゲージ0でキュレーションが信頼扱い(`ops/curate_prompt.md`)。X が空でもニュースで配信継続。※候補増でキュレーション時間が延びうる→初回の実配信で720s以内に収まるか要観察(超えるなら `collect_newsfeeds_per_genre` を下げる)。
- 速報リアルタイム配信(2026-07-02 新設): **`scripts/monitor_breaking.py` を `com.tomato.xnewsbot-breaking`(15分毎)が実行**。定時ダイジェストとは独立した「速報だけ」の常時チャンネル。
  - 無料のみ(Google ニュースRSS 主力 + GDELT best-effort。GDELTは5秒/回制限で不安定なので失敗は空で握る)。twitterapi.io は使わない。
  - 検出(積極度=`breaking_level`, 既定 medium): 監視は選択ジャンル(特大の汎用語は除外)、直近`breaking_lookback_min`分に公開・見出しに速報/注目マーカー・トピック整合、を満たすものだけ。
  - 配信先=「今のグループ」= DB `subscriber.push_to` のグループ(C…。既にbot参加済み)。`breaking_group_id` で上書き可。
  - 乱造防止3重: 重複排除(SQLite `breaking_sent`)＋日次上限(`breaking_max_per_day`, 既定5=LINE無料枠200通/月を守る)＋鮮度窓。
  - **実グループへ送るため初回稼働はユーザーの明示 GO を得てから bootstrap する**。送信しない確認は `--dry-run`。
  - 2026-07-02: 稼働初日に5件送信して「頻度が高すぎる」と指摘(マーカー語だけでは個別銘柄レーティング・委員会日程等を弾けなかった)→ **LLM判定層を追加**。
    ヒューリスティック通過分をヘッドレス Claude(`claude --model claude-opus-4-8` + `ops/breaking_judge_prompt.md`、実誤送信例で較正)が最終判定し、
    重大(major)のみ送信。失敗は fail-closed(送らない・次サイクルで再挑戦)。見送りは `breaking_rejected` テーブルに記録し再判定・再送しない。
    トグル `breaking_judge_enabled`(既定ON)。テストは `tests/test_breaking_judge.py`(claudeはモック)。
- ジャンル: **特大(常時) / AI / 株 / テクノロジー**(`config/genres.toml` で編集)。2026-10-01 に RPA を廃止(過去記事は履歴として残す)。
  各ジャンルは海外・国際キーワードも併記し日本だけでなく世界の話題も拾う(lang:jaは維持)。
  - 2026-08-10: 読者の関心変更で **政治 / 経済 / 世界情勢 を廃止**(政治・戦争・国際情勢は不要、AI・テック・株・RPA 中心へ)。
    金融政策・為替は「株」が吸収。AI は最重要ジャンルとして small も厚めに出す(`ops/curate_prompt.md` の読者関心を参照)。
- 表示(2026-10-01 刷新): **1通目=要点バブル**(件数・今日の要点 最大5本・前日の市況)、**2通目=ジャンル別カルーセル**(特大→AI→株→テクノロジー。big は要約+出典名・時刻+［詳細］［元記事］、small は見出し行でタップで詳細)。
  並びは big 先・score 降順(取り込み時に確定)。bubble 28000B・carousel 48000B を超えると分割/差し替え、不正 URI はエンコードか除外(1件で push 全体が 400 になるため)。以下は旧表示の記録。
- 旧表示: **大ニュースはジャンル順(特大→各ジャンル)に全件表示**(1ジャンルが多くても他を押し出さない/各ジャンル最低1件/無いジャンルは出さない)、**小ニュースは見出し行を全件表示**(タップで詳細。旧「ほかN件」隠れバグ解消)。1バブル~7KB超で次メッセージへ自動分割(`digest_specs`/`_pack_bubbles`、件数は削らない)。選択肢に**「キャンセル」**追加。
  通数: LINE無料枠200通/月は **push のみ計上**(reply対話=オンボ/メニュー/テストは無料無制限)。配信は内容が多い時のみ複数通。テスト用途は通数を食う「今すぐ」でなく無料の「テスト」(モック)を使う。
  「今すぐ配信」はボタンに加え**「今すぐ」「最新」等のテキスト送信でも起動**(その瞬間の最新を収集→送信)。
  **明示コマンド(今すぐ/テスト/メニュー/ヘルプ)は1:1でもグループでも応答**する(グループの雑談には無反応=荒らさない。`onboarding._try_command`)。
- レイアウト確認: **「テスト」「サンプル」「モック」等を送ると、収集せず DB の架空サンプル(`xnewsbot/mockdata.py`、センチネル日付2000-01-01)を現行レイアウトで即返す**(冒頭に架空警告)。API も時間も使わずレイアウト調整を回せる。手動投入は `scripts/seed_mock.py`(未投入なら自動投入)。
- 対応は **LINE のみ**、ニュース源は **X のみ**。XAgent / x-research のコードは触らない。

## 2部構成（READMEの「アーキテクチャ」も参照）
1. **常駐サーバ**(`com.tomato.xnewsbot`, FastAPI:8010): **LINE Webhook 受信専任**(オンボーディング/設定変更/
   詳細タップ/今すぐ配信)。定刻配信の tick は既定で無効(`scheduler_enabled=False`)。
2. **リアルタイム配信ジョブ**(`ops/deliver.sh` を launchd `com.tomato.xnewsbot-deliver` が 配信時刻の45分前=07:15 に起動):
   先に `pipeline.py collect`(--due) → **Claude Code がキュレーション** → `pipeline.py ingest` を済ませ、
   `deliver.sh` の `wait_until` で**定刻(08:00)まで待ってから** `pipeline.py push --due`(=その時刻までの最新を、届く時刻を揃えて配信/古いDBを送らない)。
   配信時刻を変えたら **plist の StartCalendarInterval(15分前) と deliver.sh の MORNING_HHMM(定刻) の両方**を更新する。
   「今すぐ配信」は常駐サーバが `deliver.sh --user` を別プロセス起動して同様にリアルタイム配信する。
   **役割分担(分業)**: 収集と送信はプログラム、記事選別・見出し・要約の生成だけヘッドレス Claude(Read/Writeのみ)。
3. **取りこぼし救済**(`ops/deliver.sh --recover` を launchd `com.tomato.xnewsbot-recover` が 12:30/17:00/21:00 に起動,
   2026-08-15 追加): 当日の朝スロットが未配信のときだけ最新を集め直して即送信する(定刻待ちなし・朝スロット固定)。
   最初に `pipeline.py pending --slot morning`(exit 0=未配信 / 64=配信済み)を見るので、成功した日は
   収集も Claude も呼ばずに0.2秒で終わる。失敗時は macOS 通知が出て、当日最終回(21:00)でも失敗した場合だけ
   **LINE に原因つきで1通アラート**が届く(`pipeline.py alert`)。途中回で送らないのは、その後の再試行で
   復旧する見込みがあるため。宛先は本人(`line_user_id`)なので1通。グループ(`push_to`)宛は人数分課金される。
   **追加の経緯**: 2026-08-12/13/15 はヘッドレス Claude が `You've hit your session limit` で落ち、
   8/14 は Mac が停止して起動自体しなかった。結果 8/11 を最後に4日間ダイジェストが届かず、誰も気づかなかった。
   セッション上限は昼過ぎ(実測12:10)にリセットされるため、時間を置いた再試行だけで大半は自動復旧する。

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
- 2026-09-30: ヘッドレス Claude はツイート/見出し(外部テキスト)を読むので権限を絞っている(deliver.sh / monitor_breaking.py)。
  `--tools Read,Write --permission-mode dontAsk --setting-sources "" --strict-mcp-config` + 入力の `Read(//abs)`・出力の `Edit(//abs)` だけ許可、`--safe-mode` 付き。緩めない。
  - cwd は実行ごとの空の `mktemp -d`/`tempfile.mkdtemp()`(速報の cands/verdict もその中)。固定パスは先置き・リンク差し替えを許すので使わない。
    `--restricted` は速報判定だけに付与(ファイル系ツールを cwd 内に閉じ込めるため、cwd 外の `/tmp` RAW を読む deliver.sh では allow ルールがあっても拒否される＝実測)。
  - 環境変数・managed 設定は隔離していない(launchd 環境を書き換えられる前提は対象外)。
- collect が twitterapi の認証で落ちる場合は Keychain が launchd 文脈で読めていない →
  `.env` に `TWITTERAPI_IO_KEY=...` を追記し、常駐サーバを `launchctl kickstart -k` で再起動。
- Mac がスリープで配信時刻を逃しても、launchd は起床時に1回だけ遅れて実行(=起床時点の最新を届ける)。
</content>
