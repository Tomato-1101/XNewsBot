"""無料ニュースソース(Google ニュースRSS / GDELT)の読み取り専用クライアント。APIキー不要。

速報監視(scripts/monitor_breaking.py)と、定時ダイジェストの候補拡張(pipeline collect)の
両方で共用する。twitterapi.io(有料)を補完/代替する無料の一次情報源。

- Google ニュースRSS: 主力。1クエリ最大100件・日本語/英語キーワード検索・pubDate で直近性が取れる。
  キー不要で安定。規約上「個人・非商用のフィードリーダー用途」なので少人数運用の範囲で使う。
- GDELT DOC 2.0 API: best-effort の補助。5秒/回のレート制限があり、超過すると 429/文言を返す。
  失敗しても本処理を壊さないよう、例外・非JSONは常に空リストで返す。

いずれも urllib + 標準ライブラリのみ(xclient.py と同じ方針・新規依存なし)。取得結果は FeedItem に正規化。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
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
    origin: str                  # "google_news" | "gdelt"


def _get(url: str) -> bytes | None:
    """URL を GET。失敗(タイムアウト/HTTP/接続断)は None を返す(呼び出し側で握らず握られる想定)。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None


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
                within_hours: float | None = 24) -> list[FeedItem]:
    """Google ニュースRSS をキーワード検索して FeedItem 配列を返す。失敗時は空リスト。

    lang/region: "ja"/"JP"=日本語, "en"/"US"=英語(世界の一次速報)。
    within_hours: 直近N時間に絞る(クエリ内 when:)。None なら絞らない。
    """
    q = query + _google_when(within_hours)
    ceid = f"{region}:{lang}"
    url = f"{GOOGLE_NEWS}?" + urllib.parse.urlencode(
        {"q": q, "hl": lang, "gl": region, "ceid": ceid})
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


def as_candidate(item: FeedItem) -> dict:
    """FeedItem をキュレーション候補(pipeline の _trim と同じ形)に変換する。

    ニュース記事はエンゲージ指標を持たないので viewCount/likeCount=0、author.userName=媒体名。
    `source="news"` を付けてツイート由来と区別できるようにする(キュレーションプロンプトが参照)。
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
