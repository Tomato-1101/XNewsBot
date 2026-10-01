"""「監視アカウント」ジャンル: 管理画面で登録した X アカウントの投稿を、前回の取り込みから今回までの分を全部集める。

キーワード・いいね下限・件数の絞り込みはしない(本人要望「全部の投稿」)。引用と自分の投稿への返信(スレッドの続き)は
含め、リポストと他人への返信は除く。
要約・取捨はキュレーション側の AI が行う。

取得期間は DB と同じ場所の watch_state.json で持つ:
  {"date": 配信日, "since": その配信日の窓の始まり, "last_until": 取り込んだ収集の時刻}(時刻は UTC の ISO8601)
- 更新は ingest 成功時だけ(収集だけしてキュレーション・配信が失敗した日は、次回に同じ範囲を取り直す)。
- 新しい配信日の収集は last_until から。記録が無ければ直近24時間。古すぎても7日前まで。
- 同じ配信日の再収集(今すぐ更新・リカバリ)は、その日の窓の始まり(since)から取り直す。
  ingest は同じ日のダイジェストを置き換えるので、last_until からだと朝に載せた分が消えるため。
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone

from sqlmodel import Session, select

from . import xclient
from .config import get_settings
from .models import WatchedAccount

# X のハンドル(@ なし)。英数字と _ の15文字まで。
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
# 1アカウントあたりの取得上限(安全弁。twitterapi.io は1件15クレジット)。
MAX_PER_ACCOUNT = 100
DEFAULT_HOURS = 24
MAX_DAYS = 7


def normalize_handle(text: str) -> str | None:
    """入力(@ 付き可)を @ なしのハンドルに。形式が不正なら None。"""
    h = (text or "").strip().removeprefix("@")
    return h if HANDLE_RE.match(h) else None


def enabled_handles(session: Session) -> list[str]:
    """有効な監視アカウントのハンドル(登録順)。"""
    rows = session.exec(select(WatchedAccount).where(WatchedAccount.enabled == True)  # noqa: E712
                        .order_by(WatchedAccount.id)).all()
    return [r.handle for r in rows]


# --- 取得期間の状態 ---

def state_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(get_settings().db_path)), "watch_state.json")


def _iso(v) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_state() -> dict:
    """読めない・壊れているときは stderr に1行出して「記録なし」で続ける(収集は止めない)。"""
    try:
        path = state_path()
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  監視アカウント: watch_state.json を読めず記録なしで続行 ({type(e).__name__}: {e})",
              file=sys.stderr)
        return {}


def save_state(day: date, since: str, until: str) -> None:
    """一時ファイル→os.replace で保存(途中で落ちても壊れたファイルを残さない)。"""
    path = state_path()
    data = {"date": day.isoformat(), "since": since, "last_until": until}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".watch_state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def window(day: date, now: datetime, state: dict) -> tuple[datetime, datetime]:
    """今回の取得期間 (since, until=now)。since は上の規則で決め、7日前より古ければ7日前にする。"""
    since = None
    if state.get("date") == day.isoformat():
        since = _iso(state.get("since"))
    if since is None:
        since = _iso(state.get("last_until"))
    if since is None:
        since = now - timedelta(hours=DEFAULT_HOURS)
    return max(since, now - timedelta(days=MAX_DAYS)), now


# --- 取得 ---

def build_query(handle: str, hours: float) -> str:
    """'from:ClaudeDevs -filter:retweets within_time:24h'。期間の書き方は xclient に合わせる
    (since_time は proxy で 0 件になるため within_time を使い、正確な範囲は createdAt で絞る)。"""
    return f"from:{handle} -filter:retweets" + xclient._window_clause(max(1, math.ceil(hours)))


def _reply_to_other(t: dict, handle: str) -> bool:
    """他人への返信か。返信先は twitterapi.io の inReplyToUsername(無ければ inReplyToUserId と author.id)で見る。
    どちらも空(API の仕様で「may be empty」)なら判定できないので残す(スレッドの続きを落とさない側に倒す)。"""
    if not t.get("isReply"):
        return False
    author = t.get("author") or {}
    to_name = str(t.get("inReplyToUsername") or "").lower()
    if to_name:
        return to_name not in {handle.lower(), str(author.get("userName") or "").lower()}
    to_id, my_id = str(t.get("inReplyToUserId") or ""), str(author.get("id") or "")
    return bool(to_id and my_id and to_id != my_id)


def fetch_account(handle: str, since: datetime, until: datetime, keys: list[str]) -> list[dict]:
    """1アカウントの [since, until] の投稿(リポストと他人への返信を除く・ID 重複除去)。失敗は XClientError を投げる。
    全件が欲しいので並びは新しい順の Latest で取る(Top は人気順で取りこぼす)。"""
    q = build_query(handle, (until - since).total_seconds() / 3600)
    tweets = xclient.fetch_with_retry(q, "Latest", MAX_PER_ACCOUNT, keys)
    if len(tweets) >= MAX_PER_ACCOUNT:  # 新しい順に打ち切るので、窓の古い側の投稿が落ちている
        print(f"  監視アカウント: @{handle} が上限 {MAX_PER_ACCOUNT} 件に達したため、古い投稿の一部を取れていません",
              file=sys.stderr)
    out: list[dict] = []
    seen: set[str] = set()
    for t in tweets:
        if xclient._is_rt(t) or _reply_to_other(t, handle):
            continue
        dt = xclient._parse_created_at(t)
        if dt is not None and not (since <= dt <= until):
            continue
        tid = str(t.get("id") or "")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        t["_official"] = True
        out.append(t)
    return out


def collect(handles: list[str], since: datetime, until: datetime,
            keys: list[str]) -> tuple[list[dict], bool]:
    """全アカウントの投稿を時刻の古い順に。2つ目は全アカウントの取得に成功したか
    (1つでも失敗したら False。呼び出し側は状態を進めず、次回に同じ範囲を取り直す)。"""
    tweets: list[dict] = []
    ok = True
    for h in handles:
        try:
            got = fetch_account(h, since, until, keys)
        except Exception as e:  # 1アカウントの失敗で他のアカウント・収集全体を止めない
            ok = False
            reason = (xclient._fail_reason(e, "") if isinstance(e, xclient.XClientError)
                      else f"{type(e).__name__}: {e}")
            print(f"  監視アカウント: @{h} の取得失敗 ({reason})", file=sys.stderr)
            continue
        print(f"  監視アカウント: @{h} {len(got)} 件", file=sys.stderr)
        tweets += got
    tweets.sort(key=lambda t: xclient._parse_created_at(t) or until)
    return tweets, ok
