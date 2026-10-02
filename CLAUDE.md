# XNewsBot — プロジェクト指針（毎セッション自動読込）

X(Twitter) 発ニュースを Claude Code でキュレーションし LINE Bot で配信する常駐システム。
**新しいセッションはまず `HANDOFF.md` を読む**（目的・決定事項・launchd 3点構成・ハマりどころが全部ある）。
仕様の一次資料は HANDOFF.md と `ops/curate_prompt.md`。このファイルには「毎回守る作業規律」だけを置く。

## キュレーションセッション（定期実行・ヘッドレス）の規律

過去の定期実行で**毎回同じエラー**が出ていた。以下を最初から守る:

1. **raw JSON は大きい（250KB 超が普通）**。いきなり全量 Read しない。
   最初の Read から `offset`/`limit` を使い、分割して全ジャンルを読み切る（読み残しジャンル禁止）。
2. **出力先ファイルが既に存在する場合、Write の前に必ずその場で Read する**（Read-before-Write）。
3. 書き終わったら「done」と言う前にセルフチェック:
   - 出力 JSON の `genres` キーが raw の全ジャンルを含むか
   - `importance` の値・各ジャンルの件数制限が `ops/curate_prompt.md` の現行仕様どおりか（仕様はプロンプト側が正。ここに複製しない）
   - `source_idxs` が「そのニュースを置いたジャンルの配列」の範囲内か
4. 仕様で迷ったら推測せず `ops/curate_prompt.md` を読み直す。

## 運用・検証

- 常駐: launchd `com.tomato.xnewsbot`(uvicorn:18010) / `-admin`(管理画面 8011) / `-ngrok` / `-deliver`(07:15 起動→08:00 送信。キュレーションが約17分かかるため 2026-10-01 に45分前起動へ) /
  `-recover`(12:30・17:00・21:00 に `deliver.sh --recover`。当日朝が未配信の日だけ集め直して送る自動復旧。
  配信済みの日は `pipeline.py pending` を見て即終了するので無害。21:00 の回も失敗したときだけ
  `pipeline.py alert` が LINE へ原因つき1通を送る(本人宛=1通)。2026-08-15 追加)。
  ※ `-breaking`(速報監視) は **2026-08-02 に停止**（bootout + disable + `breaking_enabled=False`）。
  状態確認: `launchctl list | grep xnewsbot`。plist 変更は bootout→bootstrap（kickstart では反映されない）。
- **LINE 無料枠は 200通/月**。カウントは「push 1回 × 宛先人数」で、1回の push に入れるメッセージ数（最大5）は
  通数に影響しない（公式の料金ページと 2026-10-01 の実測で確認。旧記載「メッセージ数 × 宛先数」は誤り）。
  グループ宛 push はグループ内の友だち人数分（実測3人=3通）課金されるので、定時ダイジェストは
  **1回の配信で3通**。メッセージを分けて push を2回に分けると倍になる。速報を日次5件で回した結果 18通/日 → 11日で枯渇し、
  2026-07-22〜31 の10日間は全 push が 429 で不着だった。新しい送信経路を足すときは必ずこの計算をする。
  残枠は `GET /v2/bot/message/quota/consumption`、日別実績は `GET /v2/bot/insight/message/delivery?date=YYYYMMDD` で確認できる（無料）。
  要点バブルの末尾に「X取得 今回/残りクレジット・あと約N日」と「LINE 今月 残り N/200通（今回3通）」を毎回出す（10-02 追加。
  X は収集前後の残高差 `pipeline.compute_x_usage`→`XUsageSnapshot`、LINE は push 直前に `LineMessenger.fetch_quota`。取得失敗は行を出さないだけ）。
- **速報リアルタイム配信**（2026-07-02 追加・2026-08-02 停止, `scripts/monitor_breaking.py` / `-breaking` plist）: 無料(Google ニュースRSS+GDELT補助)で
  速報を検出し「今のグループ」(DB `subscriber.push_to` のグループ)へ即 push。乱造防止=重複排除(SQLite `breaking_sent`)+日次上限
  (`breaking_max_per_day`,既定5=LINE無料枠200通/月を守る)+鮮度窓 の3重。積極度=`breaking_level`(strict/medium/broad)。
  **実グループへ送るので初回稼働はユーザーの明示 GO を得てから bootstrap する**。動作確認は送信しない `--dry-run`。
  - **LLM判定層**（同日追加, トグル `breaking_judge_enabled` 既定ON）: ヒューリスティック通過分をヘッドレス Claude
    (`claude --model claude-opus-4-8`, `ops/breaking_judge_prompt.md`)が「今すぐ割り込む価値があるか」で最終判定。
    判定失敗(タイムアウト/セッション上限/パース不能)は **fail-closed=送らない**。見送りは `breaking_rejected` に記録し再判定しない。
- **定時ダイジェストの候補は X(twitterapi.io)＋無料ニュースをマージ**（`collect_use_newsfeeds`,質向上）。
  X が空でもニュースで配信継続。ニュース候補は `source:"news"`・エンゲージ0(キュレーションプロンプトが信頼扱い)。
  取得元の構成（2026-10-01 全面見直し。ジャンル別の語・アカウント・RSS は `config/genres.toml` が正）:
  - X: 1ジャンル3クエリ＝日本語＋英語(いいね下限高め)＋公式アカウント(`from:`、`official` 付き)。RT・除外アカウント・重複を除く。
  - ニュース: 直取り RSS(`feeds`)＋Google ニュース(日本語/英語で別枠)。株価の銘柄ページは除外。
    直リンク記事は本文を取得して先頭を `body` に入れる(`xnewsbot/articles.py`、有料媒体は対象外)。
  - 市況: Yahoo chart(鍵なし)で前日終値(`xnewsbot/market.py`)。TOPIX は取れないので出さない。
    ビットコイン・イーサリアムは CoinGecko(鍵なし)の直近値と24時間比。
  - 今日の予定(`xnewsbot/schedule.py`、10-02 追加): みんかぶ経済指標・Fed calendar.json・日銀・Nasdaq/IR BANK の決算予定を
    要点バブルに表示し、直近24hの指標結果は raw の `indicator_results` でキュレーションに渡す(予想比と評価の矢印を書く材料)。
    スクレイピングなので構造が変わると空になるだけ(配信は続く)。
    決算(10-02 改訂): 日本は株探の週間予定で★の付いた注目銘柄だけ(取れない日は IR BANK の時価総額上位で代用し「代用」と表示)、
    米国は時価総額 500億ドル以上。決算サプライズ=前営業日の決算で株価が大きく動いた銘柄(日本: 株探 PTS ±5%以上かつ決算/修正の見出しあり、
    米国: Yahoo chart の時間外・当日)。raw の `earnings_surprises` でも渡す。株探は直列・0.3秒間隔・fetch 開始から90秒で打ち切り。
  - 話題ジャンル(10-02 追加): X のバズ投稿(`x_queries`)＋急上昇ワード(`trend_sources`=Yahoo!リアルタイム/Google トレンド/はてブ、
    `xnewsbot/trends.py`)＋総合 RSS。仮想通貨はキー「暗号資産」・表示名「仮想通貨」。
  - 再掲防止: 直近3日の配信見出しを raw の `recent_titles` で渡し、続報だけ採る。RPA ジャンルは 10-01 に廃止。
  - 監視アカウント(10-02 追加, `xnewsbot/watch.py`、genres.toml の `watch = true`): 管理画面で登録した X アカウントの
    前回配信以降の投稿(RT・他人への返信を除く)を、候補があれば必ず独立の組でキュレーションする。
    取得期間は DB 横の `watch_state.json`。配信日の最初の取り込みだけ前進(同日の今すぐ更新では進めない)。
- LINE の段組み(10-02 改訂、LINE PC 実画面で確認して設計): 1通目=要点(目次「→ k通目」付き)＋マーケット(市況・予定・注目決算・決算サプライズ)。
  2通目以降=原則1ジャンル1通(中は大きいニュース→注目→その他の見出し)。通数が足りない日だけ隣のジャンルを1通にまとめ、
  それでも入らない分は見出しを削って「ほか N 件は省略」。1回の push は最大5通のまま。「特大」ジャンルは 10-02 に廃止し話題へ統合。
- AI解説(10-02): LINE の詳細のクイックリプライと Web のボタンから、ヘッドレス `claude --model sonnet`(WebSearch)で記事ごとに作る
  (`xnewsbot/explain.py`、`ops/explain_prompt.md`)。作成後は押した人のトークへ push(1通×人数)。
- raw が 300KB 以上なら `pipeline.py split` でジャンルを2組（`CURATE_GROUPS`）に分け、claude を並列実行して `merge` する
  （489KB を1セッションで読むと文脈があふれた）。組をまたぐ同じ出来事は1件にまとめられないので、重なりやすいジャンルは同じ組に入れる。
- **配信は 1日1回・朝08:00 のみ**（2026-06-24 にコスト節約で夜21:00 スロットを停止）。夜スロットのコード/UIトグル
  (evening_enabled) は残してあり、`-deliver` plist の StartCalendarInterval に 20:45 dict を戻せば再開できる。
- 配信時刻を変えるときは **plist の StartCalendarInterval（45分前）と `ops/deliver.sh` の MORNING_HHMM（定刻）の両方**を更新。
- テスト: `.venv/bin/python -m pytest -q`。レイアウト確認は LINE で「テスト」（モック・無料・即時）。
  **「今すぐ配信」は push 課金（無料枠 200通/月）を消費するので乱発しない。**
- LINE 表示の不具合報告（「日本語が出ない」「詳細が出ない」等）を受けたら、推測で直さず
  まず実際の spec 生成結果 / webhook ログ / スクリーンショットで現象を確認してから修正（`root-cause-debug` スキル）。
- twitterapi.io は非決定的（空ページ・タイムアウトあり）。0件=即異常ではないが、
  **タイムアウトでジャンルが空になった場合に既存の有効なダイジェストを空で上書きしない**こと（過去に二次被害あり）。

## 触らないもの

- XAgent / x-research のコード（コードは独立）。twitterapi.io の鍵は `.key` と中央 Keychain `TWITTERAPI_IO_KEY`/`shared`（…3e15）。
  旧 Keychain 項目 `twitterapi_io_key`（残高切れの…d015）は 2026-10-02 に削除済み（xclient の参照は残っているが無害）。
- `.env` / 秘密情報（読まない・出さない・コミットしない）
