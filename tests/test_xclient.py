"""収集クエリ(言語フィルタ)と採用条件(いいね OR 表示回数)、複数キーのフォールバックの回帰テスト。"""

from __future__ import annotations

import pytest

from xnewsbot import xclient


class _Settings:
    collect_hours = 24
    collect_max_tweets = 60
    collect_min_faves = 200
    collect_min_views_floor = 20000


def test_collect_omits_lang_for_any(monkeypatch):
    """lang="any" のジャンル(特大)は lang: フィルタを付けない(英語の一次情報も拾える)。"""
    cap = {}
    monkeypatch.setattr(xclient, "fetch_with_retry",
                        lambda q, qt, mx, keys: (cap.__setitem__("q", q), [])[1])
    xclient.collect("特大", settings=_Settings(), keys=["k"])
    assert "lang:" not in cap["q"]


def test_collect_includes_lang_ja(monkeypatch):
    """lang 既定("ja")のジャンル(RPA)は lang:ja を付ける。"""
    cap = {}
    monkeypatch.setattr(xclient, "fetch_with_retry",
                        lambda q, qt, mx, keys: (cap.__setitem__("q", q), [])[1])
    xclient.collect("RPA", settings=_Settings(), keys=["k"])
    assert "lang:ja" in cap["q"]


def test_collect_keeps_high_view_even_if_low_likes(monkeypatch):
    """いいねが下限未満でも、表示回数が多い速報は採用する(いいね OR 表示回数)。"""
    tweets = [
        {"viewCount": 50000, "likeCount": 1, "text": "速報A", "createdAt": ""},   # 高view低like
        {"viewCount": 10, "likeCount": 1, "text": "雑談B", "createdAt": ""},        # 両方低い → 除外
        {"viewCount": 100, "likeCount": 500, "text": "人気C", "createdAt": ""},     # 高like → 採用
    ]
    monkeypatch.setattr(xclient, "fetch_with_retry", lambda *a, **k: [dict(t) for t in tweets])
    out = xclient.collect("特大", settings=_Settings(), keys=["k"])  # 特大 min_faves=300
    texts = {t["text"] for t in out}
    assert "速報A" in texts and "人気C" in texts
    assert "雑談B" not in texts


# --- 複数キーの優先度フォールバック ---

def test_failover_to_next_key_on_permanent_error(monkeypatch):
    """上位キーが恒久エラー(4xx)なら次の優先度キーへフォールバックして成功する。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        if key == "k1":
            raise xclient.XClientError("k1 dead (4xx)")
        return [{"viewCount": 1, "text": "ok", "likeCount": 0, "createdAt": ""}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    out = xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"])
    assert [t["text"] for t in out] == ["ok"]
    assert calls == ["k1", "k2"]  # k1 で失敗 → k2 へ


def test_failover_on_retryable_exhausted(monkeypatch):
    """上位キーが一時エラーで再試行を尽くしたら次キーへフォールバックする。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        if key == "k1":
            raise xclient.XClientRetryable("k1 timeout")
        return [{"viewCount": 1, "text": "ok", "likeCount": 0, "createdAt": ""}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    out = xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"], retries=1)
    assert [t["text"] for t in out] == ["ok"]
    assert calls == ["k1", "k2"]


def test_no_fallback_when_top_key_succeeds(monkeypatch):
    """最優先キーで成功したら下位キーは呼ばない(無駄な消費をしない)。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        return [{"viewCount": 1, "text": "ok"}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2", "k3"])
    assert calls == ["k1"]


def test_empty_result_does_not_fallback(monkeypatch):
    """空結果(該当ツイート無し)は確定。下位キーへフォールバックしない。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        return []

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    out = xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"], retries=1)
    assert out == []
    assert calls == ["k1"]


def test_all_keys_fail_raises(monkeypatch):
    """全キーが失敗したら最後の例外を送出する。"""
    def fake_fetch(q, qt, mx, key):
        raise xclient.XClientError(f"{key} dead")

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    with pytest.raises(xclient.XClientError):
        xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"])


# --- load_keys / _split_keys ---

def test_split_keys_comma_and_newline():
    assert xclient._split_keys("a, b\nc") == ["a", "b", "c"]
    assert xclient._split_keys("  ") == []
    assert xclient._split_keys(None) == []


def test_load_keys_single_key_backcompat(monkeypatch):
    """単一キーのみのとき1要素リストを返す(後方互換)。"""
    class S:
        twitterapi_io_key = "solo"

    monkeypatch.delenv("TWITTERAPI_IO_KEY", raising=False)
    monkeypatch.setattr(xclient, "_keychain_get", lambda: "")
    monkeypatch.setattr(xclient, "KEY_FILE", xclient.Path("/nonexistent/.key"))
    assert xclient.load_keys(S()) == ["solo"]


def test_load_keys_dedup_and_order(monkeypatch):
    """複数ソースから優先度順(.env→環境変数→Keychain)で集め、重複を順序保持で除去する。"""
    class S:
        twitterapi_io_key = "a,b"

    monkeypatch.setenv("TWITTERAPI_IO_KEY", "b,c")
    monkeypatch.setattr(xclient, "_keychain_get", lambda: "c")
    monkeypatch.setattr(xclient, "KEY_FILE", xclient.Path("/nonexistent/.key"))
    assert xclient.load_keys(S()) == ["a", "b", "c"]
