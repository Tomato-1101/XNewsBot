from __future__ import annotations

from sqlmodel import select

from xnewsbot.genres import SELECTABLE_KEYS
from xnewsbot.models import NewsItem, Subscriber
from xnewsbot.onboarding import handle_event, parse_time


def _sub(session, uid="U1"):
    return session.exec(select(Subscriber).where(Subscriber.line_user_id == uid)).first()


def ev(kind, uid="U1", **kw):
    base = {"kind": kind, "line_user_id": uid, "display_name": None,
            "text": "", "data": "", "reply_token": "tok"}
    base.update(kw)
    return base


def test_parse_time():
    assert parse_time("7:30") == (7, 30)
    assert parse_time("07:05") == (7, 5)
    assert parse_time("7時") == (7, 0)
    assert parse_time("0730") == (7, 30)
    assert parse_time("25:00") is None
    assert parse_time("あ") is None


def test_follow_starts_onboarding(session, messenger):
    handle_event(session, messenger, ev("follow"))
    sub = _sub(session)
    assert sub is not None
    assert sub.onboarding_step == "genres"
    assert not sub.is_onboarded
    assert len(messenger.last_reply) == 2  # welcome + genre select


def test_full_onboarding_flow(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre:AI"))
    handle_event(session, messenger, ev("postback", data="genre:株"))
    sub = _sub(session)
    assert set(sub.pending_genres) == {"AI", "株"}

    handle_event(session, messenger, ev("postback", data="genre_done"))
    sub = _sub(session)
    assert sub.enabled_genres == ["AI", "株"]  # GENRE_KEYS 順
    assert sub.onboarding_step == "morning"

    # 朝の時刻を自由入力 → 夜の入力へ
    handle_event(session, messenger, ev("message", text="7:30"))
    sub = _sub(session)
    assert (sub.morning_hour, sub.morning_minute) == (7, 30)
    assert sub.onboarding_step == "evening"
    assert not sub.is_onboarded

    # 夜の時刻を設定 → 完了
    handle_event(session, messenger, ev("message", text="22:00"))
    sub = _sub(session)
    assert (sub.evening_hour, sub.evening_minute) == (22, 0)
    assert sub.is_onboarded
    assert sub.onboarding_step == "done"


def test_genre_toggle_off(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre:AI"))
    handle_event(session, messenger, ev("postback", data="genre:AI"))  # もう一度で解除
    assert _sub(session).pending_genres == []


def test_genre_all_then_time_postback(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre_all"))
    # 「すべて」は選択可能ジャンル全件(genres.toml で増減しても追従)
    assert len(_sub(session).pending_genres) == len(SELECTABLE_KEYS)
    handle_event(session, messenger, ev("postback", data="genre_done"))
    handle_event(session, messenger, ev("postback", data="time:0800"))  # 朝
    sub = _sub(session)
    assert (sub.morning_hour, sub.morning_minute) == (8, 0)
    assert sub.onboarding_step == "evening"
    handle_event(session, messenger, ev("postback", data="time:2100"))  # 夜
    sub = _sub(session)
    assert (sub.evening_hour, sub.evening_minute) == (21, 0)
    assert sub.is_onboarded


def _onboard(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre:AI"))
    handle_event(session, messenger, ev("postback", data="genre_done"))
    handle_event(session, messenger, ev("postback", data="time:0700"))  # 朝
    handle_event(session, messenger, ev("postback", data="time:2100"))  # 夜


def test_edit_morning_time_keeps_onboarded(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("postback", data="morning_edit"))
    assert _sub(session).onboarding_step == "morning"
    handle_event(session, messenger, ev("postback", data="time:0900"))
    sub = _sub(session)
    assert (sub.morning_hour, sub.morning_minute) == (9, 0)
    assert sub.onboarding_step == "done"  # 編集は他スロットへ進まず完了
    assert sub.is_onboarded


def test_malformed_time_postback_falls_back_to_menu(session, messenger):
    """不正形式の time: postback でも必ず応答する(無反応で reply_token を捨てない)。"""
    _onboard(session, messenger)
    handle_event(session, messenger, ev("postback", data="morning_edit"))
    before = messenger.replies[-1]
    handle_event(session, messenger, ev("postback", data="time:99"))     # 桁数が不正
    assert messenger.replies[-1] != before and len(messenger.last_reply) == 1
    handle_event(session, messenger, ev("postback", data="time:2599"))   # 時刻として範囲外
    assert len(messenger.last_reply) == 1
    sub = _sub(session)
    assert (sub.morning_hour, sub.morning_minute) == (7, 0)  # 設定は書き換わらない


def test_edit_evening_time_keeps_onboarded(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("postback", data="evening_edit"))
    assert _sub(session).onboarding_step == "evening"
    handle_event(session, messenger, ev("postback", data="time:2230"))
    sub = _sub(session)
    assert (sub.evening_hour, sub.evening_minute) == (22, 30)
    assert sub.morning_hour == 7  # 朝は変わらない
    assert sub.is_onboarded


def test_deliver_now_invokes_callback(session, messenger):
    _onboard(session, messenger)
    called = []
    handle_event(session, messenger, ev("postback", data="deliver_now"),
                 deliver_now=lambda sub: called.append(sub.line_user_id))
    assert called == ["U1"]
    # 直近 reply は配信準備の ack(所要時間の目安を伝える)
    assert "10分" in messenger.last_reply[0]["text"]


def test_mock_trigger_replies_sample_layout(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("message", text="テスト"))
    specs = messenger.last_reply
    # 先頭は「架空」警告、続いて現行レイアウト(要点バブル + 主なニュース + ほかのニュース)
    assert specs[0]["type"] == "text" and "架空" in specs[0]["text"]
    assert specs[1]["type"] == "flex" and specs[1]["contents"]["type"] == "bubble"
    assert specs[2]["type"] == "flex" and specs[2]["contents"]["type"] == "carousel"
    assert specs[2]["alt"].startswith("主なニュース（")
    assert specs[3]["type"] == "flex" and specs[3]["contents"]["type"] == "carousel"
    assert specs[3]["alt"].startswith("ほかのニュース（")
    assert len(specs) <= 5  # reply 上限内
    import json
    blob = json.dumps(specs, ensure_ascii=False)
    assert "市況（前日終値）" in blob  # 架空の市況も出る
    assert "RPA" not in blob          # 配信ジャンルから廃止済み


def test_mock_reply_shows_schedule_crypto_and_summaries(session, messenger):
    """モック返信(テスト)に今日の予定・仮想通貨の市況・小ニュースの要約が出て、5メッセージ以内。"""
    import json

    from xnewsbot import mockdata

    _onboard(session, messenger)
    handle_event(session, messenger, ev("message", text="テスト"))
    specs = messenger.last_reply
    assert len(specs) <= 5
    summary = json.dumps(specs[1], ensure_ascii=False)
    assert "今日の予定" in summary and "米 FOMC 政策金利発表" in summary
    assert "$118,235" in summary and "仮想通貨は直近値・24時間比" in summary
    assert "評価は一般的な傾向で、投資助言ではありません" in summary
    assert "残り使用量" in summary
    assert "X（ニュース取得）  残り 3,040,677クレジット（約$30.41）・あと約308日／今回 9,870" in summary
    assert "LINE（配信）  今月 残り 152/200通・あと約50回／今回 3通" in summary
    assert "主なニュース" in json.dumps(specs[2], ensure_ascii=False)
    assert "ほか " in json.dumps(specs[3], ensure_ascii=False)
    # 小ニュースの要約が一覧に出る(タップしなくても読める)
    small = next(r for r in mockdata._MOCK if r[1] == "small" and r[0] == "AI")
    assert small[4] in json.dumps(specs[3], ensure_ascii=False)


def test_help_mentions_schedule_and_new_genres(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("message", text="ヘルプ"))
    text = messenger.last_reply[0]["text"]
    assert "今日の予定" in text and "仮想通貨" in text and "話題" in text
    assert "見出しのみ" not in text  # 小ニュースにも要約が付いた


def test_group_command_responds_but_chatter_ignored(session, messenger):
    _onboard(session, messenger)
    # グループでの明示コマンド(テスト)は応答する
    handle_event(session, messenger, ev("message", text="テスト", target_id="Cgroup1"))
    assert any(s["type"] == "flex" for s in messenger.last_reply)
    # グループでの雑談には無反応(返信が増えない=荒らさない)
    before = len(messenger.replies)
    handle_event(session, messenger, ev("message", text="おはよう", target_id="Cgroup1"))
    assert len(messenger.replies) == before


def test_group_deliver_now_triggers_callback(session, messenger):
    _onboard(session, messenger)
    called = []
    handle_event(session, messenger, ev("message", text="今すぐ", target_id="Cgroup1"),
                 deliver_now=lambda sub: called.append(sub.line_user_id))
    assert called == ["U1"]


def test_cancel_edit_reverts_and_returns_to_done(session, messenger):
    _onboard(session, messenger)  # AI のみで設定完了
    before = list(_sub(session).enabled_genres)
    handle_event(session, messenger, ev("postback", data="genre_edit"))
    handle_event(session, messenger, ev("postback", data="genre:株"))  # 編集中に追加
    handle_event(session, messenger, ev("postback", data="cancel"))    # キャンセル
    sub = _sub(session)
    assert sub.onboarding_step == "done"
    assert sub.enabled_genres == before          # 変更は破棄
    assert sub.pending_genres == before          # 編集中の選択も破棄


def test_cancel_via_text_word(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("postback", data="morning_edit"))
    assert _sub(session).onboarding_step == "morning"
    handle_event(session, messenger, ev("message", text="キャンセル"))
    assert _sub(session).onboarding_step == "done"


def test_detail_postback_returns_news_detail(session, messenger):
    _onboard(session, messenger)
    item = NewsItem(
        genre_digest_id=1, genre="AI", importance="small", rank=0,
        title="小ニュースの見出し", summary="短い要約。",
        detail="これは長めの詳細解説です。背景や経緯まで含めて厚く書かれています。",
        source_urls=["https://x.com/u/status/1"],
        source_tweets=[{"text": "元ツイートの本文がここに入ります。", "author": "alice",
                        "url": "https://x.com/u/status/1", "views": 100}],
        top_view_count=100,
    )
    session.add(item)
    session.commit()
    session.refresh(item)

    handle_event(session, messenger, ev("postback", data=f"detail:{item.id}"))
    specs = messenger.last_reply
    # メニューではなく、その記事の詳細が返る。タイトル再掲で終わらず detail まで載る
    assert len(specs) == 1 and specs[0]["type"] == "text"
    body = specs[0]["text"]
    assert "小ニュースの見出し" in body
    assert "これは長めの詳細解説です。" in body              # detail を表示
    assert "元ツイートの本文がここに入ります。" not in body    # 元ポストの本文は載せない
    assert "@alice" in body                                  # 元ポストへのリンクは残す
    assert "https://x.com/u/status/1" in body


def test_detail_falls_back_to_summary_when_no_detail(session, messenger):
    _onboard(session, messenger)
    item = NewsItem(
        genre_digest_id=1, genre="AI", importance="small", rank=0,
        title="見出し", summary="detail が無いときの要約。", detail="",
        source_urls=[], source_tweets=[], top_view_count=0,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    handle_event(session, messenger, ev("postback", data=f"detail:{item.id}"))
    assert "detail が無いときの要約。" in messenger.last_reply[0]["text"]


def test_detail_postback_missing_item_is_graceful(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("postback", data="detail:99999"))
    specs = messenger.last_reply
    assert specs[0]["type"] == "text" and "見つかりません" in specs[0]["text"]


def test_genre_done_empty_nudges(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre_done"))
    sub = _sub(session)
    assert sub.onboarding_step == "genres"  # まだ進まない


def test_time_postback_ignored_when_not_setting(session, messenger):
    """時刻設定中(done)でないときに古い time ボタンを押しても朝時刻を書き換えない。"""
    _onboard(session, messenger)  # morning=07:00, step=done
    assert _sub(session).onboarding_step == "done"
    handle_event(session, messenger, ev("postback", data="time:0600"))
    sub = _sub(session)
    assert (sub.morning_hour, sub.morning_minute) == (7, 0)  # 変わらない
    assert "メニュー" in messenger.last_reply[0]["text"]


def test_detail_postback_stable_key_survives_reingest(session, messenger):
    """安定キー(日付:slot:genre:rank)の詳細タップは、再収集で id が変わっても引ける。"""
    from datetime import date

    from xnewsbot import digest
    from xnewsbot.curator import parse_curated

    from .conftest import curated_items, make_tweets

    d1 = digest.ingest_curated(session, "AI", date(2026, 6, 8), "morning",
                               parse_curated(curated_items(big=1, small=1)), make_tweets(3))
    title0 = digest.items_of_digest(session, d1.id)[0].title

    # 安定キー形式の postback を旧 _handle_detail(整数idのみ)は解けない。新実装で解けることを確認。
    handle_event(session, messenger, ev("postback", data="detail:20260608:morning:AI:0"))
    assert title0 in messenger.last_reply[0]["text"]

    # 「今すぐ配信」等で取り込み直しても(本番では NewsItem.id が変わる)、同じ安定キーで引ける
    digest.ingest_curated(session, "AI", date(2026, 6, 8), "morning",
                          parse_curated(curated_items(big=1, small=1)), make_tweets(3))
    handle_event(session, messenger, ev("postback", data="detail:20260608:morning:AI:0"))
    assert title0 in messenger.last_reply[0]["text"]
