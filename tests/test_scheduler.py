from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from xnewsbot import digest, scheduler
from xnewsbot.curator import parse_curated
from xnewsbot.models import Subscriber

from .conftest import curated_items, make_tweets

JST = ZoneInfo("Asia/Tokyo")
D = date(2026, 6, 8)


def _onboarded(**kw) -> Subscriber:
    base = dict(line_user_id="U1", enabled_genres=["AI"], is_onboarded=True,
                morning_hour=8, morning_minute=0, evening_hour=21, evening_minute=0)
    base.update(kw)
    return Subscriber(**base)


def test_is_due_after_time():
    sub = _onboarded()
    assert scheduler.is_due(sub, datetime(2026, 6, 8, 8, 0, tzinfo=JST), "morning")
    assert scheduler.is_due(sub, datetime(2026, 6, 8, 10, 0, tzinfo=JST), "morning")  # catch-up
    assert scheduler.is_due(sub, datetime(2026, 6, 8, 21, 0, tzinfo=JST), "evening")


def test_is_due_before_time():
    sub = _onboarded()
    assert not scheduler.is_due(sub, datetime(2026, 6, 8, 7, 59, tzinfo=JST), "morning")
    assert not scheduler.is_due(sub, datetime(2026, 6, 8, 20, 0, tzinfo=JST), "evening")


def test_is_due_already_delivered_slot():
    sub = _onboarded(last_morning_on=D)
    assert not scheduler.is_due(sub, datetime(2026, 6, 8, 9, 0, tzinfo=JST), "morning")
    # 夜はまだ
    assert scheduler.is_due(sub, datetime(2026, 6, 8, 22, 0, tzinfo=JST), "evening")


def test_is_due_slot_disabled_or_no_genre():
    when = datetime(2026, 6, 8, 22, 0, tzinfo=JST)
    assert not scheduler.is_due(_onboarded(evening_enabled=False), when, "evening")
    assert not scheduler.is_due(_onboarded(enabled_genres=[]), when, "evening")
    assert not scheduler.is_due(_onboarded(is_onboarded=False), when, "evening")


def test_slot_for_now():
    assert scheduler.slot_for_now(datetime(2026, 6, 8, 8, 0, tzinfo=JST)) == "morning"
    assert scheduler.slot_for_now(datetime(2026, 6, 8, 21, 0, tzinfo=JST)) == "evening"


def test_missing_genres(session):
    assert digest.missing_genres(session, ["AI"], D, "morning") == ["AI"]
    digest.ingest_curated(session, "AI", D, "morning", [], [])  # 空でもダイジェストは作る
    assert digest.missing_genres(session, ["AI"], D, "morning") == []
    # 別スロットはまだ無い
    assert digest.missing_genres(session, ["AI"], D, "evening") == ["AI"]


def test_missing_for_delivery_includes_always_genres(session):
    """揃い判定は購読ジャンル+常時ジャンル(特大)で行う(特大が欠けたまま配信しない)。"""
    sub = _onboarded()
    digest.ingest_curated(session, "AI", D, "morning", [], [])
    # 購読分(AI)は揃っているが、常時ジャンル(特大)が未完成 → まだ配信しない
    assert scheduler.missing_for_delivery(session, sub, D, "morning") == ["特大"]
    digest.ingest_curated(session, "特大", D, "morning", [], [])
    assert scheduler.missing_for_delivery(session, sub, D, "morning") == []


def test_deliver_to_subscriber_from_db(session, messenger):
    digest.ingest_curated(session, "AI", D, "evening",
                          parse_curated(curated_items(1, 2)), make_tweets(5))
    sub = _onboarded(line_user_id="U9")
    session.add(sub)
    session.commit()
    session.refresh(sub)

    now = datetime(2026, 6, 8, 22, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(session, sub, "evening", messenger=messenger, now_local=now)
    assert messenger.pushes
    assert sub.last_evening_on == D
    assert sub.last_morning_on is None
    assert "flex" in [s["type"] for s in specs]


def test_deliver_to_subscriber_explicit_digest_date(session, messenger):
    """0時跨ぎ: 収集日(digest_date)を明示すれば、push 時に日付が変わっていても
    当該日のダイジェストを配信する(現在日で探して空配信にならない)。"""
    digest.ingest_curated(session, "AI", D, "evening",
                          parse_curated(curated_items(1, 1)), make_tweets(3))
    sub = _onboarded(line_user_id="U10")
    session.add(sub)
    session.commit()
    session.refresh(sub)

    after_midnight = datetime(2026, 6, 9, 0, 1, tzinfo=JST)  # D の翌日0:01
    specs = scheduler.deliver_to_subscriber(
        session, sub, "evening", messenger=messenger,
        now_local=after_midnight, mark_delivered=False, digest_date=D)
    assert "flex" in [s["type"] for s in specs]  # 空配信(textのみ)でなく中身が届く
    # digest_date を渡さない従来動作では翌日分を探して空になる(回帰確認)
    specs_old = scheduler.deliver_to_subscriber(
        session, sub, "evening", messenger=messenger,
        now_local=after_midnight, mark_delivered=False)
    assert [s["type"] for s in specs_old] == ["text"]


def test_due_subscribers_returns_slot_pairs(session):
    s1 = _onboarded(line_user_id="A", morning_hour=6)
    s2 = _onboarded(line_user_id="B", morning_hour=23, evening_hour=23)  # まだ来てない
    for s in (s1, s2):
        session.add(s)
    session.commit()

    fixed = datetime(2026, 6, 8, 9, 0, tzinfo=JST)  # 朝は過ぎ、夜はまだ
    due = scheduler.due_subscribers(session, now_provider=lambda tz: fixed)
    assert due == [(due[0][0], "morning")]
    assert due[0][0].line_user_id == "A"


def test_build_news_items_keeps_media_time_kind_and_score():
    from xnewsbot.curator import CuratedItem

    tweets = [
        {"text": "X の投稿", "viewCount": 500, "url": "https://x.com/a/status/1",
         "createdAt": "Tue Sep 30 12:34:56 +0000 2026",
         "author": {"userName": "alice", "followers": 10}, "media": "@alice", "kind": "x"},
        {"source": "news", "text": "見出し", "summary": "s", "body": "b", "url": "https://n/1",
         "createdAt": "2026-09-30T21:00:00+09:00", "author": {"userName": "日経"}, "media": "日経",
         "viewCount": 0, "likeCount": 0},
        {"text": "旧形式", "viewCount": 1, "url": "https://x.com/b/status/2", "createdAt": "壊れた日時",
         "author": {"userName": "bob"}},                       # media/kind 無し・日時解析不能
        {"source": "news", "text": "RSS", "url": "https://n/2",
         "createdAt": "Tue, 30 Sep 2026 03:00:00 GMT", "author": {"userName": "ロイター"}},
    ]
    ci = CuratedItem(title="t", summary="s", importance="big", score=87, source_idxs=[0, 1, 2, 3])
    [item] = digest._build_news_items(1, "AI", [ci], tweets)
    assert item.score == 87
    st = item.source_tweets
    assert [s["media"] for s in st] == ["@alice", "日経", "@bob", "ロイター"]
    assert [s["kind"] for s in st] == ["x", "news", "x", "news"]
    assert [s["created_at"] for s in st] == [
        "2026-09-30T12:34:56+00:00", "2026-09-30T12:00:00+00:00", "", "2026-09-30T03:00:00+00:00"]
    assert st[0]["author"] == "alice" and st[0]["url"] == "https://x.com/a/status/1"


def test_build_news_items_orders_big_first_then_score():
    from xnewsbot.curator import CuratedItem

    def ci(title, imp, score):
        return CuratedItem(title=title, summary="s", importance=imp, score=score, source_idxs=[])

    curated = [ci("s40", "small", 40), ci("b70", "big", 70), ci("s60", "small", 60),
               ci("b90", "big", 90), ci("s60b", "small", 60)]
    items = digest._build_news_items(1, "AI", curated, [])
    assert [i.title for i in items] == ["b90", "b70", "s60", "s60b", "s40"]   # 同点は元の順
    assert [i.rank for i in items] == [0, 1, 2, 3, 4]


def test_save_and_get_market_replaces_same_slot(session):
    m1 = [{"key": "nikkei", "label": "日経平均", "close": 1.0, "change": 0.0, "change_pct": 0.0,
           "asof": "2026-06-05", "kind": "index"}]
    m2 = [dict(m1[0], close=2.0)]
    assert digest.get_market(session, D, "morning") == []
    digest.save_market(session, D, "morning", m1)
    digest.save_market(session, D, "morning", m2)  # 同日同スロットは置換
    assert digest.get_market(session, D, "morning") == m2
    assert digest.get_market(session, D, "evening") == []  # 別スロットは別
    digest.save_market(session, D, "morning", [])  # 取得失敗(空)で既存を消さない
    assert digest.get_market(session, D, "morning") == m2


def test_deliver_to_subscriber_includes_market(session, messenger):
    import json

    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(1, 1)), make_tweets(3))
    digest.save_market(session, D, "morning", [
        {"key": "nikkei", "label": "日経平均", "close": 45000.0, "change": 100.0,
         "change_pct": 0.22, "asof": "2026-06-05", "kind": "index"}])
    sub = _onboarded(line_user_id="U11")
    session.add(sub)
    session.commit()
    session.refresh(sub)
    now = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(session, sub, "morning", messenger=messenger,
                                            now_local=now, mark_delivered=False)
    blob = json.dumps(specs, ensure_ascii=False)
    assert "市況（前日終値）" in blob and "45,000" in blob and "+0.22%" in blob
    assert specs[0]["alt"].startswith("朝のニュース｜大ニュース0")


def test_deliver_to_subscriber_includes_schedule(session, messenger):
    """当日・当スロットの「今日の予定」を要点バブルに渡す(配信時刻より前の予定は出さない)。"""
    import json

    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(1, 1)), make_tweets(3))
    ev = {"kind": "indicator", "country": "US", "result": "", "importance": 5}
    digest.save_schedule(session, D, "morning", [
        dict(ev, at="2026-06-08T21:30:00+09:00", time_label="21:30",
             name="米 雇用統計（非農業部門雇用者数）", forecast="12.0万人", previous="14.2万人"),
        dict(ev, at="2026-06-08T07:00:00+09:00", time_label="07:00",
             name="過ぎた指標", forecast="", previous=""),
    ])
    digest.save_schedule(session, D, "evening", [
        dict(ev, at=None, time_label="未定", name="夜スロットの予定", forecast="", previous="")])
    sub = _onboarded(line_user_id="U12")
    session.add(sub)
    session.commit()
    session.refresh(sub)
    now = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(session, sub, "morning", messenger=messenger,
                                            now_local=now, mark_delivered=False)
    blob = json.dumps(specs[0], ensure_ascii=False)
    assert "今日の予定" in blob and "米 雇用統計（非農業部門雇用者数）" in blob
    assert "予想 12.0万人｜前回 14.2万人" in blob
    assert "過ぎた指標" not in blob and "夜スロットの予定" not in blob


def test_save_get_x_usage_does_not_overwrite_with_none(session):
    assert digest.get_x_usage(session, D, "morning") is None
    digest.save_x_usage(session, D, "morning", {"used": 9870, "remaining": 3_040_677})
    assert digest.get_x_usage(session, D, "morning") == {"used": 9870, "remaining": 3_040_677}
    digest.save_x_usage(session, D, "morning", None)  # 取得失敗の再実行で良い値を消さない
    assert digest.get_x_usage(session, D, "morning") == {"used": 9870, "remaining": 3_040_677}
    digest.save_x_usage(session, D, "morning", {"used": 1, "remaining": 2})  # 同日同スロットは置き換え
    assert digest.get_x_usage(session, D, "morning") == {"used": 1, "remaining": 2}
    assert digest.get_x_usage(session, D, "evening") is None


def test_deliver_to_subscriber_shows_saved_x_usage(session, messenger):
    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(1, 2)), make_tweets(5))
    digest.save_x_usage(session, D, "morning", {"used": 9870, "remaining": 3_040_677})
    sub = _onboarded(line_user_id="U9")
    session.add(sub)
    session.commit()
    session.refresh(sub)
    now = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(session, sub, "morning", messenger=messenger, now_local=now)
    assert "X（ニュース取得）  残り 3,040,677クレジット（約$30.41）・あと約308日／今回 9,870" in json.dumps(
        specs[0], ensure_ascii=False)


def test_deliver_to_subscriber_passes_line_quota(session, messenger):
    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(1, 2)), make_tweets(5))
    sub = _onboarded(line_user_id="U9")
    session.add(sub)
    session.commit()
    session.refresh(sub)
    asked: list[str] = []

    def fetch_quota(to):
        asked.append(to)
        return {"limit": 200, "used": 45, "cost": 3}

    messenger.fetch_quota = fetch_quota
    now = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(session, sub, "morning", messenger=messenger, now_local=now)
    assert asked == [sub.push_target]
    assert "LINE（配信）  今月 残り 152/200通・あと約50回／今回 3通" in json.dumps(specs[0], ensure_ascii=False)


def test_deliver_to_subscriber_without_fetch_quota_hides_line_row(session, messenger):
    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(1, 2)), make_tweets(5))
    sub = _onboarded(line_user_id="U9")
    session.add(sub)
    session.commit()
    session.refresh(sub)
    now = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(session, sub, "morning", messenger=messenger, now_local=now)
    # 取れなくても行は出す(どこにあるか迷わせない)
    assert "LINE（配信）  取得できませんでした" in json.dumps(specs[0], ensure_ascii=False)
