from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from xnewsbot import scheduler, xclient
from xnewsbot.curator import Curator
from xnewsbot.models import Subscriber

from .conftest import fake_complete_factory, make_tweets

JST = ZoneInfo("Asia/Tokyo")


def _onboarded(**kw) -> Subscriber:
    base = dict(line_user_id="U1", enabled_genres=["AI"], is_onboarded=True,
                deliver_hour=7, deliver_minute=0)
    base.update(kw)
    return Subscriber(**base)


def test_is_due_after_time():
    sub = _onboarded()
    assert scheduler.is_due(sub, datetime(2026, 6, 8, 7, 0, tzinfo=JST))
    assert scheduler.is_due(sub, datetime(2026, 6, 8, 9, 0, tzinfo=JST))  # catch-up


def test_is_due_before_time():
    sub = _onboarded()
    assert not scheduler.is_due(sub, datetime(2026, 6, 8, 6, 59, tzinfo=JST))


def test_is_due_already_delivered_today():
    sub = _onboarded(last_delivered_on=date(2026, 6, 8))
    assert not scheduler.is_due(sub, datetime(2026, 6, 8, 8, 0, tzinfo=JST))


def test_is_due_not_onboarded_or_no_genre():
    assert not scheduler.is_due(_onboarded(is_onboarded=False), datetime(2026, 6, 8, 8, 0, tzinfo=JST))
    assert not scheduler.is_due(_onboarded(enabled_genres=[]), datetime(2026, 6, 8, 8, 0, tzinfo=JST))


def test_deliver_to_subscriber(session, messenger, monkeypatch):
    monkeypatch.setattr(xclient, "collect", lambda genre, **k: make_tweets(5))
    cur = Curator(complete=fake_complete_factory(big=1, small=2))
    sub = _onboarded(line_user_id="U9")
    session.add(sub)
    session.commit()
    session.refresh(sub)

    now = datetime(2026, 6, 8, 9, 0, tzinfo=JST)
    specs = scheduler.deliver_to_subscriber(
        session, sub, curator=cur, messenger=messenger, now_local=now
    )
    assert messenger.pushes  # push された
    assert sub.last_delivered_on == now.date()
    types = [s["type"] for s in specs]
    assert "flex" in types  # 大/小ニュースの Flex を含む


def test_due_subscribers_filters(session, monkeypatch):
    s1 = _onboarded(line_user_id="A", deliver_hour=6)
    s2 = _onboarded(line_user_id="B", deliver_hour=23)  # まだ来てない
    for s in (s1, s2):
        session.add(s)
    session.commit()

    fixed = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
    due = scheduler.due_subscribers(session, now_provider=lambda tz: fixed)
    assert {s.line_user_id for s in due} == {"A"}
