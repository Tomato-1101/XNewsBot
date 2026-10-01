"""収集の耐障害性・取り込み保護・空配信回避の回帰テスト。

「今すぐ配信」で世界情勢以外が消えた障害(収集タイムアウト→無リトライ→0件、
さらに空取り込みが定刻の良いダイジェストを破壊)の再発防止。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from xnewsbot import digest, scheduler, xclient
from xnewsbot.curator import parse_curated
from xnewsbot.models import Subscriber

from .conftest import curated_items, make_tweets

JST = ZoneInfo("Asia/Tokyo")
D = date(2026, 6, 8)


# ----------------------------------------------------------------- 収集リトライ

def test_fetch_with_retry_recovers_from_timeout(monkeypatch):
    """1回目がタイムアウト(再試行対象)でも、再試行して成功すれば結果を返す。"""
    calls = {"n": 0}

    def fake_request(query, query_type, cursor, key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise xclient.XClientRetryable("read timeout")
        return {"tweets": [{"id": "1", "viewCount": 5}], "has_next_page": False}

    monkeypatch.setattr(xclient, "_request", fake_request)
    monkeypatch.setattr(xclient.time, "sleep", lambda *_: None)
    out = xclient.fetch_with_retry("q", "Top", 10, "k", retries=3)
    assert len(out) == 1
    assert calls["n"] == 2  # 1回失敗 → 2回目で成功


def test_fetch_with_retry_raises_after_exhaustion(monkeypatch):
    """再試行を使い切っても一時的エラーが続くなら例外を投げる(呼び出し側が空扱いにする)。"""
    monkeypatch.setattr(xclient, "_request",
                        lambda *a, **k: (_ for _ in ()).throw(xclient.XClientRetryable("timeout")))
    monkeypatch.setattr(xclient.time, "sleep", lambda *_: None)
    with pytest.raises(xclient.XClientRetryable):
        xclient.fetch_with_retry("q", "Top", 10, "k", retries=3)


def test_fetch_does_not_retry_permanent_error(monkeypatch):
    """恒久エラー(4xx 等の非 Retryable)は再試行せず即送出する(無駄な待ちをしない)。"""
    calls = {"n": 0}

    def fake_request(*a, **k):
        calls["n"] += 1
        raise xclient.XClientError("HTTP 401 Unauthorized")

    monkeypatch.setattr(xclient, "_request", fake_request)
    monkeypatch.setattr(xclient.time, "sleep", lambda *_: None)
    with pytest.raises(xclient.XClientError):
        xclient.fetch_with_retry("q", "Top", 10, "k", retries=3)
    assert calls["n"] == 1


# ----------------------------------------------------------- 取り込みの破壊防止

def test_ingest_empty_keeps_existing(session):
    """既存ダイジェストがあるとき、空の取り込み(収集失敗)で上書き破壊しない。"""
    items = parse_curated(curated_items(big=1, small=1))
    d1 = digest.ingest_curated(session, "AI", D, "morning", items, make_tweets(3))
    assert len(digest.items_of_digest(session, d1.id)) == 2

    d2 = digest.ingest_curated(session, "AI", D, "morning", [], [])  # 収集失敗を模した空
    assert d2.id == d1.id
    assert len(digest.items_of_digest(session, d2.id)) == 2  # 既存が保持されている


def test_ingest_empty_without_existing_creates_empty(session):
    """既存が無ければ空ダイジェストを作る(「処理済み・該当なし」=揃い判定が完了になる)。"""
    d = digest.ingest_curated(session, "株", D, "morning", [], [])
    assert digest.items_of_digest(session, d.id) == []
    assert digest.missing_genres(session, ["株"], D, "morning") == []


def test_ingest_sets_multi_genre_tags(session):
    """横断話題は主ジャンルを含め、表示順(GENRE_KEYS)で整列した genres タグを持つ。"""
    from xnewsbot.curator import CuratedItem
    ci = CuratedItem(title="半導体大手が決算", summary="s", importance="big", score=80,
                     genres=["テクノロジー", "AI", "不正なキー"], source_idxs=[0])
    d = digest.ingest_curated(session, "株", D, "morning", [ci], make_tweets(2))
    item = digest.items_of_digest(session, d.id)[0]
    # 主ジャンル(株)を含め、未知キーは除外、GENRE_KEYS順(AI→株→テクノロジー)で整列
    assert item.genres == ["AI", "株", "テクノロジー"]


def test_ingest_nonempty_replaces(session):
    """中身がある取り込みは従来どおり置き換える(追記でなく置換・最新で更新)。"""
    from sqlmodel import select

    from xnewsbot.models import GenreDigest
    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(big=1, small=1)), make_tweets(3))
    d2 = digest.ingest_curated(session, "AI", D, "morning",
                               parse_curated(curated_items(big=1, small=2)), make_tweets(4))
    # 同一(日×slot×ジャンル)のダイジェストは1つだけ・中身は新しい方(3件)で置き換わる
    assert len(session.exec(select(GenreDigest).where(GenreDigest.genre == "AI")).all()) == 1
    assert len(digest.items_of_digest(session, d2.id)) == 3


# ------------------------------------------------------------- 空配信を送らない

def _onboarded(**kw) -> Subscriber:
    base = dict(line_user_id="U1", enabled_genres=["AI"], is_onboarded=True)
    base.update(kw)
    return Subscriber(**base)


def test_deliver_skip_if_empty(session, messenger):
    """当日分が空なら定刻配信(skip_if_empty)は push せず配信済みにもしない(次回に委ねる)。"""
    sub = _onboarded()
    specs = scheduler.deliver_to_subscriber(
        session, sub, "morning", messenger=messenger,
        now_local=datetime(2026, 6, 8, 8, 0, tzinfo=JST), skip_if_empty=True,
    )
    assert specs == []
    assert messenger.pushes == []
    assert sub.last_morning_on is None  # 配信済みにしていない


def test_deliver_pushes_when_present(session, messenger):
    """中身があれば push し、配信済みに記録する。"""
    digest.ingest_curated(session, "AI", D, "morning",
                          parse_curated(curated_items(big=1, small=1)), make_tweets(3))
    sub = _onboarded()
    specs = scheduler.deliver_to_subscriber(
        session, sub, "morning", messenger=messenger,
        now_local=datetime(2026, 6, 8, 8, 0, tzinfo=JST),
        digest_date=D, skip_if_empty=True,
    )
    assert specs and messenger.pushes
    assert sub.last_morning_on == D


# ----------------------------------------------------------------- 今日の予定の保存(マージ)

def _ev(name, at, **kw):
    return {"at": at, "time_label": "", "kind": "policy", "country": "US", "name": name,
            "forecast": "", "previous": "", "result": "", "importance": 3, **kw}


def test_save_schedule_merges_and_keeps_missing(session):
    """再収集で一部の取得元が失敗しても、新しい結果に無い既存の予定(FOMC)は残る。"""
    fomc = _ev("FOMC 政策金利", "2026-06-09T03:00:00+09:00", importance=5)
    nke = _ev("Nike（NKE）決算", None, kind="earnings")
    digest.save_schedule(session, D, "morning", [fomc, nke])
    nke2 = {**nke, "result": "増収"}
    newer = _ev("米 雇用統計", "2026-06-08T21:30:00+09:00", kind="indicator")
    digest.save_schedule(session, D, "morning", [nke2, newer])  # FOMC の取得元が失敗した再収集
    got = digest.get_schedule(session, D, "morning")
    assert got == [fomc, nke2, newer]  # FOMC は残り、同じ予定は新しい値に、新規は追加


def test_save_schedule_empty_keeps_existing(session):
    fomc = _ev("FOMC 政策金利", "2026-06-09T03:00:00+09:00")
    digest.save_schedule(session, D, "morning", [fomc])
    digest.save_schedule(session, D, "morning", [])
    assert digest.get_schedule(session, D, "morning") == [fomc]
