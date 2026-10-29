"""ニュースの評価(興味あり/興味なし): LINE の postback、記録の上書き、raw の preferences の集計、
LINE の一覧に付く「評価する」と容量上限。LINE へは送らない(FakeMessenger)。"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import select

from xnewsbot import feedback, mockdata
from xnewsbot import line_client as lc
from xnewsbot.models import GenreDigest, NewsFeedback, NewsItem, Subscriber
from xnewsbot.onboarding import (FEEDBACK_FAILED_MSG, FEEDBACK_MISSING_MSG, FEEDBACK_MOCK_MSG,
                                 handle_event)

from .test_line_client import (D, NOW, _HEAVY_MARKET, _HEAVY_SCHEDULE, _actions, _bulk, _carousels,
                               _check_accounting, _check_limits_and_coverage, _heavy, _omitted, _texts)

_PL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("pipeline", _PL_PATH)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


def _item(session, title="AIの新モデル発表", genre="AI", day=date(2026, 10, 2)) -> NewsItem:
    gd = GenreDigest(digest_date=day, slot="morning", genre=genre)
    session.add(gd)
    session.commit()
    session.refresh(gd)
    it = NewsItem(genre_digest_id=gd.id, genre=genre, importance="small", rank=0, title=title,
                  summary="要約")
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


def _ev(data: str, uid: str | None = "U1", target: str | None = "Cgroup") -> dict:
    return {"kind": "postback", "line_user_id": uid, "display_name": None, "text": "",
            "data": data, "reply_token": "tok", "target_id": target}


def _owner(session, push_to: str | None = "Cgroup", uid: str = "Uowner") -> Subscriber:
    """評価を受け付ける配信先(push_to)を持つ購読者。"""
    sub = Subscriber(line_user_id=uid, push_to=push_to, is_onboarded=True)
    session.add(sub)
    session.commit()
    return sub


def _key(it: NewsItem, day: str = "20261002") -> str:
    return f"{day}:morning:{it.genre}:{feedback.title_hash(it.title)}"


def _rows(session) -> list[NewsFeedback]:
    return session.exec(select(NewsFeedback)).all()


def _buttons(spec) -> list[dict]:
    return [n for n in _walk(spec["contents"]) if n.get("type") == "button"]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# ---------------------------------------------------------------- LINE の postback

def test_rate_postback_replies_bubble_with_two_buttons(session, messenger):
    """「評価する」には見出しと「興味あり」「興味なし」のボタンのバブル(LINE PC でも押せて、メッセージに残る)。
    ボタンに displayText は付けない(グループに文字を流さない)。"""
    _owner(session)
    it = _item(session)
    key = _key(it)
    handle_event(session, messenger, _ev(f"rate:{key}"))
    (spec,) = messenger.last_reply
    assert spec["type"] == "flex" and spec["contents"]["type"] == "bubble"
    texts = lc._text_nodes(spec["contents"])
    assert "AIの新モデル発表" in [n["text"] for n in texts]
    assert any("今後の配信の選び方に反映します" in n["text"] for n in texts)
    assert [(b["action"]["label"], b["action"]["data"]) for b in _buttons(spec)] == [
        ("興味あり", f"fb:{key}:good"), ("興味なし", f"fb:{key}:bad")]
    assert all(b["action"]["type"] == "postback" and "displayText" not in b["action"] for b in _buttons(spec))
    lc._spec_to_message(spec)  # SDK の Flex として組み立てられる(送信はしない)
    assert not messenger.pushes  # 返事は reply(無料)だけ
    assert _rows(session) == []  # rate は選択肢を出すだけで記録しない


def test_reimported_same_rank_other_article_is_not_rated(session, messenger):
    """同じ日に取り込み直して同じ rank に別の記事が入ったら、前の配信の評価ボタンは「見つかりませんでした」。
    同じ記事が別の rank に移っただけなら、見出しで引いて新しい行に記録する。"""
    _owner(session)
    old = _item(session, title="取り込み前")
    key = _key(old)
    session.delete(old)
    session.delete(session.get(GenreDigest, old.genre_digest_id))
    session.commit()
    _item(session, title="取り込み後")  # 同じ日・スロット・ジャンル・rank 0 の別の記事

    for data in (f"rate:{key}", f"fb:{key}:good"):
        handle_event(session, messenger, _ev(data))
        assert messenger.last_reply[0]["text"] == FEEDBACK_MISSING_MSG
        assert "配信し直された" in FEEDBACK_MISSING_MSG
    assert _rows(session) == []

    gd = session.exec(select(GenreDigest)).first()
    moved = NewsItem(genre_digest_id=gd.id, genre="AI", importance="small", rank=2, title="取り込み前",
                     summary="要約")
    session.add(moved)
    session.commit()
    handle_event(session, messenger, _ev(f"fb:{key}:good"))
    assert messenger.last_reply[0]["text"] == "興味あり として記録しました"
    (row,) = _rows(session)
    assert (row.newsitem_id, row.title) == (moved.id, "取り込み前")


def test_fb_postback_records_and_overwrites(session, messenger):
    _owner(session)
    it = _item(session)
    key = _key(it)
    handle_event(session, messenger, _ev(f"fb:{key}:good"))
    assert messenger.last_reply[0]["text"] == "興味あり として記録しました"
    (row,) = _rows(session)
    assert (row.newsitem_id, row.user_id, row.verdict) == (it.id, "U1", "good")
    assert (row.genre, row.title, row.digest_date) == ("AI", "AIの新モデル発表", date(2026, 10, 2))
    first_created = row.created_at

    # 同じ人が押し直したら上書き(行は増えない)
    handle_event(session, messenger, _ev(f"fb:{key}:bad"))
    assert messenger.last_reply[0]["text"] == "興味なし として記録しました"
    (row,) = _rows(session)
    assert row.verdict == "bad" and row.created_at == first_created and row.updated_at >= first_created

    # 別の人は別の行
    handle_event(session, messenger, _ev(f"fb:{key}:good", uid="U2"))
    assert sorted((r.user_id, r.verdict) for r in _rows(session)) == [("U1", "bad"), ("U2", "good")]
    assert not messenger.pushes


def test_feedback_without_user_id_counts_as_group_and_creates_no_subscriber(session, messenger):
    _owner(session)
    it = _item(session)
    handle_event(session, messenger, _ev(f"fb:{_key(it)}:good", uid=None))
    (row,) = _rows(session)
    assert row.user_id == "group"
    assert [s.line_user_id for s in session.exec(select(Subscriber)).all()] == ["Uowner"]


def test_feedback_only_from_delivery_groups_or_subscribers(session, messenger):
    """評価を受けるのは、配信先のグループ/ルーム(Subscriber.push_to)か、1:1 で既存の購読者だけ。
    それ以外は返事もせず黙って無視する。"""
    it = _item(session)
    key = _key(it)
    handle_event(session, messenger, _ev(f"fb:{key}:good", target="Cother"))  # 配信先でないグループ
    handle_event(session, messenger, _ev(f"fb:{key}:good", uid=None, target="Cother"))
    handle_event(session, messenger, _ev(f"rate:{key}", uid="Ustranger", target=None))  # 購読者でない 1:1
    handle_event(session, messenger, _ev(f"fb:{key}:good", uid=None, target=None))
    assert messenger.replies == [] and _rows(session) == []
    assert session.exec(select(Subscriber)).all() == []

    _owner(session, push_to="Cgroup", uid="Uowner")
    handle_event(session, messenger, _ev(f"fb:{key}:good", uid="Uowner", target=None))  # 1:1 の購読者
    handle_event(session, messenger, _ev(f"fb:{key}:bad", uid="U9", target="Cgroup"))  # 配信先のグループ
    assert sorted((r.user_id, r.verdict) for r in _rows(session)) == [("U9", "bad"), ("Uowner", "good")]
    handle_event(session, messenger, _ev(f"fb:{key}:bad", uid="Uowner", target="Cother"))
    assert len(_rows(session)) == 2  # 購読者でも、配信先でないグループからは受けない


def test_feedback_one_to_one_requires_onboarded_subscriber(session, messenger):
    """1:1 は Subscriber の行があるだけでは受けない(「友だち追加」だけで離脱した人の行がある)。オンボーディング済みだけ。"""
    it = _item(session)
    key = _key(it)
    session.add(Subscriber(line_user_id="Uhalf", is_onboarded=False))
    session.commit()
    handle_event(session, messenger, _ev(f"fb:{key}:good", uid="Uhalf", target=None))
    handle_event(session, messenger, _ev(f"rate:{key}", uid="Uhalf", target=None))
    assert messenger.replies == [] and _rows(session) == []

    _owner(session, push_to=None, uid="Udone")
    handle_event(session, messenger, _ev(f"fb:{key}:good", uid="Udone", target=None))
    assert [(r.user_id, r.verdict) for r in _rows(session)] == [("Udone", "good")]


def test_feedback_missing_item_and_old_numeric_key(session, messenger):
    _owner(session)
    it = _item(session)
    for data in (f"rate:{it.id}", f"fb:{it.id}:good", "rate:99999", "rate:abc",
                 "rate:20261002:morning:AI:00000000", "fb:20261002:morning:AI:00000000:good",
                 "rate:2026xx02:morning:AI:00000000"):
        handle_event(session, messenger, _ev(data))
        assert messenger.last_reply[0]["text"] == FEEDBACK_MISSING_MSG
    assert _rows(session) == []


def test_feedback_snapshot_survives_item_deletion(session, messenger):
    """取り込み直しで NewsItem が消えても評価(ジャンル・見出しの写し)は好みとして残る。"""
    _owner(session)
    it = _item(session)
    key = _key(it)
    handle_event(session, messenger, _ev(f"fb:{key}:good"))
    session.delete(it)
    session.commit()
    assert feedback.preferences(session)["good"] == [{"genre": "AI", "title": "AIの新モデル発表"}]
    handle_event(session, messenger, _ev(f"rate:{key}"))
    assert messenger.last_reply[0]["text"] == FEEDBACK_MISSING_MSG


def test_invalid_verdict_does_not_record(session, messenger):
    _owner(session)
    it = _item(session)
    handle_event(session, messenger, _ev(f"fb:{_key(it)}:meh"))
    assert _rows(session) == []
    assert messenger.last_reply  # 無反応にしない


def test_record_failure_replies_and_raises(session, messenger, monkeypatch):
    """記録に失敗したら無応答にせず「記録に失敗しました」と返し、例外は webhook 側に任せる(rollback する)。"""
    _owner(session)
    it = _item(session)

    def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(feedback, "record", broken)
    with pytest.raises(RuntimeError):
        handle_event(session, messenger, _ev(f"fb:{_key(it)}:good"))
    assert messenger.last_reply[0]["text"] == FEEDBACK_FAILED_MSG


# ---------------------------------------------------------------- テストの架空記事(モック)

def test_mock_articles_are_never_rated(session, messenger):
    """「テスト」のモック表示には「評価する」を出さず、モックの記事への評価は記録せず、集計にも入れない。"""
    sub = _owner(session, push_to=None, uid="U1")
    sub.is_onboarded = True
    session.add(sub)
    session.commit()
    handle_event(session, messenger, {"kind": "message", "line_user_id": "U1", "display_name": None,
                                      "text": "テスト", "data": "", "reply_token": "tok", "target_id": None})
    specs = messenger.last_reply
    assert len(specs) >= 2 and _rate_datas(specs) == []

    gd = session.exec(select(GenreDigest).where(GenreDigest.digest_date == mockdata.MOCK_DATE)).first()
    mock_item = session.exec(select(NewsItem).where(NewsItem.genre_digest_id == gd.id)).first()
    key = f"{mockdata.MOCK_DATE:%Y%m%d}:{gd.slot}:{gd.genre}:{feedback.title_hash(mock_item.title)}"
    for data in (f"rate:{key}", f"fb:{key}:good"):
        handle_event(session, messenger, _ev(data, target=None))
        assert messenger.last_reply[0]["text"] == FEEDBACK_MOCK_MSG
    assert _rows(session) == []

    # 以前に記録されてしまった架空記事の評価も、好みには入れない
    _fb(session, "架空の記事", "U1", "bad", datetime.now(UTC).replace(tzinfo=None))
    row = _rows(session)[0]
    row.digest_date = mockdata.MOCK_DATE
    session.add(row)
    session.commit()
    _fb(session, "本物の記事", "U1", "good", datetime.now(UTC).replace(tzinfo=None))
    assert feedback.preferences(session) == {"good": [{"genre": "AI", "title": "本物の記事"}], "bad": []}


# ---------------------------------------------------------------- preferences の集計

T0 = datetime(2026, 10, 29, 0, 0)  # naive UTC(DB の入れ方に合わせる)


def _fb(session, title, user, verdict, at, genre="AI", item_id=1):
    session.add(NewsFeedback(newsitem_id=item_id, user_id=user, verdict=verdict, genre=genre,
                             title=title, created_at=at, updated_at=at))
    session.commit()


def test_preferences_empty_has_both_keys(session):
    assert feedback.preferences(session, now=T0.replace(tzinfo=UTC)) == {"good": [], "bad": []}


def test_preferences_majority_tie_newest_and_order(session):
    now = T0.replace(tzinfo=UTC)
    # 2対1で bad
    _fb(session, "多数決", "U1", "good", T0 - timedelta(hours=5))
    _fb(session, "多数決", "U2", "bad", T0 - timedelta(hours=4))
    _fb(session, "多数決", "U3", "bad", T0 - timedelta(hours=3))
    # 1対1 は新しい方(good)
    _fb(session, "同数", "U1", "bad", T0 - timedelta(hours=2), genre="株")
    _fb(session, "同数", "U2", "good", T0 - timedelta(hours=1), genre="株")
    # 1人の古い記録が重なって残っていても、その人の最新だけを数える(good 2票にはならない)
    _fb(session, "一人", "U1", "good", T0 - timedelta(days=2))
    _fb(session, "一人", "U1", "good", T0 - timedelta(days=1, hours=1))
    _fb(session, "一人", "U2", "bad", T0 - timedelta(days=1))
    prefs = feedback.preferences(session, now=now)
    assert prefs == {
        "good": [{"genre": "株", "title": "同数"}],
        "bad": [{"genre": "AI", "title": "多数決"}, {"genre": "AI", "title": "一人"}],
    }


def test_preferences_90_days_and_40_each_newest_first(session):
    now = T0.replace(tzinfo=UTC)
    for k in range(45):
        _fb(session, f"良{k:02d}", "U1", "good", T0 - timedelta(hours=k), item_id=k)
        _fb(session, f"悪{k:02d}", "U1", "bad", T0 - timedelta(hours=k, minutes=30), item_id=100 + k)
    _fb(session, "古い", "U1", "good", T0 - timedelta(days=91))
    _fb(session, "ぎりぎり", "U1", "bad", T0 - timedelta(days=89))
    prefs = feedback.preferences(session, now=now)
    assert [p["title"] for p in prefs["good"]] == [f"良{k:02d}" for k in range(40)]
    assert len(prefs["bad"]) == 40 and prefs["bad"][0]["title"] == "悪00"
    assert all(p["title"] != "古い" for p in prefs["good"])
    assert all(set(p) == {"genre", "title"} for p in prefs["good"] + prefs["bad"])  # summary は入れない


def test_record_reused_item_id_is_a_different_article(session):
    """id が使い回された別の記事(見出しが違う)への評価は、前の記事の評価を上書きしない。"""
    it = _item(session, title="前の記事")
    feedback.record(session, it, "U1", "good")
    it.title = "別の記事"
    session.add(it)
    session.commit()
    feedback.record(session, it, "U1", "bad")
    assert {(r.title, r.verdict) for r in _rows(session)} == {("前の記事", "good"), ("別の記事", "bad")}


# ---------------------------------------------------------------- raw への受け渡し

def test_raw_dump_and_split_keep_preferences():
    prefs = {"good": [{"genre": "AI", "title": "良い記事"}], "bad": [{"genre": "株", "title": "悪い記事"}]}
    raw = {"date": "2026-10-29", "tz": "Asia/Tokyo", "slot": "morning", "market": [], "schedule": [],
           "indicator_results": [], "recent_titles": {"AI": [], "株": []}, "preferences": prefs,
           "genres": {"AI": [{"text": "a"}], "株": [{"text": "b"}]}}
    assert json.loads(pl._dump_raw(raw))["preferences"] == prefs
    parts = pl.split_raw(raw)
    assert parts and all(p["preferences"] == prefs for p in parts)
    assert all(json.loads(pl._dump_raw(p))["preferences"] == prefs for p in parts)
    # 古い raw(preferences なし)を書き直しても、キーは空配列で入る
    old = {k: v for k, v in raw.items() if k != "preferences"}
    assert json.loads(pl._dump_raw(old))["preferences"] == {"good": [], "bad": []}


def test_pipeline_preferences_reads_db(monkeypatch, session):
    import contextlib

    _fb(session, "読む記事", "U1", "good", datetime.now(UTC).replace(tzinfo=None))

    @contextlib.contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "get_session", fake_session)
    assert pl._preferences() == {"good": [{"genre": "AI", "title": "読む記事"}], "bad": []}

    def broken():
        raise RuntimeError("db locked")
    monkeypatch.setattr(pl, "init_db", broken)
    assert pl._preferences() == {"good": [], "bad": []}  # 読めなくても収集は続ける


# ---------------------------------------------------------------- LINE の一覧の「評価する」

def _with_ids(grouped, start=100000):
    """本番に近い桁数(6桁)の id を振る(postback data の長さが容量に効くため)。"""
    n = start
    for items in grouped.values():
        for it in items:
            it.id = n
            n += 1
    return grouped


def _rate_datas(node) -> list[str]:
    return [a["data"] for a in _actions(node) if a["type"] == "postback" and a["data"].startswith("rate:")]


def test_every_row_has_rate_link_next_to_meta():
    """大きいニュース・注目・その他の見出しの全行に、出典・時刻と同じ小ささの「評価する」(rate:<日付:スロット:ジャンル:見出しハッシュ>。
    取り込み直しで rank が変わっても別の記事を評価しない)。
    行全体の「詳細」タップは残し、「評価する」には displayText を付けない(グループに流さない)。"""
    from .test_line_client import _big, _small
    items = _with_ids({"AI": [_big("AI", 0), _small(1, "AI", score=70), _small(2, "AI", score=30)]})
    specs = lc.digest_specs(items, slot="morning", digest_date=D, now=NOW)
    bubble = specs[1]["contents"]["contents"][0]
    keys = [f"rate:20260608:morning:AI:{feedback.title_hash(it.title)}" for it in items["AI"]]
    assert sorted(_rate_datas(bubble)) == sorted(keys)
    rates = [n for n in lc._text_nodes(bubble) if n["text"] == lc.RATE_LABEL]
    assert len(rates) == 3
    for n in rates:
        assert (n["size"], n["color"], n["flex"]) == ("xxs", lc.META_COLOR, 0)
        assert "displayText" not in n["action"]
    rows = [c for c in bubble["body"]["contents"] if c.get("type") == "box" and c.get("action")]
    assert [r["action"]["data"] for r in rows] == ["detail:20260608:morning:AI:1",
                                                   "detail:20260608:morning:AI:2"]
    assert all(_rate_datas(r) == [keys[k + 1]] for k, r in enumerate(rows))
    # 出典も時刻も無い記事でも「評価する」は出る。日付・スロットが無い表示(モック)には出さない(架空の記事なので)
    bare = _with_ids({"AI": [lc.NewsItem(genre="AI", importance="big", rank=0, title="出典なし")]}, 7)
    assert _rate_datas(lc.digest_specs(bare, slot="morning", digest_date=D, now=NOW)[1]) == [
        f"rate:20260608:morning:AI:{feedback.title_hash('出典なし')}"]
    assert _rate_datas(lc.digest_specs(bare, now=NOW)[1]) == []


def test_rate_links_stay_within_line_limits_on_heavy_days():
    """重い日(約130〜160件)でも 5通・12枚・48000B・28000B を超えない。入りきる日は出た記事すべてに「評価する」が付き、
    「評価する」のせいで省略が出る日は段を落として組み直す(省略は最も多く載る段と同じ件数。評価のために記事を削らない)。"""
    g5 = _bulk(["AI", "株", "暗号資産", "テクノロジー", "話題"], 32, summary_len=100)
    for grouped, expect_all in ((_heavy(), False), (g5, True)):
        _with_ids(grouped)
        specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                                schedule=_HEAVY_SCHEDULE, now=NOW)
        _check_limits_and_coverage(grouped, specs)
        shown = _check_accounting(grouped, specs)
        rates = [d for car in _carousels(specs) for d in _rate_datas(car)]
        if expect_all:
            assert len(rates) == len(shown) == len(set(rates))
            assert "評価する" in _texts(_carousels(specs))
        else:
            assert len(rates) < len(shown)  # 全行には付かない(付けると省略が増える)

        def count(level):
            cars = lc._genre_carousels(grouped, D, "morning", NOW, lc.MAX_MESSAGES - 1, level)
            return sum(e[1] for car in cars for e in car)
        total = sum(len(v) for v in grouped.values())
        counts = [count(lc.RATE_ALL), count(lc.RATE_TOP), count(lc.RATE_NONE)]
        assert (counts[0] < max(counts)) is not expect_all  # 重い日は全行に評価を付けると載る件数が減る
        assert _omitted(specs) == total - max(counts)  # 最も多く載る段と同じ省略件数

def _top_scored(n: int):
    """3ジャンル × n 件。先頭3件が big、big を除く先頭9件が注目(score >= 50)、残りがその他の見出し。"""
    grouped = _with_ids(_bulk(["AI", "株", "暗号資産"], n, summary_len=60))
    for items in grouped.values():
        for i, it in enumerate(items):
            it.score = 90 - i if i < 12 else 30
    return grouped


def test_middle_rate_tier_keeps_rate_links_on_big_and_notable_when_it_omits_nothing_more():
    """全行に評価を付けると省略が出るが、big と notable の行だけなら省略が増えない日は、
    評価なしに落とさず big／notable の行だけに「評価する」を残す。"""
    grouped = _top_scored(65)
    total = sum(len(v) for v in grouped.values())

    def count(level):
        cars = lc._genre_carousels(grouped, D, "morning", NOW, lc.MAX_MESSAGES - 1, level)
        return sum(e[1] for car in cars for e in car)
    assert count(lc.RATE_ALL) < count(lc.RATE_TOP) == count(lc.RATE_NONE) == total  # この日は中間段で全件載る

    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                            schedule=_HEAVY_SCHEDULE, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    shown = _check_accounting(grouped, specs)
    assert _omitted(specs) == 0
    rates = [d for car in _carousels(specs) for d in _rate_datas(car)]
    assert len(rates) == len(set(rates)) == 3 * 12  # big 3 + notable 9 の各ジャンル12件だけ。その他の見出しには付かない
    assert 0 < len(rates) < len(shown)


# ---------------------------------------------------------------- webhook(グループの userId なし)

def _hook(monkeypatch):
    import contextlib

    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from xnewsbot.api import line_webhook
    from xnewsbot.config import Settings

    from .test_webhook import SECRET, FakeLineMessenger
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    sess = Session(engine)

    @contextlib.contextmanager
    def fake_session():
        yield sess

    dummy = Settings(line_channel_secret=SECRET, line_channel_access_token="tok")
    monkeypatch.setattr(line_webhook, "get_settings", lambda: dummy)
    monkeypatch.setattr(line_webhook, "get_session", fake_session)
    monkeypatch.setattr(line_webhook, "LineMessenger", FakeLineMessenger)
    monkeypatch.setattr(line_webhook, "make_deliver_now", lambda settings: (lambda sub: None))
    return sess


def _postback_body(data: str, source: dict) -> str:
    return json.dumps({"destination": "x", "events": [{
        "type": "postback", "postback": {"data": data}, "mode": "active", "timestamp": 1,
        "source": source, "replyToken": "tok", "webhookEventId": "e1",
        "deliveryContext": {"isRedelivery": False}}]})


def test_webhook_group_postback_without_user_id_is_recorded(monkeypatch):
    from fastapi.testclient import TestClient

    from xnewsbot.api.main import app

    from .test_webhook import FakeLineMessenger, _sign
    sess = _hook(monkeypatch)
    _owner(sess, push_to="Cg1")  # 配信先のグループからの評価だけ受ける
    it = _item(sess)
    with TestClient(app) as c:
        body = _postback_body(f"fb:{_key(it)}:good", {"type": "group", "groupId": "Cg1"})
        assert c.post("/line/callback", content=body, headers={"X-Line-Signature": _sign(body)}).status_code == 200
        assert FakeLineMessenger.last.replies[0][1][0]["text"] == "興味あり として記録しました"
        # 評価以外の postback は、userId が無ければ今までどおり無視する
        body = _postback_body("detail:20261002:morning:AI:0", {"type": "group", "groupId": "Cg1"})
        c.post("/line/callback", content=body, headers={"X-Line-Signature": _sign(body)})
        assert FakeLineMessenger.last.replies == []
    (row,) = sess.exec(select(NewsFeedback)).all()
    assert row.user_id == "group" and row.verdict == "good"
    assert [s.line_user_id for s in sess.exec(select(Subscriber)).all()] == ["Uowner"]  # 購読者は増やさない


def test_webhook_rolls_back_failed_event_and_answers(monkeypatch):
    """記録に失敗したイベントは rollback し(同じ session の次のイベントを巻き込まない)、本人には失敗を返す。"""
    from fastapi.testclient import TestClient

    from xnewsbot.api.main import app

    from .test_webhook import FakeLineMessenger, _sign
    sess = _hook(monkeypatch)
    _owner(sess, push_to="Cg1")
    it = _item(sess)
    calls = []
    real_rollback = sess.rollback
    monkeypatch.setattr(sess, "rollback", lambda: (calls.append(1), real_rollback()))

    def broken(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(feedback, "record", broken)
    with TestClient(app) as c:
        body = _postback_body(f"fb:{_key(it)}:good", {"type": "group", "groupId": "Cg1", "userId": "U5"})
        assert c.post("/line/callback", content=body, headers={"X-Line-Signature": _sign(body)}).status_code == 200
        assert FakeLineMessenger.last.replies[0][1][0]["text"] == FEEDBACK_FAILED_MSG
    assert calls == [1]
    assert sess.exec(select(NewsFeedback)).all() == []


# ---------------------------------------------------------------- 管理画面(Web)

def test_web_feedback_records_overwrites_and_shows_pressed(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from xnewsbot.admin.main import app

    from .test_admin import AUTH
    from .test_admin import wired as admin_wired
    w = admin_wired.__wrapped__(monkeypatch, tmp_path)
    it = _item(w.session, title="管理画面で評価する記事", day=date(2026, 6, 15))
    with TestClient(app) as c:
        page = c.get("/?date_str=2026-06-15&slot=morning", auth=AUTH).text
        assert f'data-feedback-id="{it.id}"' in page
        h = feedback.title_hash(it.title)
        assert f'data-feedback-hash="{h}"' in page
        assert 'data-verdict="good" aria-pressed="false"' in page

        r = c.post(f"/feedback/{it.id}/{h}/good", auth=AUTH)
        assert r.status_code == 200 and r.json() == {"verdict": "good"}
        assert 'data-verdict="good" aria-pressed="true"' in c.get("/?date_str=2026-06-15&slot=morning", auth=AUTH).text

        assert c.post(f"/feedback/{it.id}/{h}/bad", auth=AUTH).json() == {"verdict": "bad"}
        page = c.get("/?date_str=2026-06-15&slot=morning", auth=AUTH).text
        assert 'data-verdict="bad" aria-pressed="true"' in page
        assert 'data-verdict="good" aria-pressed="false"' in page

        assert c.post(f"/feedback/99999/{h}/good", auth=AUTH).status_code == 404
        assert c.post(f"/feedback/{it.id}/{h}/meh", auth=AUTH).status_code == 400
        # 開いたままのページの後で取り込み直され、同じ id が別の記事になっていたら記録しない
        r = c.post(f"/feedback/{it.id}/00000000/good", auth=AUTH)
        assert r.status_code == 409 and "入れ替わりました" in r.json()["detail"]
    with TestClient(app) as c:  # Basic 認証後の cookie を持たない別の端末
        assert c.post(f"/feedback/{it.id}/{h}/good").status_code == 401
    (row,) = w.session.exec(select(NewsFeedback)).all()
    assert (row.user_id, row.verdict, row.title) == ("web", "bad", "管理画面で評価する記事")
