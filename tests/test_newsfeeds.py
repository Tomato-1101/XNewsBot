"""newsfeeds(RSS/GDELT パース)と速報検出ロジックの単体テスト。ネットワークは使わない。"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xnewsbot import newsfeeds as nf

# scripts/monitor_breaking.py を import(パッケージ外なのでパス指定でロード)
_MB_PATH = Path(__file__).resolve().parent.parent / "scripts" / "monitor_breaking.py"
_spec = importlib.util.spec_from_file_location("monitor_breaking", _MB_PATH)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)


SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item>
  <title>\xe6\x97\xa5\xe9\x8a\x80\xe3\x81\x8c\xe5\x88\xa9\xe4\xb8\x8a\xe3\x81\x92\xe3\x82\x92\xe6\xb1\xba\xe5\xae\x9a - NHK\xe3\x83\x8b\xe3\x83\xa5\xe3\x83\xbc\xe3\x82\xb9</title>
  <link>https://news.google.com/rss/articles/AAA</link>
  <pubDate>Thu, 02 Jul 2026 03:31:06 GMT</pubDate>
  <source url="https://www3.nhk.or.jp">NHK\xe3\x83\x8b\xe3\x83\xa5\xe3\x83\xbc\xe3\x82\xb9</source>
</item>
<item>
  <title>\xe6\x99\xae\xe9\x80\x9a\xe3\x81\xae\xe8\xa9\xb1\xe9\xa1\x8c - \xe3\x81\x82\xe3\x82\x8b\xe5\xaa\x92\xe4\xbd\x93</title>
  <link>https://news.google.com/rss/articles/BBB</link>
  <pubDate>Thu, 02 Jul 2026 03:00:00 GMT</pubDate>
  <source url="https://x">\xe3\x81\x82\xe3\x82\x8b\xe5\xaa\x92\xe4\xbd\x93</source>
</item>
</channel></rss>"""


def test_parse_google_rss_extracts_and_strips_source():
    items = nf.parse_google_rss(SAMPLE_RSS)
    assert len(items) == 2
    first = items[0]
    assert first.title == "日銀が利上げを決定"       # 「 - NHKニュース」が除去される
    assert first.source == "NHKニュース"
    assert first.url.endswith("AAA")
    assert first.published is not None and first.published.tzinfo is not None
    assert first.origin == "google_news"


def test_parse_google_rss_bad_xml_returns_empty():
    assert nf.parse_google_rss(b"<not xml") == []


def test_parse_gdelt_json():
    body = (b'{"articles":[{"title":"Big quake hits","url":"http://a/b",'
            b'"domain":"reuters.com","seendate":"20260702T030000Z"}]}')
    items = nf.parse_gdelt(body)
    assert len(items) == 1
    assert items[0].source == "reuters.com"
    assert items[0].origin == "gdelt"
    assert items[0].published.year == 2026


def test_parse_gdelt_rate_limit_text_returns_empty():
    # 429 時は JSON でないプレーンテキストが返る → 空でフェイルセーフ
    assert nf.parse_gdelt(b"Please limit requests to one every 5 seconds") == []


def _item(title, minutes_ago, now):
    return nf.FeedItem(title=title, url="http://x/" + title, source="s",
                       published=now - timedelta(minutes=minutes_ago), origin="google_news")


def test_select_breaking_freshness_and_markers():
    now = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)
    cands = [
        ("経済", _item("【速報】日銀が緊急利上げを決定", 5, now)),   # 新鮮+強マーカー → 採用
        ("株", _item("株価の平凡な値動きまとめ", 5, now)),          # マーカー無し → 不採用
        ("政治", _item("首相が辞任を表明", 200, now)),              # マーカー有だが古い → 不採用
    ]
    picked = mb.select_breaking(cands, level="medium", lookback_min=25, now=now, seen_keys=set())
    titles = [it.title for _, it, _ in picked]
    assert titles == ["【速報】日銀が緊急利上げを決定"]


def test_select_breaking_dedup_against_seen():
    now = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)
    it = _item("【速報】大地震が発生", 3, now)
    seen = {mb.norm_key(it.title)}
    picked = mb.select_breaking([("特大", it)], level="medium", lookback_min=25, now=now, seen_keys=seen)
    assert picked == []  # 既送は出さない


def test_select_breaking_strict_vs_broad():
    now = datetime(2026, 7, 2, 3, 30, tzinfo=timezone.utc)
    soft = [("AI", _item("新サービスを提供", 5, now))]  # マーカー無し
    assert mb.select_breaking(soft, level="strict", lookback_min=25, now=now, seen_keys=set()) == []
    # broad は鮮度のみ(マーカー不問)で採用する
    picked = mb.select_breaking(soft, level="broad", lookback_min=25, now=now, seen_keys=set())
    assert len(picked) == 1


# --- 直取り RSS(RSS2 / Atom / RDF)の汎用パース ---

RSS2_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel>
<item>
  <title>新モデル &amp; API を公開</title>
  <link>https://example.com/a</link>
  <pubDate>Thu, 01 Oct 2026 01:00:00 GMT</pubDate>
  <description>&lt;p&gt;本日、&lt;b&gt;新モデル&lt;/b&gt;を公開しました。&amp;amp;  詳細は&lt;a href="x"&gt;こちら&lt;/a&gt;&lt;/p&gt;</description>
</item>
<item>
  <title>リンクの無い項目</title>
</item>
</channel></rss>""".encode("utf-8")

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<entry>
  <title>Atom の記事</title>
  <link rel="self" href="https://example.com/self"/>
  <link rel="alternate" href="https://example.com/atom-1"/>
  <updated>2026-10-01T02:30:00+09:00</updated>
  <summary type="html">&lt;div&gt;要約です&lt;/div&gt;</summary>
</entry>
</feed>""".encode("utf-8")

RDF_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://example.com/rdf-1">
  <title>RDF の記事</title>
  <link>https://example.com/rdf-1</link>
  <dc:date>2026-10-01T09:00:00+09:00</dc:date>
  <description>RDF の概要</description>
</item>
</rdf:RDF>""".encode("utf-8")

GOOGLE_TOP_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item>
  <title>首相が会見 - NHKニュース</title>
  <link>https://news.google.com/rss/articles/CCC</link>
  <pubDate>Thu, 01 Oct 2026 00:00:00 GMT</pubDate>
  <description>&lt;a href="https://news.google.com/x"&gt;首相が会見&lt;/a&gt;&amp;nbsp;&lt;font&gt;NHKニュース&lt;/font&gt;</description>
  <source url="https://www3.nhk.or.jp">NHKニュース</source>
</item>
</channel></rss>""".encode("utf-8")


def test_parse_feed_rss2_strips_double_escaped_html():
    items = nf.parse_feed(RSS2_FEED, "Example")
    assert len(items) == 1                         # リンクの無い項目は落とす
    it = items[0]
    assert it.title == "新モデル & API を公開"
    assert it.url == "https://example.com/a"
    assert it.source == "Example" and it.origin == "feed"
    assert it.published == datetime(2026, 10, 1, 1, 0, tzinfo=timezone.utc)
    assert it.summary == "本日、 新モデル を公開しました。& 詳細は こちら"
    assert "<" not in it.summary


def test_parse_feed_atom_prefers_alternate_link_and_iso_date():
    it, = nf.parse_feed(ATOM_FEED, "Atom")
    assert it.url == "https://example.com/atom-1"
    assert it.published == datetime(2026, 9, 30, 17, 30, tzinfo=timezone.utc)
    assert it.summary == "要約です"


def test_parse_feed_rdf_with_dc_date():
    it, = nf.parse_feed(RDF_FEED, "PC Watch")
    assert (it.title, it.url, it.source) == ("RDF の記事", "https://example.com/rdf-1", "PC Watch")
    assert it.published == datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
    assert it.summary == "RDF の概要"


def test_parse_feed_google_top_uses_source_and_drops_echo_summary():
    """Google ニュースのトップ RSS: 媒体名は <source>、見出し末尾の「 - 媒体名」を削り、
    見出しの繰り返しだけの概要は空にする。"""
    it, = nf.parse_feed(GOOGLE_TOP_FEED, "Google ニュース(日本)")
    assert it.title == "首相が会見"
    assert it.source == "NHKニュース"
    assert it.summary == ""


def test_parse_feed_bad_xml_returns_empty():
    assert nf.parse_feed(b"<not-xml", "x") == []


def test_strip_html_limits_length():
    assert nf.strip_html("<p>" + "あ" * 500 + "</p>") == "あ" * 300
    assert nf.strip_html(None) == ""


def test_fetch_feed_keeps_only_recent_dated_items(monkeypatch):
    now = datetime.now(timezone.utc)

    def rfc(dt):
        return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")

    xml = f"""<rss version="2.0"><channel>
<item><title>新しい</title><link>https://e.com/1</link><pubDate>{rfc(now - timedelta(hours=2))}</pubDate></item>
<item><title>古い</title><link>https://e.com/2</link><pubDate>{rfc(now - timedelta(hours=30))}</pubDate></item>
<item><title>日付なし</title><link>https://e.com/3</link></item>
</channel></rss>""".encode("utf-8")
    monkeypatch.setattr(nf, "_get", lambda url: xml)
    assert [i.title for i in nf.fetch_feed("https://e.com/rss", "E", within_hours=24)] == ["新しい"]
    assert len(nf.fetch_feed("https://e.com/rss", "E", within_hours=None)) == 3


def test_fetch_feed_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(nf, "_get", lambda url: None)
    assert nf.fetch_feed("https://e.com/rss", "E") == []


def test_get_decompresses_gzip_even_without_header(monkeypatch):
    """Accept-Encoding を送らなくても gzip で返すサーバ(deepmind.google)の応答を展開する。"""
    import gzip

    class _Resp:
        def __init__(self, data):
            self._d = data

        def read(self):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(nf.urllib.request, "urlopen", lambda req, timeout=0: _Resp(gzip.compress(RDF_FEED)))
    assert nf._get("https://e.com/rss") == RDF_FEED
    monkeypatch.setattr(nf.urllib.request, "urlopen", lambda req, timeout=0: _Resp(RDF_FEED))
    assert nf._get("https://e.com/rss") == RDF_FEED


def test_as_candidate_keeps_legacy_keys_and_adds_new():
    it, = nf.parse_feed(RSS2_FEED, "Example")
    c = nf.as_candidate(it)
    # 既存キー(キュレーション・monitor_breaking が使う形)はそのまま
    assert c["text"] == "新モデル & API を公開"
    assert c["url"] == "https://example.com/a"
    assert c["viewCount"] == 0 and c["likeCount"] == 0
    assert c["author"] == {"userName": "Example", "followers": 0}
    assert c["source"] == "news"
    assert c["createdAt"].startswith("Thu Oct 01 01:00:00 +0000 2026")
    # 追加キー
    assert c["summary"].startswith("本日、")
    assert c["media"] == "Example"
    assert c["body"] == ""
