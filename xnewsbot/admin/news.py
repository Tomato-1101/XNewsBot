"""ニュース閲覧(読み取り専用)。DB の GenreDigest + NewsItem を日付/スロットで表示する。

LINE 配信と同じ並び(要点 → ジャンルごとの主なニュース → ほかのニュース)に、市況・今日の予定・
残り使用量(X のクレジットと LINE の今月の通数)を添えて1ページで見せる。
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import select

from .. import digest, explain, quota
from .. import line_client as lc
from ..config import get_settings
from ..db import get_session
from ..digest import assemble_for_genres
from ..genres import GENRE_KEYS, GENRES
from ..models import GenreDigest, NewsItem, XUsageSnapshot
from ..scheduler import slot_for_now
from .web import require_auth, templates

log = logging.getLogger("xnewsbot.admin")
router = APIRouter()

SLOT_LABELS = {"morning": "朝", "evening": "夜"}
EDITION_LABELS = {"morning": "朝刊", "evening": "夕刊"}
X_HISTORY_DAYS = 14   # 使用量グラフに出す配信回数
X_AVG_RUNS = 7        # 「あと約N日」の平均に使う直近の配信回数
LINE_QUOTA_TTL = 600  # LINE の残り通数を取り直す間隔(秒)。ページを開くたびに API を叩かない
_line_cache: dict = {"at": float("-inf"), "value": None}


def _host(url: str) -> str:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host.removeprefix("www.") or "元記事"


def _sources(it: NewsItem) -> list[dict]:
    """出典の表示名とリンク(媒体名ごとに1つ)。出典に URL が無い旧データは source_urls のドメイン名で出す。"""
    out, seen = [], set()
    for src in it.source_tweets or []:
        name, url = lc._source_name(src), lc._safe_uri(src.get("url"))
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "url": url})
    if not any(o["url"] for o in out):
        out = [{"name": _host(u), "url": u} for u in dict.fromkeys(
            u for u in (it.source_urls or []) if lc._safe_uri(u))] or out
    return out


def _item_dict(it: NewsItem, now: datetime) -> dict:
    """テンプレート用に NewsItem を素の dict 化(セッション外で参照しても安全に)。"""
    tags = [GENRES[g]["label"] for g in (it.genres or []) if g in GENRES and g != it.genre]
    return {
        "id": it.id,
        "title": it.title,
        "summary": it.summary,
        "detail": it.detail or "",
        "tags": tags,
        "ago": lc._ago(it, now),
        "sources": _sources(it),
        "explanation": None,  # AI解説(作成済みのときだけ文字列)
        "top_view_count": it.top_view_count or 0,
    }


def _market_rows(market: list[dict]) -> list[dict]:
    """市況の表示行(LINE の要点バブルと同じ書式)。終値が無い行は出さない。"""
    rows = []
    for m in market:
        label = str(m.get("label") or m.get("key") or "")
        try:
            close = float(m.get("close"))
        except (TypeError, ValueError):
            continue
        if not label:
            continue
        kind = m.get("kind")
        value = (f"{close:,.2f}円" if kind == "fx" else f"{close:.2f}%" if kind == "yield"
                 else f"${close:,.0f}" if kind == "crypto" else f"{close:,.0f}")
        raw, unit = (m.get("change"), "pt") if kind == "yield" else (m.get("change_pct"), "%")
        try:
            v = round(float(raw), 2) or 0.0
            change, trend = f"{v:+.2f}{unit}", "up" if v > 0 else "down" if v < 0 else "flat"
        except (TypeError, ValueError):
            change, trend = "—", "flat"
        rows.append({"label": label, "value": value, "change": change, "trend": trend})
    return rows


def _schedule_rows(schedule: list[dict], now: datetime) -> list[dict]:
    """今日の予定(時刻順・時刻なしは最後)。終わった予定も消さずに薄く出す(後から結果を見返せるように)。"""
    rows = []
    for ev in schedule:
        if not ev.get("name"):
            continue
        at = lc._parse_time(ev["at"]) if ev.get("at") else None
        rows.append({
            "at": at,
            "time": str(ev.get("time_label") or "未定"),
            "name": str(ev["name"]),
            "forecast": ev.get("forecast") or "",
            "previous": ev.get("previous") or "",
            "result": ev.get("result") or "",
            "major": int(ev.get("importance") or 0) >= 5,
            "past": bool(at and at < now),
        })
    rows.sort(key=lambda r: (r["at"] is None, r["at"] or now))
    return rows


def _x_usage(session) -> dict | None:
    """X(twitterapi.io)の残りクレジット。最新の記録と、直近の配信ごとの消費(グラフ用)。"""
    snaps = session.exec(
        select(XUsageSnapshot).order_by(XUsageSnapshot.digest_date.desc(), XUsageSnapshot.id.desc())
    ).all()
    latest_by_run: dict[tuple, XUsageSnapshot] = {}
    for s in snaps:  # 同じ配信回の記録が複数あれば最新の1件だけ
        latest_by_run.setdefault((s.digest_date, s.slot), s)
    runs = list(latest_by_run.values())[:X_HISTORY_DAYS]
    if not runs:
        return None
    last = runs[0]
    recent = [r.used for r in runs[:X_AVG_RUNS] if r.used > 0]
    avg = sum(recent) / len(recent) if recent else 0
    peak = max((r.used for r in runs), default=0) or 1
    return {
        "remaining": last.remaining,
        "usd": last.remaining / lc.CREDITS_PER_USD,
        "last_used": last.used,
        "as_of": last.digest_date,
        "avg": round(avg),
        "days_left": int(last.remaining // avg) if avg else None,
        "low": bool(avg) and last.remaining // avg < lc.X_USAGE_WARN_DAYS,
        "history": [{"date": r.digest_date, "used": r.used, "pct": round(r.used / peak * 100)}
                    for r in reversed(runs)],
    }


def _fetch_line_quota(session) -> dict | None:
    """今月の LINE 無料枠 {"limit", "used", "costs": {宛先: push 1回の通数}}(朝の配信の宛先ごと)。"""
    token = get_settings().line_channel_access_token
    if not token:
        return None
    subs = quota.morning_subscribers(session)
    if not subs:
        return None
    m = lc.LineMessenger(token)
    lu = m.fetch_limit_used()
    costs = quota.fetch_costs(m, [s.push_target for s in subs]) if lu else None
    return {**lu, "costs": costs} if costs is not None else None


def _line_usage(session, now: datetime) -> dict | None:
    """LINE の今月の残り通数と、月末まで毎朝配信したときに足りるか。

    要る通数は AI解説の通数ガードと同じ quota.morning_need(購読者ごとの配信済み記録で今日を数えるか決める)。"""
    subs = quota.morning_subscribers(session)
    if not subs:
        return None
    targets = {s.push_target for s in subs}
    cached = _line_cache["value"]
    # 取得失敗(None)も TTL の間は覚えておく(LINE が落ちているときにページを開くたび待たせない)。
    # 覚えている人数に無い宛先が増えたときだけは取り直す
    if (time.monotonic() - _line_cache["at"] > LINE_QUOTA_TTL
            or (cached and not targets <= cached["costs"].keys())):
        try:
            _line_cache["value"] = _fetch_line_quota(session)
        except Exception:  # noqa: BLE001 — 表示のためだけなのでページは出す
            log.warning("LINE の残り通数を取得できませんでした", exc_info=True)
            _line_cache["value"] = None
        _line_cache["at"] = time.monotonic()
    q = _line_cache["value"]
    if not q or not targets <= q["costs"].keys():
        return None
    cost = sum(q["costs"][s.push_target] for s in subs)  # 1回の配信で消費する通数
    if cost <= 0:
        return None
    remaining = max(q["limit"] - q["used"], 0)
    # 月末までの残りの配信回数(今日の分が配信済みの購読者は今日を数えない)
    days_left = max(quota.runs_left(s, now) for s in subs)
    need = quota.morning_need(subs, q["costs"], now)
    return {
        "limit": q["limit"], "used": q["used"], "remaining": remaining, "cost": cost,
        "runs": remaining // cost, "days_left": days_left, "need": need,
        "enough": remaining >= need,
        "pct_used": min(round(q["used"] / q["limit"] * 100), 100) if q["limit"] else 100,
        "low": remaining // cost < lc.LINE_QUOTA_WARN_RUNS,
    }


def usage_context(session, now: datetime) -> dict:
    return {"x": _x_usage(session), "line": _line_usage(session, now)}


def _jp_date(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日（{lc._WEEKDAYS[d.weekday()]}）"


@router.get("/", response_class=HTMLResponse)
def news_index(
    request: Request,
    date_str: str | None = None,
    slot: str | None = None,
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    settings = get_settings()
    now_local = datetime.now(ZoneInfo(settings.default_tz))

    with get_session() as session:
        avail_dates = sorted(
            set(session.exec(select(GenreDigest.digest_date).distinct())),
            reverse=True,
        )
        # 日付: 指定 > 最新のダイジェスト日 > 今日
        local_date = None
        if date_str:
            try:
                local_date = date.fromisoformat(date_str)
            except ValueError:
                pass
        if local_date is None:
            local_date = avail_dates[0] if avail_dates else now_local.date()
        avail_slots = [s for s in SLOT_LABELS if s in set(session.exec(
            select(GenreDigest.slot).where(GenreDigest.digest_date == local_date)))]
        if slot in SLOT_LABELS:
            cur_slot = slot
        else:  # 指定が無ければ、その日に記事のあるスロット(夜は配信停止中なので通常は朝)
            guess = slot_for_now(now_local)
            cur_slot = guess if guess in avail_slots or not avail_slots else avail_slots[0]

        grouped = assemble_for_genres(session, GENRE_KEYS, local_date, cur_slot)
        grouped = {g: items for g, items in grouped.items() if items}
        main, others = lc._split_main_others(grouped)
        points = [{"id": it.id, "title": it.title, "genre": GENRES.get(it.genre, {}).get("label", it.genre),
                   "color": lc._genre_color(it.genre)} for it in lc._pick_points(grouped)]
        sections = [{
            "key": g,
            "label": GENRES[g]["label"] if g in GENRES else g,
            "color": lc._genre_color(g),
            "main": [_item_dict(it, now_local) for it in main.get(g, [])],
            "others": [_item_dict(it, now_local) for it in others.get(g, [])],
        } for g in grouped]
        done = explain.done_texts(session, {it.id: it.title for items in grouped.values() for it in items})
        for s in sections:
            for d in s["main"] + s["others"]:
                d["explanation"] = done.get(d["id"])
        market = _market_rows(digest.get_market(session, local_date, cur_slot))
        schedule = _schedule_rows(digest.get_schedule(session, local_date, cur_slot), now_local)
        usage = usage_context(session, now_local)

    idx = avail_dates.index(local_date) if local_date in avail_dates else -1
    newer = avail_dates[idx - 1].isoformat() if idx > 0 else None
    older = avail_dates[idx + 1].isoformat() if 0 <= idx < len(avail_dates) - 1 else None
    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "active": "news",
            "sections": sections,
            "points": points,
            "market": market,
            "schedule": schedule,
            "usage": usage,
            "avail_dates": [d.isoformat() for d in avail_dates],
            "avail_slots": avail_slots,
            "cur_date": local_date.isoformat(),
            "cur_date_jp": _jp_date(local_date),
            "edition": EDITION_LABELS[cur_slot],
            "is_latest": idx == 0,
            "newer": newer,
            "older": older,
            "cur_slot": cur_slot,
            "slot_labels": SLOT_LABELS,
            "total": sum(len(s["main"]) + len(s["others"]) for s in sections),
        },
    )


# --- AI解説(管理UIから押されたとき。LINE には送らないので通数ガードは不要) ---

def _start_explain(item_id: int, token: str) -> None:
    """生成は1〜2分かかるので裏のスレッドで回し、画面は GET /explain/{id} で状態を見に来る。"""
    threading.Thread(target=explain.run_job, args=(item_id, token),
                     kwargs={"session_factory": get_session}, daemon=True,
                     name=f"explain-{item_id}").start()


def _refused(error: str) -> dict:
    """受け付けなかったときの状態 JSON(画面は error をそのまま出し、ボタンを戻す)。"""
    return {"status": "refused", "text": "", "error": error, "elapsed": None}


@router.post("/explain/{item_id}")
def explain_create(item_id: int, _user: str = Depends(require_auth)) -> dict:
    """作成済みならその本文、作成中ならその状態、無ければ確保して生成を始める。"""
    with get_session() as session:
        item = session.get(NewsItem, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="記事が見つかりません")
        if explain.status_of(explain.get(session, item_id, item.title)) not in ("done", "running"):
            if explain.in_quiet_hours(datetime.now(ZoneInfo(get_settings().default_tz))):
                return _refused("朝の配信の準備中(7:00〜8:30)はAI解説を受け付けていません。"
                                "8:30以降にもう一度押してください")
            token = explain.claim(session, item_id, title=item.title)
            if token:
                _start_explain(item_id, token)
            elif explain.status_of(explain.get(session, item_id, item.title)) != "running":
                return _refused("いまほかのAI解説を作っています。少し後にもう一度押してください")
            # 取れずに running なら別の押下(LINE 含む)が作成中なので、その状態を返す
        return explain.state(explain.get(session, item_id, item.title))


@router.get("/explain/{item_id}")
def explain_status(item_id: int, _user: str = Depends(require_auth)) -> dict:
    with get_session() as session:
        item = session.get(NewsItem, item_id)
        return explain.state(explain.get(session, item_id, item.title) if item else None)
