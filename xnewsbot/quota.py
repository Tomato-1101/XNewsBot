"""LINE 無料枠(200通/月)の残りと、朝の配信に要る通数の計算。

管理UIの残量表示(admin/news.py)と、push を使う機能(AI解説)の通数ガードで共用する。
通数は「push 1回 × 宛先人数」(グループは人数分)。取得は無料 API。
今日の朝の分が要るかは時刻ではなく購読者ごとの配信済み記録(last_morning_on)で決める
(配信が遅れた・失敗して 12:30 以降の自動復旧で送る日も、今日の分を残しておくため)。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlmodel import select

from .models import Subscriber

MEMBER_TTL = 600  # グループ人数を取り直す間隔(秒)。押すたびに人数の API を叩かない
_members: dict[str, tuple[float, int]] = {}


def morning_subscribers(session) -> list[Subscriber]:
    """朝の配信を受ける購読者(初期設定済み・朝が有効・ジャンルあり)。"""
    subs = session.exec(select(Subscriber).where(Subscriber.is_onboarded == True)).all()  # noqa: E712
    return [s for s in subs if s.morning_enabled and s.enabled_genres]


def member_count(messenger, to: str) -> int | None:
    """to へ push 1回の通数。成功した値だけプロセス内で MEMBER_TTL 秒覚える。"""
    hit = _members.get(to)
    if hit and time.monotonic() - hit[0] < MEMBER_TTL:
        return hit[1]
    n = messenger.fetch_member_count(to)
    if n is not None:
        _members[to] = (time.monotonic(), n)
    return n


def fetch_costs(messenger, targets: list[str]) -> dict[str, int] | None:
    """宛先ごとの push 1回の通数(同じ宛先は1回だけ取る)。1件でも取れなければ None。"""
    costs = {}
    for t in dict.fromkeys(targets):
        n = member_count(messenger, t)
        if n is None:
            return None
        costs[t] = n
    return costs


def runs_left(sub: Subscriber, now: datetime) -> int:
    """月末までにこの購読者へ残っている朝の配信回数(今日の分が配信済みなら今日は数えない)。"""
    today = now.astimezone(ZoneInfo(sub.tz)).date()
    next_month = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return (next_month - today).days - (1 if sub.last_morning_on == today else 0)


def morning_need(subs: list[Subscriber], costs: dict[str, int], now: datetime) -> int:
    """月末までの朝の配信に要る通数(購読者ごとの残り回数 × その宛先の通数の合計)。"""
    return sum(runs_left(s, now) * costs[s.push_target] for s in subs)


def push_allowed(remaining: int, reserved: int, push_cost: int, need: int) -> bool:
    """作成中の分(reserved)と今回の push を送っても、月末までの朝の配信に要る通数が残るか。"""
    return remaining - reserved - push_cost >= need


def fetch_extra_push(messenger, session, to: str, now: datetime) -> dict | None:
    """朝の配信以外の push(AI解説など)を to へ送る判定の材料。

    返り値 {"remaining", "need", "cost"}。上限・使用数は1回だけ、人数は宛先ごとに取る。
    1つでも取れないときは None(呼び出し側で安全側=送らない に倒す)。"""
    lu = messenger.fetch_limit_used()
    if not lu:
        return None
    subs = morning_subscribers(session)
    costs = fetch_costs(messenger, [to] + [s.push_target for s in subs])
    if costs is None:
        return None
    return {"remaining": max(lu["limit"] - lu["used"], 0), "need": morning_need(subs, costs, now),
            "cost": costs[to]}
