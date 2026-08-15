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

- 常駐: launchd `com.tomato.xnewsbot`(uvicorn:8010) / `-ngrok` / `-deliver`(07:45 起動→08:00 送信) /
  `-recover`(12:30・17:00・21:00 に `deliver.sh --recover`。当日朝が未配信の日だけ集め直して送る自動復旧。
  配信済みの日は `pipeline.py pending` を見て即終了するので無害。2026-08-15 追加)。
  ※ `-breaking`(速報監視) は **2026-08-02 に停止**（bootout + disable + `breaking_enabled=False`）。
  状態確認: `launchctl list | grep xnewsbot`。plist 変更は bootout→bootstrap（kickstart では反映されない）。
- **LINE 無料枠は 200通/月**。カウントは「メッセージ数 × 宛先数」で、グループ宛 push はグループ内の
  友だち人数分（実測3通）課金される。定時ダイジェストは Flex が 3〜4 メッセージに分割されるので
  **1回の配信で 3〜4通**消費する（1通ではない）。速報を日次5件で回した結果 18通/日 → 11日で枯渇し、
  2026-07-22〜31 の10日間は全 push が 429 で不着だった。新しい送信経路を足すときは必ずこの計算をする。
  残枠は `GET /v2/bot/message/quota/consumption`、日別実績は `GET /v2/bot/insight/message/delivery?date=YYYYMMDD` で確認できる（無料）。
- **速報リアルタイム配信**（2026-07-02 追加・2026-08-02 停止, `scripts/monitor_breaking.py` / `-breaking` plist）: 無料(Google ニュースRSS+GDELT補助)で
  速報を検出し「今のグループ」(DB `subscriber.push_to` のグループ)へ即 push。乱造防止=重複排除(SQLite `breaking_sent`)+日次上限
  (`breaking_max_per_day`,既定5=LINE無料枠200通/月を守る)+鮮度窓 の3重。積極度=`breaking_level`(strict/medium/broad)。
  **実グループへ送るので初回稼働はユーザーの明示 GO を得てから bootstrap する**。動作確認は送信しない `--dry-run`。
  - **LLM判定層**（同日追加, トグル `breaking_judge_enabled` 既定ON）: ヒューリスティック通過分をヘッドレス Claude
    (`claude --model claude-opus-4-8`, `ops/breaking_judge_prompt.md`)が「今すぐ割り込む価値があるか」で最終判定。
    判定失敗(タイムアウト/セッション上限/パース不能)は **fail-closed=送らない**。見送りは `breaking_rejected` に記録し再判定しない。
- **定時ダイジェストの候補は X(twitterapi.io)＋無料ニュース(Google ニュースRSS)をマージ**（`collect_use_newsfeeds`,質向上）。
  X が空でもニュースで配信継続。ニュース候補は `source:"news"`・エンゲージ0(キュレーションプロンプトが信頼扱い)。
- **配信は 1日1回・朝08:00 のみ**（2026-06-24 にコスト節約で夜21:00 スロットを停止）。夜スロットのコード/UIトグル
  (evening_enabled) は残してあり、`-deliver` plist の StartCalendarInterval に 20:45 dict を戻せば再開できる。
- 配信時刻を変えるときは **plist の StartCalendarInterval（15分前）と `ops/deliver.sh` の MORNING_HHMM（定刻）の両方**を更新。
- テスト: `.venv/bin/python -m pytest -q`。レイアウト確認は LINE で「テスト」（モック・無料・即時）。
  **「今すぐ配信」は push 課金（無料枠 200通/月）を消費するので乱発しない。**
- LINE 表示の不具合報告（「日本語が出ない」「詳細が出ない」等）を受けたら、推測で直さず
  まず実際の spec 生成結果 / webhook ログ / スクリーンショットで現象を確認してから修正（`root-cause-debug` スキル）。
- twitterapi.io は非決定的（空ページ・タイムアウトあり）。0件=即異常ではないが、
  **タイムアウトでジャンルが空になった場合に既存の有効なダイジェストを空で上書きしない**こと（過去に二次被害あり）。

## 触らないもの

- XAgent / x-research のコード（鍵は Keychain `twitterapi_io_key` を共有するがコードは独立）
- `.env` / 秘密情報（読まない・出さない・コミットしない）
