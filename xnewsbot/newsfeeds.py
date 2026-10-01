"""無料ニュースソース(Google ニュースRSS / GDELT)の読み取り専用クライアント。APIキー不要。

速報監視(scripts/monitor_breaking.py)と、定時ダイジェストの候補拡張(pipeline collect)の
両方で共用する。twitterapi.io(有料)を補完/代替する無料の一次情報源。

- Google ニュースRSS: 主力。1クエリ最大100件・日本語/英語キーワード検索・pubDate で直近性が取れる。
  キー不要で安定。規約上「個人・非商用のフィードリーダー用途」なので少人数運用の範囲で使う。
- GDELT DOC 2.0 API: best-effort の補助。5秒/回のレート制限があり、超過すると 429/文言を返す。
  失敗しても本処理を壊さないよう、例外・非JSONは常に空リストで返す。
- 直取り RSS(fetch_feed): 媒体・公式ブログの RSS2.0 / Atom / RDF(RSS1.0) を汎用に読む
  (genres.toml の feeds)。Google ニュース経由より一次情報に近く、概要(description)も取れる。

いずれも urllib + 標準ライブラリのみ(xclient.py と同じ方針・新規依存なし)。取得結果は FeedItem に正規化。
"""

from __future__ import annotations

import gzip
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

# Google ニュースRSS は素の urllib UA だと弾かれることがあるためブラウザ風 UA を付ける。
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
_TIMEOUT = 20

GOOGLE_NEWS = "https://news.google.com/rss/search"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT は「1リクエスト/5秒」を要求する。プロセス内で最後の呼び出し時刻を持ち、間隔を空ける。
_GDELT_MIN_INTERVAL = 5.0
_gdelt_last = 0.0


@dataclass
class FeedItem:
    title: str
    url: str
    source: str                  # 媒体名(Google=<source>, GDELT=domain)
    published: datetime | None   # tz-aware(UTC)。取れなければ None
    origin: str                  # "google_news" | "gdelt" | "feed"
    summary: str = ""            # 概要(HTML 除去・空白正規化・300字まで)。取れなければ空


def _get(url: str) -> bytes | None:
    """URL を GET。失敗(タイムアウト/HTTP/接続断)は None を返す(呼び出し側で握らず握られる想定)。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None
    # Accept-Encoding を送らなくても gzip で返すサーバがある(deepmind.google で実測。
    # 応答ごとに圧縮/非圧縮が揺れる)。先頭のマジックで判定して展開する。
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            return None
    return data


def _parse_pubdate(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # naive(tz 情報なし)は UTC とみなす。以降の比較は必ず tz-aware で行う。
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _google_when(hours: float | None) -> str:
    """Google ニュースは検索クエリ内の `when:1h` / `when:2d` で期間を絞れる。"""
    if not hours:
        return ""
    if hours < 24:
        return f" when:{max(1, int(round(hours)))}h"
    return f" when:{max(1, int(round(hours / 24)))}d"


def parse_google_rss(xml_bytes: bytes) -> list[FeedItem]:
    """Google ニュースRSS の XML を FeedItem 配列へ(パースだけ・ネットワークなし=テスト可能)。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[FeedItem] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        # Google の title は「見出し - 媒体名」の形が多い。媒体名の重複を削って見出しだけにする。
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        out.append(FeedItem(
            title=title, url=link, source=source or "Google ニュース",
            published=_parse_pubdate(item.findtext("pubDate")), origin="google_news",
        ))
    return out


def google_news(query: str, *, lang: str = "ja", region: str = "JP",
                within_hours: float | None = 24, hl: str | None = None) -> list[FeedItem]:
    """Google ニュースRSS をキーワード検索して FeedItem 配列を返す。失敗時は空リスト。

    lang/region: "ja"/"JP"=日本語, "en"/"US"=英語(世界の一次速報)。
    within_hours: 直近N時間に絞る(クエリ内 when:)。None なら絞らない。
    hl: 表示言語(省略時は lang)。英語は Google 推奨形の "en-US" を渡す。
    """
    q = query + _google_when(within_hours)
    ceid = f"{region}:{lang}"
    url = f"{GOOGLE_NEWS}?" + urllib.parse.urlencode(
        {"q": q, "hl": hl or lang, "gl": region, "ceid": ceid})
    body = _get(url)
    if not body:
        return []
    return parse_google_rss(body)


def parse_gdelt(body: bytes) -> list[FeedItem]:
    """GDELT DOC API の JSON を FeedItem 配列へ。非JSON(レート制限文言等)は空。"""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        return []  # 429 時は "Please limit requests..." のプレーンテキストが返る
    out: list[FeedItem] = []
    for a in data.get("articles") or []:
        title = (a.get("title") or "").strip()
        url = (a.get("url") or "").strip()
        if not title or not url:
            continue
        seen = a.get("seendate") or ""
        pub = None
        try:
            pub = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pub = None
        out.append(FeedItem(title=title, url=url, source=a.get("domain") or "GDELT",
                            published=pub, origin="gdelt"))
    return out


_SUMMARY_MAX = 300
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(raw: str | None, limit: int = _SUMMARY_MAX) -> str:
    """HTML タグ・実体参照を除いて空白を正規化し limit 字までにする。

    RSS の description は実体参照で二重にエスケープされた HTML が多く、1回の unescape では
    タグが残るため「タグ除去→unescape→タグ除去」の順に通す。"""
    s = _TAG_RE.sub(" ", raw or "")
    s = html.unescape(s)
    s = _TAG_RE.sub(" ", s)
    s = " ".join(s.split())
    return s[:limit]


def _local(tag: str) -> str:
    """'{名前空間}item' → 'item'。RSS2/Atom/RDF の名前空間差を吸収する。"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse_date_any(raw: str | None) -> datetime | None:
    """RFC822(pubDate) と ISO8601(Atom published/updated, dc:date) の両方を tz-aware(UTC) に。"""
    if not raw:
        return None
    raw = raw.strip()
    dt = _parse_pubdate(raw)
    if dt is not None:
        return dt
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _entry_link(el) -> str:
    """item/entry のリンク。Atom は <link href rel="alternate">、RSS/RDF は <link>テキスト。"""
    fallback = ""
    for ch in el:
        if _local(ch.tag) != "link":
            continue
        href = ch.get("href")
        if href:
            if ch.get("rel") in (None, "alternate"):
                return href.strip()
            fallback = fallback or href.strip()
        elif (ch.text or "").strip():
            return ch.text.strip()
    if not fallback:
        # RDF は rdf:about に URL を持つこともある
        for k, v in el.attrib.items():
            if _local(k) == "about" and v:
                return v.strip()
    return fallback


def parse_feed(xml_bytes: bytes, name: str) -> list[FeedItem]:
    """RSS2.0 / Atom / RDF(RSS1.0) を FeedItem 配列へ(パースだけ・ネットワークなし=テスト可能)。

    item に <source> があれば(Google ニュースのトップ記事など)媒体名をそれにし、見出し末尾の
    「 - 媒体名」を削る。概要が見出しの繰り返しだけなら(Google ニュース)空にする。
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: list[FeedItem] = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields: dict[str, object] = {}
        for ch in el:
            fields.setdefault(_local(ch.tag), ch)
        title_el = fields.get("title")
        title = strip_html(title_el.text if title_el is not None else "", limit=500)
        link = _entry_link(el)
        if not title or not link:
            continue
        source = name
        src_el = fields.get("source")
        if src_el is not None and (src_el.text or "").strip():
            source = src_el.text.strip()
            if title.endswith(f" - {source}"):
                title = title[: -len(f" - {source}")].strip()
        pub = None
        for k in ("pubDate", "published", "updated", "date", "issued", "modified"):
            d = fields.get(k)
            if d is not None and (d.text or "").strip():
                pub = _parse_date_any(d.text)
                if pub is not None:
                    break
        summary = ""
        for k in ("description", "summary", "encoded", "content"):
            d = fields.get(k)
            if d is not None and (d.text or "").strip():
                summary = strip_html(d.text)
                if summary:
                    break
        if summary and "".join(summary.split()).startswith("".join(title.split())):
            summary = ""  # Google ニュースの description は見出し+媒体名の繰り返しで情報が無い
        out.append(FeedItem(title=title, url=link, source=source, published=pub,
                            origin="feed", summary=summary))
    return out


def fetch_feed(url: str, name: str, within_hours: float | None = 24) -> list[FeedItem]:
    """直取り RSS を読み、直近 within_hours 以内の項目を返す。失敗時は空リスト。

    日付の無い項目は鮮度を確かめられないので落とす(古い記事を「今日のニュース」として出さない)。
    """
    body = _get(url)
    if not body:
        return []
    items = parse_feed(body, name)
    if not within_hours:
        return items
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    return [it for it in items if it.published is not None and it.published >= cutoff]


def as_candidate(item: FeedItem) -> dict:
    """FeedItem をキュレーション候補(pipeline の _trim と同じ形)に変換する。

    ニュース記事はエンゲージ指標を持たないので viewCount/likeCount=0、author.userName=媒体名。
    `source="news"` を付けてツイート由来と区別できるようにする(キュレーションプロンプトが参照)。
    summary=概要、media=媒体名、body=本文抜粋(articles.enrich_bodies が後から埋める。初期値は空)。
    """
    created = ""
    if item.published is not None:
        created = item.published.strftime("%a %b %d %H:%M:%S %z %Y")
    return {
        "text": item.title,
        "viewCount": 0,
        "likeCount": 0,
        "url": item.url,
        "createdAt": created,
        "author": {"userName": item.source, "followers": 0},
        "source": "news",
        "summary": item.summary,
        "media": item.source,
        "body": "",
    }


def gdelt(query: str, *, timespan: str = "1h", maxrecords: int = 25) -> list[FeedItem]:
    """GDELT DOC 2.0 を検索(best-effort)。5秒/回のレート制限を尊重し、失敗は空リスト。"""
    global _gdelt_last
    wait = _GDELT_MIN_INTERVAL - (time.monotonic() - _gdelt_last)
    if wait > 0:
        time.sleep(wait)
    _gdelt_last = time.monotonic()
    url = f"{GDELT_DOC}?" + urllib.parse.urlencode({
        "query": query, "mode": "artlist", "format": "json",
        "timespan": timespan, "maxrecords": maxrecords, "sort": "datedesc"})
    body = _get(url)
    if not body:
        return []
    return parse_gdelt(body)
