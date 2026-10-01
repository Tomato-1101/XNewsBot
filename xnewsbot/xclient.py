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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings, get_settings
from .genres import (accounts, exclude_accounts, excludes, keywords, keywords_en,
                     min_faves, min_faves_en)

BASE_URL = "https://api.twitterapi.io/twitter/tweet/advanced_search"
KEY_FILE = Path(__file__).resolve().parent.parent / ".key"

# 1リクエストのタイムアウト(秒)。twitterapi.io は1ページ数秒だが、混雑時に伸びる。
REQUEST_TIMEOUT = 30
# 一時的エラー(タイムアウト/429/5xx/瞬断)の再試行バックオフ(秒)。指数で伸ばし上限で頭打ち。
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 8.0

# 1ジャンルの X 収集は最大3クエリ(日本語・英語・公式)。twitterapi.io は1ページ約20件なので
# 日本語3+英語2+公式1=約6ページ/ジャンル。旧方式(1クエリ100件=5ページ+空ページ再試行)と同程度に抑える。
JA_MAX_TWEETS = 60
EN_MAX_TWEETS = 40
OFFICIAL_MAX_TWEETS = 20
# 英語圏は桁が大きく、日本語と同じ下限だと使い回しの速報アカウントや雑談が溢れるため高くする
# (いいね OR 表示回数。ジャンルで min_faves_en を書けばいいね下限はそちらが優先)。
EN_MIN_FAVES = 500
EN_MIN_VIEWS_FLOOR = 100_000


class XClientError(RuntimeError):
    """status/detail はキー切替ログに失敗理由を出すため(以前は型名だけで、キー#0 が毎回
    落ちる原因=残高不足を調べるのにわざわざ再現が要った)。鍵の値は含めない。"""

    def __init__(self, message: str, status: int | None = None, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class XClientRetryable(XClientError):
    """一時的エラー(タイムアウト/429/5xx/接続瞬断/応答崩れ)。再試行で回復しうる。

    以前はタイムアウト(socket.timeout=TimeoutError)を _request が捕捉せず、
    fetch_with_retry も「空ページ時のみ」再試行だったため、1回のタイムアウトで
    そのジャンルが0件確定し、混雑時に複数ジャンルが同時に空配信化していた。"""

    def __init__(self, message: str, retry_after: float | None = None,
                 status: int | None = None, detail: str = "") -> None:
        super().__init__(message, status=status, detail=detail)
        self.retry_after = retry_after


def _error_detail(body: str) -> str:
    """エラー応答本文から人が読む要旨を取り出す(JSON の message/msg/error。無ければ本文)。"""
    try:
        j = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.strip()
    if isinstance(j, dict):
        for k in ("message", "msg", "error"):
            if j.get(k):
                return str(j[k])
    return body.strip()


def _fail_reason(e: XClientError, key: str) -> str:
    """キー切替ログ用の失敗理由「HTTP 402: Credits is not enough...」(先頭100字・鍵は伏せる)。"""
    msg = (e.detail or str(e)).replace(key, "***") if key else (e.detail or str(e))
    msg = " ".join(msg.split())[:100]
    return f"HTTP {e.status}: {msg}" if e.status else f"{type(e).__name__}: {msg}"


def _parse_retry_after(value: str | None) -> float | None:
    """HTTP 429 の Retry-After(秒)を float に。日付形式や不正値は None。"""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


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


def _split_keys(raw: str | None) -> list[str]:
    """カンマ/改行区切りの文字列を個別キーのリストに分割(strip・空除去)。"""
    if not raw:
        return []
    return [p.strip() for p in raw.replace(",", "\n").splitlines() if p.strip()]


def load_keys(settings: Settings | None = None) -> list[str]:
    """優先度順(先頭=最優先)の twitterapi.io APIキー配列を返す。重複は順序を保って除去。

    探索順(上が優先): .env(カンマ区切り可) -> 環境変数(同) -> Keychain(単一) -> .key(1行1キー)。
    キーが1つだけなら従来と同じ挙動(後方互換)。全滅で XClientError。
    上位キーが 429/失敗のときだけ下位キーへフォールバックする(fetch_with_retry)。
    """
    settings = settings or get_settings()
    keys: list[str] = []
    keys += _split_keys(settings.twitterapi_io_key)
    keys += _split_keys(os.environ.get("TWITTERAPI_IO_KEY"))
    kc = _keychain_get()
    if kc:
        keys.append(kc)
    if KEY_FILE.exists():
        keys += _split_keys(KEY_FILE.read_text(encoding="utf-8"))
    keys = list(dict.fromkeys(keys))  # 順序保持の重複除去
    if not keys:
        raise XClientError(
            "twitterapi.io の APIキーが見つかりません(.env / 環境変数 / Keychain / .key いずれも未設定)。\n"
            "  Keychain設定例: security add-generic-password -a \"$USER\" -s twitterapi_io_key -w"
        )
    return keys


def load_key(settings: Settings | None = None) -> str:
    """最優先の1キーを返す(後方互換の薄いラッパ)。"""
    return load_keys(settings)[0]


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
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        detail = _error_detail(body)
        # 429(レート超過)/5xx(サーバ側一時障害)は待てば回復しうる → 再試行対象。
        if e.code == 429 or 500 <= e.code < 600:
            retry_after = _parse_retry_after(e.headers.get("Retry-After") if e.headers else None)
            raise XClientRetryable(f"twitterapi.io HTTP {e.code}: {body}", retry_after=retry_after,
                                   status=e.code, detail=detail)
        raise XClientError(f"twitterapi.io HTTP {e.code}: {body}", status=e.code, detail=detail)
    except (TimeoutError, ConnectionError) as e:
        # 読み取りタイムアウト(socket.timeout=TimeoutError)・接続断 → 再試行対象。
        raise XClientRetryable(f"twitterapi.io タイムアウト/接続断: {type(e).__name__}: {e}")
    except urllib.error.URLError as e:
        # URLError は接続失敗(reason に socket.timeout を含むこともある) → 再試行対象。
        raise XClientRetryable(f"twitterapi.io 接続エラー: {e.reason}")
    except (json.JSONDecodeError, ValueError) as e:
        # 途中で切れた/空ボディ等で JSON が壊れた → 再試行対象。
        raise XClientRetryable(f"twitterapi.io 応答パース失敗: {e}")


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


def fetch_with_retry(query: str, query_type: str, max_tweets: int, keys: list[str],
                     retries: int = 3) -> list[dict]:
    """空ページ(プール由来の非決定性)と一時的エラー(タイムアウト/429/5xx)の両方を再試行する。
    複数キーは優先度順(先頭=最優先)。各キーで下記の再試行を尽くし、そのキーが失敗(429含む)
    したら次の優先度キーへフォールバックする。

    - XClientRetryable は指数バックオフ(429 は Retry-After 尊重)で再試行。
    - 空結果も従来どおり再試行。空のまま完走したら「該当ツイート無し」として確定し、
      下位キーへはフォールバックしない(同じデータソースなので無駄・下位キーを無駄に消費しない)。
    - そのキーで最終試行でも一時的エラー、または恒久エラー(4xx)なら次キーへ。
    - 全キーが失敗したら最後の例外を投げ、呼び出し側(pipeline._one)がそのジャンルだけ
      空として扱う(他ジャンルの収集は止めない)。
    """
    last_exc: XClientError | None = None
    for i, key in enumerate(keys):
        try:
            for attempt in range(retries):
                try:
                    tweets = fetch(query, query_type, max_tweets, key)
                except XClientRetryable as e:
                    if attempt >= retries - 1:
                        raise
                    delay = e.retry_after if e.retry_after is not None else RETRY_BASE_DELAY * (2 ** attempt)
                    time.sleep(min(delay, RETRY_MAX_DELAY))
                    continue
                if tweets:
                    return tweets
                if attempt < retries - 1:
                    time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            return []  # このキーで完走・結果は空(=該当ツイート無し)。フォールバックしない。
        except XClientError as e:  # XClientRetryable(再試行尽き)も恒久4xxもここで捕捉
            last_exc = e
            if i < len(keys) - 1:
                # 鍵の値は絶対に出さない。index と失敗理由のみログ(deliver.sh のログに乗る)。
                print(f"  キー#{i} 失敗 → 次キーへ ({type(e).__name__} {_fail_reason(e, key)})",
                      file=sys.stderr)
            continue
    assert last_exc is not None  # keys は load_keys で非空保証
    raise last_exc


def _term(t: str) -> str:
    """空白を含む語はフレーズ検索にする("Claude Code" が Claude AND Code に分解されて広がらないように)。"""
    t = t.strip()
    return f'"{t}"' if any(c.isspace() for c in t) else t


def build_keyword_query(terms: list[str], lang: str, hours: float | None,
                        exclude: list[str] = ()) -> str:
    """'(kw1 OR "kw 2") lang:ja within_time:24h -除外語' を組み立てる。"""
    q = "(" + " OR ".join(_term(t) for t in terms) + ")"
    if lang:
        q += f" lang:{lang}"
    q += _window_clause(hours)
    # 除外語はサーバ側(best-effort)とクライアント側(確定的)の両方で効かせる
    for term in exclude:
        q += f" -{_term(term)}"
    return q


def build_official_query(handles: list[str], hours: float | None) -> str:
    """'(from:A OR from:B) within_time:24h'。公式・一次情報は言語もいいねも問わず拾う。"""
    return "(" + " OR ".join(f"from:{h}" for h in handles) + ")" + _window_clause(hours)


_URL_RE = re.compile(r"https?://\S+")


def _norm_text(text: str | None) -> str:
    """重複判定用の正規化本文(URL と空白を除いた先頭60字)。転載・使い回しの同文を1件にする。"""
    s = _URL_RE.sub("", text or "")
    return "".join(s.split()).lower()[:60]


def _is_rt(t: dict) -> bool:
    return (t.get("text") or "").startswith("RT @")


def _filter_engagement(tweets: list[dict], min_f: int | None, min_v: int | None) -> list[dict]:
    """いいね下限 OR 表示回数下限。伸びる前の速報(高view・低like)を取りこぼさない。"""
    if not min_f:
        return tweets
    return [t for t in tweets
            if _int(t, "likeCount") >= min_f or (min_v and _int(t, "viewCount") >= min_v)]


def merge_tweets(official: list[dict], others: list[dict],
                 deny_accounts: list[str] = ()) -> list[dict]:
    """公式を先頭→残りを viewCount 降順に並べ、RT・除外アカウント・重複(ID/正規化本文)を落とす。

    各群を先に viewCount 降順にしてから重複判定するので、同文の使い回しは表示回数の多い1件が残る。
    公式群を先に通すので、公式投稿と同じものがキーワード検索にも出たら公式扱いで残る。
    """
    deny = {a.lower().lstrip("@") for a in deny_accounts}
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    out: list[dict] = []
    for t in sorted(official, key=views, reverse=True) + sorted(others, key=views, reverse=True):
        if _is_rt(t):
            continue
        if ((t.get("author") or {}).get("userName") or "").lower() in deny:
            continue
        tid = str(t.get("id") or "")
        nt = _norm_text(t.get("text"))
        if (tid and tid in seen_ids) or (nt and nt in seen_text):
            continue
        if tid:
            seen_ids.add(tid)
        if nt:
            seen_text.add(nt)
        out.append(t)
    return out


def collect(genre: str, settings: Settings | None = None, keys: list[str] | None = None) -> list[dict]:
    """指定ジャンルの直近24hの投稿を「公式(_official=True)を先頭→残りを viewCount 降順」で返す。

    最大3クエリ: 日本語(keywords, lang:ja)・英語(keywords_en, lang:en, いいね下限高め)・
    公式(accounts の from:, いいね下限なし)。twitterapi.io の min_faves / -filter:replies は
    best-effort で揺らぐため、いいね下限・返信除外・直近性はクライアント側で確定的にフィルタする。
    1クエリの失敗では他のクエリを捨てない。全クエリ失敗のときだけ例外を投げる
    (呼び出し側 pipeline._one がそのジャンルの X を空として扱う)。
    """
    settings = settings or get_settings()
    keys = keys or load_keys(settings)
    hours = settings.collect_hours
    ex = excludes(genre)
    # 管理UIの「1ジャンルの取得上限」(collect_max_tweets)は各クエリの上限としても効かせる(費用の歯止め)。
    cap = settings.collect_max_tweets
    gmin = min_faves(genre)
    min_ja = gmin if gmin is not None else settings.collect_min_faves
    min_v = settings.collect_min_views_floor
    gmin_en = min_faves_en(genre)
    min_en = gmin_en if gmin_en is not None else max(min_ja or 0, EN_MIN_FAVES)
    min_v_en = max(min_v or 0, EN_MIN_VIEWS_FLOOR)

    # (名前, クエリ, 取得上限, 公式か, いいね下限, 表示回数下限)
    plans: list[tuple[str, str, int, bool, int | None, int | None]] = []
    if keywords(genre):
        plans.append(("日本語", build_keyword_query(keywords(genre), "ja", hours, ex),
                      min(JA_MAX_TWEETS, cap), False, min_ja, min_v))
    if keywords_en(genre):
        plans.append(("英語", build_keyword_query(keywords_en(genre), "en", hours, ex),
                      min(EN_MAX_TWEETS, cap), False, min_en, min_v_en))
    if accounts(genre):
        plans.append(("公式", build_official_query(accounts(genre), hours),
                      min(OFFICIAL_MAX_TWEETS, cap), True, None, None))

    official: list[dict] = []
    others: list[dict] = []
    errors: list[XClientError] = []
    for name, query, mx, is_official, min_f, mv in plans:
        try:
            tweets = fetch_with_retry(query, "Top", mx, keys)
        except XClientError as e:
            errors.append(e)
            print(f"  {genre}: X {name}クエリ失敗 ({_fail_reason(e, '')})", file=sys.stderr)
            continue
        tweets = [t for t in tweets if not t.get("isReply")]
        if is_official:
            for t in tweets:
                t["_official"] = True
            official += _filter_recent(tweets, hours)
            continue
        if ex:
            tweets = [t for t in tweets if not any(term in (t.get("text") or "") for term in ex)]
        tweets = _filter_engagement(tweets, min_f, mv)
        others += _filter_recent(tweets, hours)
    if plans and len(errors) == len(plans):
        raise errors[-1]
    return merge_tweets(official, others, exclude_accounts(genre))
