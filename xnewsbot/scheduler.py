"""配信スケジューラ。

- tick(): 配信時刻を過ぎ & 当日未配信の購読者を検出し配信(catch-up方式)。
  Macスリープで定刻を逃しても、復帰後の次tickで当日分を配信する。
- run_now(): 「今すぐ配信」用。自前セッションで1人に即配信。
- make_deliver_now(): onboarding に渡す deliver_now コールバック(別スレッドで run_now)。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import digest
from . import line_client as lc
from .config import Settings, get_settings
from .curator import Curator
from .db import get_session
from .models import Subscriber

log = logging.getLogger("xnewsbot.scheduler")


def _now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def is_due(sub: Subscriber, now_local: datetime) -> bool:
    """now_local(購読者tzの現在時刻)時点で配信すべきか。"""
    if not sub.is_onboarded or not sub.enabled_genres:
        return False
    if sub.last_delivered_on == now_local.date():
        return False
    sched = sub.deliver_hour * 60 + sub.deliver_minute
    cur = now_local.hour * 60 + now_local.minute
    return cur >= sched


def due_subscribers(session: Session, now_provider=_now_in) -> list[Subscriber]:
    subs = session.exec(select(Subscriber).where(Subscriber.is_onboarded == True)).all()  # noqa: E712
    return [s for s in subs if is_due(s, now_provider(s.tz))]


def deliver_to_subscriber(
    session: Session,
    sub: Subscriber,
    *,
    curator: Curator,
    messenger,
    settings: Settings | None = None,
    now_local: datetime | None = None,
    greeting: bool = True,
    key: str | None = None,
) -> list[dict]:
    """購読者の有効ジャンルから当日ダイジェストを組み立てて push し、配信日を記録する。"""
    settings = settings or get_settings()
    now_local = now_local or _now_in(sub.tz)
    local_date = now_local.date()
    grouped = digest.assemble_for_genres(
        session, sub.enabled_genres, local_date, curator=curator, settings=settings, key=key
    )
    specs = lc.digest_specs(grouped, greeting=greeting)
    messenger.push(sub.line_user_id, specs)
    sub.last_delivered_on = local_date
    session.add(sub)
    session.commit()
    return specs


def _build_messenger(settings: Settings):
    if not settings.line_channel_access_token:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN が未設定。配信をスキップ。")
        return None
    return lc.LineMessenger(settings.line_channel_access_token)


def tick() -> None:
    """常駐スケジューラから定期実行される。例外は握り潰してデーモンを止めない。"""
    settings = get_settings()
    try:
        with get_session() as session:
            due = due_subscribers(session)
            if not due:
                return
            messenger = _build_messenger(settings)
            if messenger is None:
                return
            curator = Curator(settings)
            for sub in due:
                try:
                    deliver_to_subscriber(
                        session, sub, curator=curator, messenger=messenger, settings=settings
                    )
                    log.info("配信完了 user=%s genres=%s", sub.line_user_id, sub.enabled_genres)
                except Exception:
                    log.exception("配信に失敗 user=%s", sub.line_user_id)
    except Exception:
        log.exception("tick で例外")


def run_now(line_user_id: str, settings: Settings | None = None) -> None:
    """「今すぐ配信」。自前セッションで1人に配信する(別スレッドから呼ばれる想定)。"""
    settings = settings or get_settings()
    messenger = _build_messenger(settings)
    if messenger is None:
        return
    curator = Curator(settings)
    try:
        with get_session() as session:
            sub = session.exec(
                select(Subscriber).where(Subscriber.line_user_id == line_user_id)
            ).first()
            if not sub or not sub.enabled_genres:
                messenger.push(line_user_id, [lc.text_spec("先にジャンルを設定してください。")])
                return
            deliver_to_subscriber(
                session, sub, curator=curator, messenger=messenger, settings=settings
            )
    except Exception:
        log.exception("run_now で例外 user=%s", line_user_id)
        try:
            messenger.push(line_user_id, [lc.text_spec("ニュースの取得中にエラーが発生しました。")])
        except Exception:
            pass


def make_deliver_now(settings: Settings | None = None):
    """onboarding に渡す deliver_now。重い配信を別スレッドで実行し webhook をブロックしない。"""
    def _deliver(sub: Subscriber) -> None:
        uid = sub.line_user_id
        threading.Thread(target=run_now, args=(uid,), daemon=True).start()
    return _deliver
