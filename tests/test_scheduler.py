from __future__ import annotations

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
