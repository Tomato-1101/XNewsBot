"""監視アカウント(xnewsbot/watch.py)と pipeline への組み込み。X の API は呼ばない(fetch_with_retry をモック)。"""

from __future__ import annotations

import contextlib
import importlib.util
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from xnewsbot import genres, watch, xclient

_PL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("pipeline", _PL_PATH)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)

WG = "監視アカウント"
NOW = datetime(2026, 10, 2, 22, 15, tzinfo=timezone.utc)   # JST 10/03 07:15
DAY = date(2026, 10, 3)


def _created(dt: datetime) -> str:
    return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")


def _tw(tid: str, hours_ago: float, text: str = "", user: str = "ClaudeDevs", **kw) -> dict:
    return {"id": tid, "text": text or f"投稿{tid}", "url": f"https://x.com/{user}/status/{tid}",
            "createdAt": _created(NOW - timedelta(hours=hours_ago)), "viewCount": 100, "likeCount": 5,
            "author": {"userName": user, "id": "u1", "followers": 1000}, **kw}


@pytest.fixture
def state_file(monkeypatch, tmp_path):
    p = tmp_path / "watch_state.json"
    monkeypatch.setattr(watch, "state_path", lambda: str(p))
    monkeypatch.setattr(pl.watch, "state_path", lambda: str(p))
    return p


# --- ジャンル定義 ---

def test_watch_genre_in_genres_toml():
    assert genres.WATCH_KEYS == [WG]
    assert genres.is_watch(WG) and not genres.is_watch("AI")
    g = genres.GENRES[WG]
    assert g["label"] == WG and g["keywords"] == [] and g["feeds"] == [] and g["selectable"]


# --- ハンドル ---

@pytest.mark.parametrize("text,want", [("ClaudeDevs", "ClaudeDevs"), (" @ClaudeDevs ", "ClaudeDevs"),
                                       ("a_1", "a_1"), ("x" * 15, "x" * 15), ("x" * 16, None),
                                       ("", None), ("@", None), ("a-b", None), ("@@a", None)])
def test_normalize_handle(text, want):
    assert watch.normalize_handle(text) == want


# --- 取得期間 ---

def test_window_without_state_is_24h():
    assert watch.window(DAY, NOW, {}) == (NOW - timedelta(hours=24), NOW)


def test_window_new_day_starts_at_last_until():
    st = {"date": "2026-10-02", "since": "2026-09-30T22:15:00Z", "last_until": "2026-10-01T22:16:00Z"}
    assert watch.window(DAY, NOW, st)[0] == datetime(2026, 10, 1, 22, 16, tzinfo=timezone.utc)


def test_window_same_day_rerun_starts_at_that_days_since():
    """同じ配信日の再収集(今すぐ更新など)は、その日の窓の始まりから(朝に載せた分を消さない)。"""
    st = {"date": "2026-10-03", "since": "2026-10-01T22:16:00Z", "last_until": "2026-10-02T22:00:00Z"}
    assert watch.window(DAY, NOW, st)[0] == datetime(2026, 10, 1, 22, 16, tzinfo=timezone.utc)


def test_window_caps_at_7_days():
    st = {"date": "2026-09-01", "since": "x", "last_until": "2026-09-01T00:00:00Z"}
    assert watch.window(DAY, NOW, st)[0] == NOW - timedelta(days=7)


def test_window_broken_state_falls_back_to_24h():
    assert watch.window(DAY, NOW, {"date": "2026-10-03", "since": "壊れ", "last_until": None})[0] \
        == NOW - timedelta(hours=24)


def test_state_roundtrip_and_broken_file(state_file, capsys):
    assert watch.load_state() == {}
    watch.save_state(DAY, "2026-10-01T22:16:00Z", "2026-10-02T22:15:00Z")
    assert watch.load_state() == {"date": "2026-10-03", "since": "2026-10-01T22:16:00Z",
                                  "last_until": "2026-10-02T22:15:00Z"}
    state_file.write_text("{壊れ", encoding="utf-8")
    assert watch.load_state() == {}
    assert "watch_state.json を読めず" in capsys.readouterr().err


# --- 取得 ---

def test_fetch_account_query_and_filters(monkeypatch):
    calls = []
    tweets = [
        _tw("1", 2),
        _tw("2", 30),                                   # 期間外(古い)
        _tw("3", 3, text="RT @other: リポスト"),           # リポスト
        _tw("4", 4, isReply=True, inReplyToUsername="someone"),     # 他人への返信
        _tw("5", 5, isReply=True, inReplyToUsername="claudedevs"),  # 自分のスレッドの続き
        _tw("6", 6, isReply=True, inReplyToUsername="", inReplyToUserId="u9"),  # 他人(ID で判定)
        _tw("7", 7, isReply=True, inReplyToUsername="", inReplyToUserId=""),    # 判定不能は残す
        _tw("1", 2),                                    # ページまたぎの重複
        _tw("8", 8, quoted_tweet={"id": "q"}),         # 引用は残す
    ]

    def fake(query, query_type, max_tweets, keys):
        calls.append((query, query_type, max_tweets, keys))
        return [dict(t) for t in tweets]
    monkeypatch.setattr(xclient, "fetch_with_retry", fake)
    got = watch.fetch_account("ClaudeDevs", NOW - timedelta(hours=24), NOW, ["k"])
    assert calls == [("from:ClaudeDevs -filter:retweets within_time:24h", "Latest", 100, ["k"])]
    assert [t["id"] for t in got] == ["1", "5", "7", "8"]
    assert all(t["_official"] for t in got)


def test_fetch_account_rounds_hours_up(monkeypatch):
    calls = []
    monkeypatch.setattr(xclient, "fetch_with_retry", lambda q, *a: calls.append(q) or [])
    watch.fetch_account("a", NOW - timedelta(hours=25, minutes=1), NOW, ["k"])
    assert calls == ["from:a -filter:retweets within_time:26h"]


def test_collect_sorts_oldest_first_and_continues_after_failure(monkeypatch, capsys):
    def fake(query, query_type, max_tweets, keys):
        if "from:bad " in query:
            raise xclient.XClientRetryable("timeout")
        if "from:A " in query:
            return [_tw("a1", 1, user="A"), _tw("a2", 10, user="A")]
        return [_tw("b1", 5, user="B")]
    monkeypatch.setattr(xclient, "fetch_with_retry", fake)
    got, ok = watch.collect(["A", "bad", "B"], NOW - timedelta(hours=24), NOW, ["k"])
    assert [t["id"] for t in got] == ["a2", "b1", "a1"]
    assert ok is False
    assert "@bad の取得失敗" in capsys.readouterr().err
    assert watch.collect(["A"], NOW - timedelta(hours=24), NOW, ["k"])[1] is True


# --- pipeline collect ---

def _collect(monkeypatch, tmp_path, fetch, genres_arg=f"株,{WG}"):
    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace(default_tz="Asia/Tokyo"))
    monkeypatch.setattr(pl.xclient, "load_keys", lambda settings: ["k"])
    monkeypatch.setattr(pl.xclient, "fetch_balance", lambda k: None)
    monkeypatch.setattr(pl.xclient, "fetch_with_retry", fetch)
    monkeypatch.setattr(pl.xclient, "collect", lambda g, settings=None, keys=None: [])
    monkeypatch.setattr(pl, "_newsfeed_candidates", lambda g, settings: [
        {"source": "news", "text": f"{g}見出し", "url": f"https://n/{g}"}])
    monkeypatch.setattr(pl.articles, "enrich_bodies", lambda cands: None)
    monkeypatch.setattr(pl.market, "fetch_market", lambda: [])
    monkeypatch.setattr(pl.schedule, "fetch", lambda: {"schedule": [], "results": []})
    monkeypatch.setattr(pl, "_recent_titles", lambda genres, day: {g: [] for g in genres})
    monkeypatch.setattr(pl.watch, "enabled_handles", lambda session: ["ClaudeDevs"])
    out = tmp_path / "raw.json"
    pl.cmd_collect(SimpleNamespace(user=None, due=False, genres=genres_arg, slot="morning", out=str(out)))
    return json.loads(out.read_text(encoding="utf-8"))


def test_collect_watch_genre_candidates(monkeypatch, tmp_path, state_file):
    now = datetime.now(timezone.utc)   # collect は実時刻で期間を決めるので、投稿も実時刻基準で作る
    many = [{**_tw(str(i), 0), "createdAt": _created(now - timedelta(hours=1 + i * 0.1))}
            for i in range(70)]  # _cap_x(50件)の制限はかけない
    raw = _collect(monkeypatch, tmp_path, lambda q, t, m, k: [dict(x) for x in many])
    cands = raw["genres"][WG]
    assert len(cands) == 70
    assert cands[0]["text"] == "投稿69" and cands[-1]["text"] == "投稿0"  # 古い順
    c = cands[0]
    assert {k for k in c if k != "i"} == {"text", "viewCount", "likeCount", "url", "createdAt",
                                          "author", "media", "kind", "official"}
    assert c["official"] is True and c["author"]["userName"] == "ClaudeDevs" and c["kind"] == "x"
    assert c["media"] == "@ClaudeDevs"
    assert raw["genres"]["株"][0]["text"] == "株見出し"    # 他のジャンルは従来どおり
    assert raw["watch"]["since"] < raw["watch"]["until"]


def test_collect_watch_failure_does_not_stop_collect(monkeypatch, tmp_path, state_file):
    def boom(q, t, m, k):
        raise xclient.XClientError("dead", status=402)
    raw = _collect(monkeypatch, tmp_path, boom)
    assert raw["genres"][WG] == []
    assert raw["genres"]["株"]                    # 他のジャンルは集まっている
    assert raw["watch"] is None                   # 状態を進めない(次回に同じ範囲を取り直す)


def test_collect_without_watch_genre_does_not_touch_watch(monkeypatch, tmp_path, state_file):
    def never(*a):
        raise AssertionError("呼ばれない")
    monkeypatch.setattr(pl.watch, "load_state", never)
    raw = _collect(monkeypatch, tmp_path, never, genres_arg="株")
    assert raw["watch"] is None and WG not in raw["genres"]


# --- pipeline ingest(状態は取り込み成功時だけ進む) ---

def _ingest(monkeypatch, tmp_path, session, raw_watch, cands, curated):
    raw = {"date": "2026-10-03", "slot": "morning", "market": [], "watch": raw_watch,
           "genres": {"株": [{"text": "s"}], WG: cands}}
    (tmp_path / "raw.json").write_text(json.dumps(raw), encoding="utf-8")
    (tmp_path / "cur.json").write_text(json.dumps({"genres": curated}), encoding="utf-8")

    @contextlib.contextmanager
    def fake_session():
        yield session
    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "get_session", fake_session)
    pl.cmd_ingest(SimpleNamespace(raw=str(tmp_path / "raw.json"), curated=str(tmp_path / "cur.json"),
                                  date=None, slot=None))


W = {"since": "2026-10-01T22:16:00Z", "until": "2026-10-02T22:15:00Z"}
ITEM = [{"title": "Claude Code の新機能", "summary": "s", "importance": "small", "source_idxs": [0]}]


def test_ingest_advances_state(monkeypatch, tmp_path, session, state_file):
    _ingest(monkeypatch, tmp_path, session, W, [{"text": "投稿"}], {"株": [], WG: ITEM})
    assert json.loads(state_file.read_text()) == {"date": "2026-10-03", "since": W["since"],
                                                  "last_until": W["until"]}


def test_ingest_advances_state_when_no_posts(monkeypatch, tmp_path, session, state_file):
    _ingest(monkeypatch, tmp_path, session, W, [], {"株": []})
    assert state_file.exists()


def test_ingest_keeps_state_when_fetch_failed(monkeypatch, tmp_path, session, state_file):
    _ingest(monkeypatch, tmp_path, session, None, [], {"株": []})
    assert not state_file.exists()


def test_ingest_keeps_state_when_curation_missing_watch_genre(monkeypatch, tmp_path, session, state_file, capsys):
    _ingest(monkeypatch, tmp_path, session, W, [{"text": "投稿"}], {"株": []})
    assert not state_file.exists()
    assert "次回も同じ範囲から取り直します" in capsys.readouterr().err


def test_ingest_advances_state_when_curation_judged_all_posts_empty(monkeypatch, tmp_path, session, state_file):
    # あいさつだけの日など、キュレーションが正常に「載せるもの無し」と判断した場合は進める(窓が伸び続けない)
    _ingest(monkeypatch, tmp_path, session, W, [{"text": "おはよう"}], {"株": [], WG: []})
    assert json.loads(state_file.read_text())["last_until"] == W["until"]


def test_ingest_same_day_rerun_keeps_state(monkeypatch, tmp_path, session, state_file):
    # 同じ配信日の再取り込み(今すぐ更新など)で last_until を進めると、朝の配信以降の投稿が翌朝に届かなくなる
    first = {"date": "2026-10-03", "since": W["since"], "last_until": W["until"]}
    state_file.write_text(json.dumps(first))
    later = {"since": W["since"], "until": "2026-10-03T03:00:00Z"}
    _ingest(monkeypatch, tmp_path, session, later, [{"text": "投稿"}], {"株": [], WG: ITEM})
    assert json.loads(state_file.read_text()) == first


def test_ingest_state_save_failure_does_not_fail_ingest(monkeypatch, tmp_path, session, state_file, capsys):
    def boom(*a):
        raise OSError("disk full")
    monkeypatch.setattr(pl.watch, "save_state", boom)
    _ingest(monkeypatch, tmp_path, session, W, [{"text": "投稿"}], {"株": [], WG: ITEM})
    assert "保存できず続行" in capsys.readouterr().err


# --- split(監視アカウントは独立した組) ---

def _raw(gs: dict) -> dict:
    return {"date": "2026-10-03", "tz": "Asia/Tokyo", "slot": "morning", "market": [], "schedule": [],
            "indicator_results": [], "recent_titles": {g: [] for g in gs}, "genres": gs}


def test_split_raw_watch_is_own_group_and_skipped_when_empty():
    gs = {"AI": [{"text": "a"}], "暗号資産": [{"text": "c"}], "テクノロジー": [{"text": "t"}], WG: [{"text": "w"}]}
    parts = pl.split_raw(_raw(gs))
    assert [list(p["genres"]) for p in parts] == [["AI", "暗号資産"], ["テクノロジー"], [WG]]
    assert [list(p["genres"]) for p in pl.split_raw(_raw(gs), whole=True)] == [["AI", "暗号資産", "テクノロジー"], [WG]]
    gs[WG] = []
    assert [list(p["genres"]) for p in pl.split_raw(_raw(gs), whole=True)] == [["AI", "暗号資産", "テクノロジー"]]
    assert [list(p["genres"]) for p in pl.split_raw(_raw({WG: [{"text": "w"}]}), whole=True)] == [[WG]]


def test_cmd_split_small_raw_with_watch_posts_splits_off_watch(tmp_path, capsys):
    p = tmp_path / "xnews_morning_raw.json"
    p.write_text(pl._dump_raw(_raw({"AI": [{"text": "a"}], WG: [{"text": "w"}]})), encoding="utf-8")
    pl.cmd_split(SimpleNamespace(raw=str(p)))
    outs = capsys.readouterr().out.splitlines()
    assert len(outs) == 2
    assert list(json.loads(Path(outs[1]).read_text())["genres"]) == [WG]
    assert json.loads(Path(outs[1]).read_text())["genres"][WG][0]["i"] == 0


def test_cmd_split_small_raw_without_watch_posts_is_not_split(tmp_path, capsys):
    p = tmp_path / "xnews_morning_raw.json"
    p.write_text(pl._dump_raw(_raw({"AI": [{"text": "a"}], WG: []})), encoding="utf-8")
    pl.cmd_split(SimpleNamespace(raw=str(p)))
    assert capsys.readouterr().out.splitlines() == [str(p)]


def test_dump_raw_has_watch_window():
    raw = _raw({"AI": []})
    raw["watch"] = W
    assert json.loads(pl._dump_raw(raw))["watch"] == W
    assert json.loads(pl._dump_raw(_raw({"AI": []})))["watch"] is None


def test_ingested_watch_digest_roundtrip(monkeypatch, tmp_path, session, state_file):
    """監視アカウントのジャンルも通常のジャンルと同じく DB に入る(source_idxs で出典を引ける)。"""
    from xnewsbot import digest
    _ingest(monkeypatch, tmp_path, session, W, [{"text": "投稿", "url": "https://x.com/ClaudeDevs/status/1",
                                                 "author": {"userName": "ClaudeDevs"}}],
            {"株": [], WG: ITEM})
    d = digest.get_genre_digest(session, WG, DAY, "morning")
    items = digest.items_of_digest(session, d.id)
    assert [it.title for it in items] == ["Claude Code の新機能"]
    assert items[0].source_urls == ["https://x.com/ClaudeDevs/status/1"]
