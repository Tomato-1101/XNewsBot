"""「話題」ジャンル用: 急上昇ワード・人気エントリーを無料の公開ページから集めて候補にする。APIキー不要。

X や世の中で大きく話題になっていることを拾うため(本人要望 2026-10-01)。候補の形は
newsfeeds.as_candidate と同じ(pipeline.merge_news でニュース候補とそのまま混ぜる)で、
どれだけ話題かを `trend` キーに添える(キュレーションが話題の大きさを判断する材料):
  {"source": "yahoo_realtime", "word", "posts", "related": [関連語], "headlines": [見出し]}
  {"source": "google_trends", "word", "traffic", "headlines": [見出し]}
  {"source": "hatena", "bookmarks"}

取得元(どれも鍵なし。2026-10-01 に実ページを1回ずつ取得して構造を確認):
- Yahoo!リアルタイム検索のトレンド(HTML 内 __NEXT_DATA__ の pageData.buzzTrend.items):
  X 由来の語と投稿数。語だけでは何が起きたか分からないので、上位の語ごとに Google ニュースの
  見出しを引いて候補にする(見出しが無い語は候補にしない)。
- Google トレンド急上昇 RSS(関連ニュースつき): 関連ニュースの先頭を候補にする。
- はてなブックマーク人気エントリー RSS(RDF・ブクマ数つき): ブクマ数の多い順。
取得元ごとに失敗しても空を返す(stderr に1行)。配信は止めない。
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import newsfeeds
from .newsfeeds import FeedItem

YAHOO_RT_URL = "https://search.yahoo.co.jp/realtime/search/trend"
GOOGLE_TRENDS_URL = "https://trends.google.com/trending/rss?geo=JP"
HATENA_URL = "https://b.hatena.ne.jp/hotentry.rss"

YAHOO_WORDS = 10        # Yahoo のトレンド語は上位この数まで(1語ごとに Google ニュースを1回引く)
HEADLINES = 2           # Yahoo の1語あたりの見出し数
GOOGLE_TRENDS_MAX = 15
GT_HEADLINES = 3        # Google トレンドの1語あたりに添える見出し数
HATENA_MAX = 15

# 同じ UA・タイムアウト・失敗時 None(newsfeeds と同じ取り方)
_get = newsfeeds._get

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)
_HT = "{https://trends.google.com/trending/rss}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_HATENA = "{http://www.hatena.ne.jp/info/xmlns#}"


def _need(url: str) -> bytes:
    body = _get(url)
    if not body:
        raise ConnectionError(f"取得できませんでした: {url}")
    return body


def _cutoff(hours: float | None) -> datetime | None:
    return datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None


def _candidate(item: FeedItem, trend: dict) -> dict:
    c = newsfeeds.as_candidate(item)
    c["trend"] = trend
    return c


# --- Yahoo!リアルタイム検索 ---

def parse_yahoo_realtime(body: bytes) -> list[dict]:
    """トレンドページ → [{"word", "posts", "related"}](ページの順=トレンドの順)。"""
    m = _NEXT_DATA.search(body.decode("utf-8", "replace"))
    if not m:
        return []
    data = json.loads(m.group(1))
    items = (((data.get("props") or {}).get("pageProps") or {}).get("pageData") or {}) \
        .get("buzzTrend", {}).get("items") or []
    out: list[dict] = []
    for it in items:
        word = " ".join(str(it.get("query") or "").split())
        if word:
            out.append({"word": word, "posts": int(it.get("tweetCount") or 0),
                        "related": [str(x) for x in it.get("childBuzz") or []]})
    return out


def yahoo_realtime(hours: float | None = 24) -> list[dict]:
    words = parse_yahoo_realtime(_need(YAHOO_RT_URL))[:YAHOO_WORDS]
    with ThreadPoolExecutor(max_workers=5) as pool:
        heads = list(pool.map(
            lambda w: newsfeeds.google_news(w["word"], within_hours=hours)[:HEADLINES], words))
    return [_candidate(hs[0], {"source": "yahoo_realtime", "word": w["word"], "posts": w["posts"],
                               "related": w["related"], "headlines": [h.title for h in hs]})
            for w, hs in zip(words, heads) if hs]


# --- Google トレンド ---

def parse_google_trends(body: bytes) -> list[dict]:
    """急上昇 RSS → [{"word", "traffic", "published", "news": [FeedItem]}]。"""
    root = ET.fromstring(body)
    out: list[dict] = []
    for item in root.iter("item"):
        word = (item.findtext("title") or "").strip()
        pub = newsfeeds._parse_date_any(item.findtext("pubDate"))
        news = []
        for n in item.findall(f"{_HT}news_item"):
            title = newsfeeds.strip_html(n.findtext(f"{_HT}news_item_title"), limit=500)
            url = (n.findtext(f"{_HT}news_item_url") or "").strip()
            if title and url:
                news.append(FeedItem(title=title, url=url, published=pub, origin="feed",
                                     source=(n.findtext(f"{_HT}news_item_source") or "").strip()
                                     or "Google トレンド"))
        if word:
            out.append({"word": word, "traffic": (item.findtext(f"{_HT}approx_traffic") or "").strip(),
                        "published": pub, "news": news})
    return out


def google_trends(hours: float | None = 24) -> list[dict]:
    cut = _cutoff(hours)
    out: list[dict] = []
    for t in parse_google_trends(_need(GOOGLE_TRENDS_URL)):
        if not t["news"] or (cut and t["published"] and t["published"] < cut):
            continue
        out.append(_candidate(t["news"][0], {
            "source": "google_trends", "word": t["word"], "traffic": t["traffic"],
            "headlines": [n.title for n in t["news"][:GT_HEADLINES]]}))
    return out[:GOOGLE_TRENDS_MAX]


# --- はてなブックマーク ---

def parse_hatena(body: bytes) -> list[tuple[FeedItem, int]]:
    """人気エントリー RSS(RDF) → [(FeedItem, ブクマ数)]。媒体名は記事のドメイン。"""
    root = ET.fromstring(body)
    out: list[tuple[FeedItem, int]] = []
    for item in root.iter(f"{_RSS1}item"):
        # 見出しは実体参照が二重のことがある(&amp;#39; 等)。strip_html が unescape する
        title = newsfeeds.strip_html(item.findtext(f"{_RSS1}title"), limit=500)
        url = (item.findtext(f"{_RSS1}link") or "").strip()
        if not title or not url:
            continue
        host = urllib.parse.urlsplit(url).hostname or "はてなブックマーク"
        try:
            n = int(item.findtext(f"{_HATENA}bookmarkcount") or 0)
        except ValueError:
            n = 0
        out.append((FeedItem(title=title, url=url, source=host.removeprefix("www."),
                             published=newsfeeds._parse_date_any(item.findtext(f"{_DC}date")),
                             origin="feed", summary=newsfeeds.strip_html(item.findtext(f"{_RSS1}description"))),
                    n))
    return out


def hatena(hours: float | None = 24) -> list[dict]:
    cut = _cutoff(hours)
    rows = [(it, n) for it, n in parse_hatena(_need(HATENA_URL))
            if not cut or (it.published is not None and it.published >= cut)]
    rows.sort(key=lambda r: -r[1])
    return [_candidate(it, {"source": "hatena", "bookmarks": n}) for it, n in rows[:HATENA_MAX]]


def _safe(name: str, fn):
    def run(hours: float | None = 24) -> list[dict]:
        try:
            return fn(hours)
        except Exception as e:
            print(f"  話題: {name} 取得失敗のため省略 ({type(e).__name__}: {e})", file=sys.stderr)
            return []
    return run


# genres.toml の trend_sources に書く名前 → 取得関数(hours) -> 候補。失敗は [](stderr に1行)。
SOURCES = {
    "yahoo_realtime": _safe("Yahoo!リアルタイム検索", yahoo_realtime),
    "google_trends": _safe("Google トレンド", google_trends),
    "hatena": _safe("はてなブックマーク", hatena),
}
