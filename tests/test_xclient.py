"""収集クエリ(言語フィルタ)と採用条件(いいね OR 表示回数)の回帰テスト。"""

from __future__ import annotations

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
                        lambda q, qt, mx, key: (cap.__setitem__("q", q), [])[1])
    xclient.collect("特大", settings=_Settings(), key="k")
    assert "lang:" not in cap["q"]


def test_collect_includes_lang_ja(monkeypatch):
    """lang 既定("ja")のジャンル(政治)は lang:ja を付ける。"""
    cap = {}
    monkeypatch.setattr(xclient, "fetch_with_retry",
                        lambda q, qt, mx, key: (cap.__setitem__("q", q), [])[1])
    xclient.collect("政治", settings=_Settings(), key="k")
    assert "lang:ja" in cap["q"]


def test_collect_keeps_high_view_even_if_low_likes(monkeypatch):
    """いいねが下限未満でも、表示回数が多い速報は採用する(いいね OR 表示回数)。"""
    tweets = [
        {"viewCount": 50000, "likeCount": 1, "text": "速報A", "createdAt": ""},   # 高view低like
        {"viewCount": 10, "likeCount": 1, "text": "雑談B", "createdAt": ""},        # 両方低い → 除外
        {"viewCount": 100, "likeCount": 500, "text": "人気C", "createdAt": ""},     # 高like → 採用
    ]
    monkeypatch.setattr(xclient, "fetch_with_retry", lambda *a, **k: [dict(t) for t in tweets])
    out = xclient.collect("特大", settings=_Settings(), key="k")  # 特大 min_faves=300
    texts = {t["text"] for t in out}
    assert "速報A" in texts and "人気C" in texts
    assert "雑談B" not in texts
