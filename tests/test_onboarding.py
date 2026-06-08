from __future__ import annotations

from sqlmodel import select

from xnewsbot.models import Subscriber
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
    assert sub.onboarding_step == "time"

    # 自由入力で時刻設定 → 完了
    handle_event(session, messenger, ev("message", text="7:30"))
    sub = _sub(session)
    assert (sub.deliver_hour, sub.deliver_minute) == (7, 30)
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
    assert len(_sub(session).pending_genres) == 4
    handle_event(session, messenger, ev("postback", data="genre_done"))
    handle_event(session, messenger, ev("postback", data="time:0800"))
    sub = _sub(session)
    assert (sub.deliver_hour, sub.deliver_minute) == (8, 0)
    assert sub.is_onboarded


def _onboard(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre:AI"))
    handle_event(session, messenger, ev("postback", data="genre_done"))
    handle_event(session, messenger, ev("postback", data="time:0700"))


def test_edit_time_keeps_onboarded(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("postback", data="time_edit"))
    assert _sub(session).onboarding_step == "time"
    handle_event(session, messenger, ev("postback", data="time:0900"))
    sub = _sub(session)
    assert (sub.deliver_hour, sub.deliver_minute) == (9, 0)
    assert sub.is_onboarded


def test_deliver_now_invokes_callback(session, messenger):
    _onboard(session, messenger)
    called = []
    handle_event(session, messenger, ev("postback", data="deliver_now"),
                 deliver_now=lambda sub: called.append(sub.line_user_id))
    assert called == ["U1"]
    # 直近 reply は準備中の ack
    assert "準備" in messenger.last_reply[0]["text"]


def test_genre_done_empty_nudges(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre_done"))
    sub = _sub(session)
    assert sub.onboarding_step == "genres"  # まだ進まない
