"""ニュース閲覧(読み取り専用)。DB の GenreDigest + NewsItem を日付/スロットで表示する。"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import select

from ..config import get_settings
from ..db import get_session
from ..digest import assemble_for_genres
from ..genres import GENRE_KEYS, GENRES
from ..models import GenreDigest, NewsItem
from ..scheduler import slot_for_now
from .web import require_auth, templates

router = APIRouter()

SLOT_LABELS = {"morning": "朝", "evening": "夜"}


def _item_dict(it: NewsItem) -> dict:
    """テンプレート用に NewsItem を素の dict 化(セッション外で参照しても安全に)。"""
    tags = [GENRES[g]["label"] for g in (it.genres or []) if g in GENRES]
    if not tags and it.genre in GENRES:
        tags = [GENRES[it.genre]["label"]]
    return {
        "title": it.title,
        "summary": it.summary,
        "detail": it.detail or "",
        "tags": tags,
        "source_urls": it.source_urls or [],
        "top_view_count": it.top_view_count or 0,
    }


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
        if date_str:
            try:
                local_date = date.fromisoformat(date_str)
            except ValueError:
                local_date = avail_dates[0] if avail_dates else now_local.date()
        else:
            local_date = avail_dates[0] if avail_dates else now_local.date()
        cur_slot = slot if slot in ("morning", "evening") else slot_for_now(now_local)

        grouped = assemble_for_genres(session, GENRE_KEYS, local_date, cur_slot)
        sections = []
        for g in GENRE_KEYS:
            items = grouped.get(g, [])
            if not items:
                continue
            big = [_item_dict(it) for it in items if it.importance == "big"]
            small = [_item_dict(it) for it in items if it.importance == "small"]
            sections.append({"key": g, "label": GENRES[g]["label"], "big": big, "small": small})

    return templates.TemplateResponse(
        request,
        "news.html",
        {
            "active": "news",
            "sections": sections,
            "avail_dates": [d.isoformat() for d in avail_dates],
            "cur_date": local_date.isoformat(),
            "cur_slot": cur_slot,
            "slot_labels": SLOT_LABELS,
            "total": sum(len(s["big"]) + len(s["small"]) for s in sections),
        },
    )
