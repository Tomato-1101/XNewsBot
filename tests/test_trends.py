"""話題の取得元(trends)の単体テスト。ネットワークは使わない(実ページを削った tests/fixtures を読む)。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from xnewsbot import newsfeeds, trends
from xnewsbot.newsfeeds import FeedItem

FIX = Path(__file__).resolve().parent / "fixtures"
CAND_KEYS = set(newsfeeds.as_candidate(FeedItem("t", "u", "s", None, "feed")))


def _fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _serve(monkeypatch, body: bytes | None):
    urls = []
    monkeypatch.setattr(trends, "_get", lambda url: urls.append(url) or body)
    return urls


# --- Yahoo!リアルタイム検索 ---

def test_parse_yahoo_realtime():
    words = trends.parse_yahoo_realtime(_fx("yahoo_realtime_trend.html"))
    assert [w["word"] for w in words] == ["日本マクドナルド", "ドナルド・マクドナルド", "新 美味しんぼ", "解散ライブ"]
    assert words[0]["posts"] == 87 and words[0]["related"][0] == "ロナルド"
    assert trends.parse_yahoo_realtime(b"<html>no data</html>") == []


def test_yahoo_realtime_adds_headlines_and_skips_words_without_news(monkeypatch):
    _serve(monkeypatch, _fx("yahoo_realtime_trend.html"))
    monkeypatch.setattr(trends, "YAHOO_WORDS", 3)
    calls = []

    def gn(q, within_hours=None, **kw):
        calls.append((q, within_hours))
        if q == "ドナルド・マクドナルド":
            return []                                  # 見出しが無い語は候補にしない
        return [FeedItem(f"{q}の見出し{i}", f"https://example.com/{q}/{i}", "媒体", None, "google_news")
                for i in range(3)]
    monkeypatch.setattr(trends.newsfeeds, "google_news", gn)
    out = trends.yahoo_realtime(24)
    assert sorted(calls) == sorted([("日本マクドナルド", 24), ("ドナルド・マクドナルド", 24),
                                    ("新 美味しんぼ", 24)])   # 上位 YAHOO_WORDS 語だけ引く
    assert [c["text"] for c in out] == ["日本マクドナルドの見出し0", "新 美味しんぼの見出し0"]
    assert set(out[0]) == CAND_KEYS | {"trend"} and out[0]["source"] == "news"
    assert out[0]["trend"] == {"source": "yahoo_realtime", "word": "日本マクドナルド", "posts": 87,
                               "related": ["ロナルド", "品質基準", "ドナルドの", "販売休止", "ハンバーガー"],
                               "headlines": ["日本マクドナルドの見出し0", "日本マクドナルドの見出し1"]}


# --- Google トレンド ---

def test_google_trends_candidates_from_related_news(monkeypatch):
    urls = _serve(monkeypatch, _fx("google_trends_jp.xml"))
    out = trends.google_trends(None)
    assert urls == [trends.GOOGLE_TRENDS_URL]
    assert [c["trend"]["word"] for c in out] == ["鈴木唯人", "川口春奈", "中国新聞"]
    c = out[0]
    assert c["url"] == "https://news.yahoo.co.jp/articles/71904151e2248b492dc31825506e411b43aa840d"
    assert c["media"] == "Yahoo!ニュース" and c["text"].startswith("「ユイト君のチーム」")
    assert c["trend"]["traffic"] == "2000+" and len(c["trend"]["headlines"]) == 3
    assert c["createdAt"] == "Thu Oct 01 10:40:00 +0000 2026"


def _gt_xml(items: list[tuple[str, datetime, bool]]) -> bytes:
    body = "".join(
        f"<item><title>{w}</title><ht:approx_traffic>500+</ht:approx_traffic>"
        f"<pubDate>{format_datetime(pub)}</pubDate>"
        + (f"<ht:news_item><ht:news_item_title>{w}のニュース</ht:news_item_title>"
           f"<ht:news_item_url>https://example.com/{w}</ht:news_item_url>"
           f"<ht:news_item_source>媒体</ht:news_item_source></ht:news_item>" if news else "")
        + "</item>" for w, pub, news in items)
    return (f'<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>{body}'
            "</channel></rss>").encode()


def test_google_trends_drops_old_and_newsless(monkeypatch):
    now = datetime.now(timezone.utc)
    _serve(monkeypatch, _gt_xml([("新しい", now - timedelta(hours=1), True),
                                 ("古い", now - timedelta(hours=30), True),
                                 ("記事なし", now, False)]))
    assert [c["trend"]["word"] for c in trends.google_trends(24)] == ["新しい"]


# --- はてなブックマーク ---

def test_hatena_sorted_by_bookmarks(monkeypatch):
    _serve(monkeypatch, _fx("hatena_hotentry.xml"))
    out = trends.hatena(None)
    assert [c["trend"]["bookmarks"] for c in out] == [519, 449, 390, 222]
    assert out[0]["text"] == "あなたの納得はチームには関係ない｜Aki" and out[0]["media"] == "note.com"
    assert out[-1]["media"] == "news.denfaminicogamer.jp"   # www. は落とす・ドメインを媒体名に
    assert out[-1]["summary"].startswith("マクドナルドのキャラクター")
    assert set(out[0]) == CAND_KEYS | {"trend"}


def _hatena_xml(items: list[tuple[str, datetime, int]]) -> bytes:
    body = "".join(
        f'<item rdf:about="https://www.example.com/{n}"><title>{t}</title>'
        f"<link>https://www.example.com/{n}</link><description>概要</description>"
        f"<dc:date>{d.strftime('%Y-%m-%dT%H:%M:%SZ')}</dc:date>"
        f"<hatena:bookmarkcount>{n}</hatena:bookmarkcount></item>" for t, d, n in items)
    return ('<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:hatena="http://www.hatena.ne.jp/info/xmlns#">{body}</rdf:RDF>').encode()


def test_hatena_unescapes_titles_and_drops_old_entries(monkeypatch):
    now = datetime.now(timezone.utc)
    _serve(monkeypatch, _hatena_xml([
        ("Rust&amp;#39;s &amp;amp; Go", now - timedelta(hours=2), 50),   # 二重の実体参照
        ("古い記事", now - timedelta(hours=40), 900),
    ]))
    out = trends.hatena(24)
    assert [c["text"] for c in out] == ["Rust's & Go"]
    assert out[0]["media"] == "example.com"


# --- 失敗時 ---

def test_sources_failure_returns_empty_with_one_line(monkeypatch, capsys):
    _serve(monkeypatch, None)

    def boom(url, timeout):
        raise TimeoutError("down")
    monkeypatch.setattr(trends, "_get_json", boom)    # 新モデル2取得元(JSON API)も実ネットに出さない
    assert all(fn(24) == [] for fn in trends.SOURCES.values())
    _serve(monkeypatch, b"<rss><broken")
    assert trends.SOURCES["google_trends"](24) == [] and trends.SOURCES["hatena"](24) == []
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 7 and all(line.strip().startswith("話題:") for line in err)
