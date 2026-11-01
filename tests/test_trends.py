"""話題の取得元(trends)の単体テスト。ネットワークは使わない(実ページを削った tests/fixtures を読む)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from xnewsbot import genres, newsfeeds, trends
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


# --- alphaXiv の Hot 論文 ---

def _ax() -> dict:
    return json.loads(_fx("alphaxiv_hot.json"))


def test_parse_alphaxiv_keeps_hot_order_and_builds_candidates():
    out = trends.parse_alphaxiv(_ax())
    assert [c["trend"]["id"] for c in out] == ["2610.40564", "2610.40476", "2610.40636", "2610.40647",
                                               "2610.40325"]   # id の無い要素は飛ばす
    assert [c["trend"]["rank"] for c in out] == [1, 2, 3, 4, 5]
    c = out[0]
    assert set(c) == CAND_KEYS | {"trend"} and c["source"] == "news" and c["media"] == "alphaXiv"
    assert c["text"] == "Training Foundation Models with a Single Algorithm"
    assert c["url"] == "https://www.alphaxiv.org/abs/2610.40564"
    assert c["createdAt"].startswith("Thu Oct 29 17:42:05")
    assert c["trend"] == {"source": "alphaxiv_hot", "rank": 1, "id": "2610.40564", "votes": 63, "visits": 755,
                          "topics": ["Computer Science", "cs.LG"],
                          "orgs": ["Carnegie Mellon University", "Johns Hopkins University", "Aurora Innovation"]}
    assert c["summary"].startswith("alphaXiv Hot 1位(投票 63・閲覧 755)。所属: Carnegie Mellon University・")
    assert "GitHub: なし。The authors cast pretraining" in c["summary"]
    abstract = _ax()["papers"][0]["abstract"]                                       # 要旨を本文に入れる
    assert c["body"] == " ".join(abstract.split())[:trends.ALPHAXIV_BODY_CHARS] and c["body"]
    assert "GitHub: あり" in out[3]["summary"]
    # paper_summary が無い論文は feed_description を使う
    assert out[2]["summary"].endswith(_ax()["papers"][2]["feed_description"][:trends.ALPHAXIV_SUMMARY_CHARS])


def test_alphaxiv_hot_requests_hot_feed_and_caps(monkeypatch, capsys):
    urls = []
    monkeypatch.setattr(trends, "_get_json", lambda url, timeout: urls.append(url) or _ax())
    monkeypatch.setattr(trends, "_delivered_ids", lambda: set())
    monkeypatch.setattr(trends, "ALPHAXIV_MAX", 2)
    out = trends.SOURCES["alphaxiv_hot"](24)
    assert [c["trend"]["rank"] for c in out] == [1, 2]
    assert urls == ["https://api.alphaxiv.org/papers/v3/feed?pageNum=0&sort=Hot&interval=3%20Days&pageSize=30"]
    assert capsys.readouterr().err.strip() == "論文: alphaXiv 2件（既出除外 0件）"


def test_alphaxiv_hot_skips_delivered_papers_then_takes_top(monkeypatch, capsys):
    """既出(直近14日に配信した論文)を除いてから上位 ALPHAXIV_MAX 本。rank は Hot の元の順位のまま。"""
    monkeypatch.setattr(trends, "_get_json", lambda url, timeout: _ax())
    monkeypatch.setattr(trends, "_delivered_ids", lambda: {"2610.40564", "2610.40636"})
    monkeypatch.setattr(trends, "ALPHAXIV_MAX", 2)
    out = trends.SOURCES["alphaxiv_hot"](24)
    assert [(c["trend"]["id"], c["trend"]["rank"]) for c in out] == [("2610.40476", 2), ("2610.40647", 4)]
    assert capsys.readouterr().err.strip() == "論文: alphaXiv 2件（既出除外 2件）"


def test_delivered_paper_ids_reads_recent_digest_urls(session):
    """配信済みの判定は DB の出典 URL。前日〜14日前だけ(当日と15日前以前は含めない)・ジャンルを問わない。"""
    from datetime import date

    from xnewsbot.models import GenreDigest, NewsItem

    day = date(2026, 11, 1)
    for d, genre, urls in [(1, "論文", ["https://www.alphaxiv.org/abs/2610.1"]),
                           (14, "AI", ["https://example.com/x", "https://www.alphaxiv.org/abs/2610.2"]),
                           (0, "論文", ["https://www.alphaxiv.org/abs/2610.3"]),
                           (15, "論文", ["https://www.alphaxiv.org/abs/2610.4"])]:
        gd = GenreDigest(digest_date=day - timedelta(days=d), genre=genre)
        session.add(gd)
        session.flush()
        session.add(NewsItem(genre_digest_id=gd.id, genre=genre, title="t", source_urls=urls))
    session.commit()
    assert trends.delivered_paper_ids(session, day) == {"2610.1", "2610.2"}


def test_delivered_ids_failure_is_no_exclusion(monkeypatch, capsys):
    import xnewsbot.db as db

    def boom():
        raise RuntimeError("locked")
    monkeypatch.setattr(db, "init_db", boom)
    assert trends._delivered_ids() == set()
    assert capsys.readouterr().err.strip().startswith("論文: 既出の論文を読めず除外なしで続行")


def test_alphaxiv_hot_failure_and_broken_shape_are_empty(monkeypatch):
    def boom(url, timeout):
        raise TimeoutError("down")
    monkeypatch.setattr(trends, "_get_json", boom)
    assert trends.SOURCES["alphaxiv_hot"](24) == []
    assert trends.parse_alphaxiv({}) == [] and trends.parse_alphaxiv([]) == []   # type: ignore[arg-type]
    # null など dict でない要素は飛ばす(残りは順位どおり)
    broken = {"papers": [None, "x", _ax()["papers"][0]]}
    assert [c["trend"]["rank"] for c in trends.parse_alphaxiv(broken)] == [1]


def test_paper_genre_uses_only_alphaxiv():
    assert genres.trend_sources("論文") == ["alphaxiv_hot"]
    assert not genres.keywords("論文") and not genres.keywords_en("論文") and not genres.accounts("論文")
    assert not genres.feeds("論文") and not genres.x_queries("論文")
    keys = genres.GENRE_KEYS
    assert keys.index("論文") == keys.index("監視アカウント") + 1


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
    assert len(err) == 8 and all(line.strip().startswith("話題:") for line in err)
