"""LINE Webhook 受信。署名検証 → イベント正規化 → onboarding.handle_event。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ..config import get_settings
from ..db import get_session
from ..line_client import LineMessenger
from ..onboarding import handle_event
from ..scheduler import make_deliver_now

log = logging.getLogger("xnewsbot.webhook")

router = APIRouter()


def _normalize(ev) -> dict | None:
    """line-bot-sdk(v3) のイベントを正規化 dict に変換(扱わない種別は None)。

    target_id: グループ/ルームに来たイベントならそのID(配信先候補)。1:1なら None。
    join: ボットがグループ/ルームに追加された(user_id は無い)。"""
    from linebot.v3.webhooks import (
        FollowEvent,
        JoinEvent,
        MessageEvent,
        PostbackEvent,
        TextMessageContent,
    )

    src = getattr(ev, "source", None)
    uid = getattr(src, "user_id", None)
    gid = getattr(src, "group_id", None)
    rid = getattr(src, "room_id", None)
    token = getattr(ev, "reply_token", "") or ""
    base = {"line_user_id": uid, "display_name": None,
            "text": "", "data": "", "reply_token": token,
            "target_id": gid or rid,
            "source_type": "group" if gid else ("room" if rid else "user")}

    if isinstance(ev, JoinEvent):  # ボットがグループ/ルームに参加(user_id 無し)
        return {**base, "kind": "join"}
    if not uid:
        return None
    if isinstance(ev, FollowEvent):
        return {**base, "kind": "follow"}
    if isinstance(ev, MessageEvent) and isinstance(ev.message, TextMessageContent):
        return {**base, "kind": "message", "text": ev.message.text}
    if isinstance(ev, PostbackEvent):
        return {**base, "kind": "postback", "data": ev.postback.data}
    return None


def _process(body: str, signature: str) -> None:
    from linebot.v3 import WebhookParser
    from linebot.v3.exceptions import InvalidSignatureError

    settings = get_settings()
    parser = WebhookParser(settings.line_channel_secret)
    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="署名が不正です")

    messenger = LineMessenger(settings.line_channel_access_token)
    deliver_now = make_deliver_now(settings)
    with get_session() as session:
        for ev in events:
            norm = _normalize(ev)
            if not norm:
                continue
            try:
                handle_event(session, messenger, norm, deliver_now=deliver_now)
            except Exception:
                log.exception("イベント処理に失敗: %s", norm.get("kind"))


@router.post("/line/callback")
async def callback(request: Request, x_line_signature: str = Header(default="")) -> str:
    settings = get_settings()
    if not settings.line_channel_secret or not settings.line_channel_access_token:
        raise HTTPException(status_code=503, detail="LINE のトークン/シークレットが未設定です。")
    body = (await request.body()).decode("utf-8")
    # 署名検証・返信・LINE API 呼び出しは同期I/O。イベントループをブロックしないよう別スレッドで。
    await run_in_threadpool(_process, body, x_line_signature)
    return "OK"
