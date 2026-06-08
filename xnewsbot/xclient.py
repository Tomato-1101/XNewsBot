"""twitterapi.io 経由でXを読み取る薄いクライアント(読み取り専用)。

ロジックは実証済みの x-research/scripts/x_search.py から移植(出典)。
クロスリポジトリ import を避け XNewsBot 内に自己完結させる(x-research のパス移動で壊れないため)。
鍵だけは Keychain(service=twitterapi_io_key) 経由で x-research と共有できる。

鍵の探索順: Settings(.env/env) -> 環境変数 -> macOS Keychain -> プロジェクト直下 .key。
鍵は表示・送信しない。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings, get_settings
from .genres import keywords

BASE_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
KEY_FILE = Path(__file__).resolve().parent.parent / ".key"


class XClientError(RuntimeError):
    pass


def _keychain_get() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "twitterapi_io_key", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""


def load_key(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    key = (settings.twitterapi_io_key or "").strip()
    if not key:
        key = os.environ.get("TWITTERAPI_IO_KEY", "").strip()
    if not key:
        key = _keychain_get()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise XClientError(
            "twitterapi.io の APIキーが見つかりません(.env / 環境変数 / Keychain / .key いずれも未設定)。\n"
            "  Keychain設定例: security add-generic-password -a \"$USER\" -s twitterapi_io_key -w"
        )
    return key


def _int(obj: dict, field: str) -> int:
    try:
        return int(obj.get(field) or 0)
    except (TypeError, ValueError):
        return 0


def views(t: dict) -> int:
    return _int(t, "viewCount")


def _window_clause(hours: float | None) -> str:
    """直近N時間に絞る。twitterapi.io は within_time:<N>h を解釈する。
    (since_time/until_time の epoch 秒は proxy 側で 0 件になるため使わない。)"""
    if not hours:
        return ""
    return f" within_time:{max(1, int(round(hours)))}h"


_CREATED_FMT = "%a %b %d %H:%M:%S %z %Y"  # 例: 'Mon Jun 08 12:27:09 +0000 2026'


def _parse_created_at(t: dict) -> datetime | None:
    raw = t.get("createdAt")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, _CREATED_FMT)
    except (ValueError, TypeError):
        return None


def _filter_recent(tweets: list[dict], hours: float | None) -> list[dict]:
    """createdAt による直近N時間フィルタ(within_time が効かない場合のバックストップ)。
    パースできないものは安全側で残す。"""
    if not hours:
        return tweets
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for t in tweets:
        dt = _parse_created_at(t)
        if dt is None or dt >= cutoff:
            out.append(t)
    return out


def _request(query: str, query_type: str, cursor: str, key: str) -> dict:
    qs = urllib.parse.urlencode({"query": query, "queryType": query_type, "cursor": cursor})
    req = urllib.request.Request(f"{BASE_URL}?{qs}", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise XClientError(f"twitterapi.io HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise XClientError(f"twitterapi.io 接続エラー: {e.reason}")


def fetch(query: str, query_type: str, max_tweets: int, key: str) -> list[dict]:
    """next_cursor を辿って max_tweets まで集める。"""
    collected: list[dict] = []
    cursor = ""
    while len(collected) < max_tweets:
        data = _request(query, query_type, cursor, key)
        tweets = data.get("tweets") or []
        if not tweets:
            break
        collected.extend(tweets)
        if not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]
        time.sleep(0.3)  # 礼儀的レート
    return collected[:max_tweets]


def fetch_with_retry(query: str, query_type: str, max_tweets: int, key: str,
                     retries: int = 3) -> list[dict]:
    """twitterapi.io は同一クエリでも空ページを返すことがある(プール由来の非決定性)。
    結果が空なら数回まで再試行する。"""
    for attempt in range(retries):
        tweets = fetch(query, query_type, max_tweets, key)
        if tweets:
            return tweets
        if attempt < retries - 1:
            time.sleep(0.6)
    return []


def collect(genre: str, settings: Settings | None = None, key: str | None = None) -> list[dict]:
    """指定ジャンルの直近トップ投稿を viewCount 降順で返す。

    twitterapi.io の min_faves / -filter:replies は best-effort で揺らぐため、
    クエリは最小限(キーワード+言語+時間窓)に留め、いいね下限・返信除外・直近性は
    クライアント側で確定的にフィルタする。空ページ対策に再試行する。
    """
    settings = settings or get_settings()
    key = key or load_key(settings)
    kws = keywords(genre)
    query = "(" + " OR ".join(kws) + ") lang:ja" + _window_clause(settings.collect_hours)

    tweets = fetch_with_retry(query, "Top", settings.collect_max_tweets, key)
    tweets = [t for t in tweets if not t.get("isReply")]
    if settings.collect_min_faves:
        tweets = [t for t in tweets if _int(t, "likeCount") >= settings.collect_min_faves]
    tweets = _filter_recent(tweets, settings.collect_hours)
    tweets.sort(key=views, reverse=True)
    return tweets
