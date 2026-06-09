from __future__ import annotations

from sqlmodel import select

from xnewsbot.genres import SELECTABLE_KEYS
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
    # 直近 reply は準備中の ack
    assert "準備" in messenger.last_reply[0]["text"]


def test_mock_trigger_replies_sample_layout(session, messenger):
    _onboard(session, messenger)
    handle_event(session, messenger, ev("message", text="テスト"))
    specs = messenger.last_reply
    # 先頭は「架空」警告、続いて現行レイアウト(大=縦長1枚 / 小=横カルーセル)
    assert specs[0]["type"] == "text" and "架空" in specs[0]["text"]
    assert any(s["type"] == "flex" and s["alt"] == "大ニュース" for s in specs)
    assert len(specs) <= 5  # reply 上限内


def test_group_command_responds_but_chatter_ignored(session, messenger):
    _onboard(session, messenger)
    # グループでの明示コマンド(テスト)は応答する
    handle_event(session, messenger, ev("message", text="テスト", target_id="Cgroup1"))
    assert any(s["type"] == "flex" and s["alt"] == "大ニュース" for s in messenger.last_reply)
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


def test_genre_done_empty_nudges(session, messenger):
    handle_event(session, messenger, ev("follow"))
    handle_event(session, messenger, ev("postback", data="genre_done"))
    sub = _sub(session)
    assert sub.onboarding_step == "genres"  # まだ進まない
