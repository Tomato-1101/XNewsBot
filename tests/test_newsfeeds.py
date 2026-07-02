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
