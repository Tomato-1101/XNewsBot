"""配信スケジューラ(LLMは呼ばない)。

キュレーションは Claude Code の定期実行が DB に取り込み済み。ここは DB の既存ダイジェストを
購読者の時刻に合わせて push するだけ。

- tick(): 配信時刻を過ぎ & 当日未配信 & 当日ダイジェストが揃っている購読者へ配信(catch-up方式)。
- run_now(): 「今すぐ配信」。当日ダイジェストがあれば即 push、無ければ準備中を返す。
- make_deliver_now(): onboarding に渡す deliver_now(別スレッドで run_now)。
"""

from __future__ import annotations

import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import digest
from . import line_client as lc
from .config import Settings, get_settings
from .db import get_session
from .genres import display_genres
from .models import SLOTS, Subscriber

# 「今すぐ配信」で起動するリアルタイム配信スクリプト(収集→Claudeキュレーション→送信)
DELIVER_SH = Path(__file__).resolve().parent.parent / "ops" / "deliver.sh"

log = logging.getLogger("xnewsbot.scheduler")

# 「今すぐ配信」やスロット未指定時に、現在時刻からスロットを推定する境界(この時刻以降は夜扱い)
EVENING_BOUNDARY_HOUR = 15


def _now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def slot_for_now(now_local: datetime) -> str:
    """現在時刻から朝/夜スロットを推定(今すぐ配信のフォールバック用)。"""
    return "evening" if now_local.hour >= EVENING_BOUNDARY_HOUR else "morning"


def missing_for_delivery(session: Session, sub: Subscriber, local_date, slot: str) -> list[str]:
    """配信前の揃い判定。実際に配信するのは購読ジャンル+常時ジャンル(特大)なので、
    判定も同じ集合で行う(購読分だけ見ると特大が欠けたまま配信されてしまう)。"""
    return digest.missing_genres(session, display_genres(sub.enabled_genres), local_date, slot)


def is_due(sub: Subscriber, now_local: datetime, slot: str) -> bool:
    """now_local(購読者tz)時点で当該スロットを配信すべきか(有効・時刻到来・当日未配信)。"""
    if not sub.is_onboarded or not sub.enabled_genres:
        return False
    if not sub.slot_enabled(slot):
        return False
    if sub.last_on(slot) == now_local.date():
        return False
    h, m = sub.slot_time(slot)
    return (now_local.hour * 60 + now_local.minute) >= (h * 60 + m)


def due_subscribers(session: Session, now_provider=_now_in) -> list[tuple[Subscriber, str]]:
    """配信すべき (購読者, スロット) の組を返す。"""
    subs = session.exec(select(Subscriber).where(Subscriber.is_onboarded == True)).all()  # noqa: E712
    out: list[tuple[Subscriber, str]] = []
    for s in subs:
        now_local = now_provider(s.tz)
        for slot in SLOTS:
            if is_due(s, now_local, slot):
                out.append((s, slot))
    return out


def deliver_to_subscriber(
    session: Session,
    sub: Subscriber,
    slot: str,
    *,
    messenger,
    now_local: datetime | None = None,
    greeting: bool = True,
    mark_delivered: bool = True,
) -> list[dict]:
    """DB の既存ダイジェスト(当日・当スロット)から購読者へ push し、配信日を記録する。"""
    now_local = now_local or _now_in(sub.tz)
    local_date = now_local.date()
    grouped = digest.assemble_for_genres(
        session, display_genres(sub.enabled_genres), local_date, slot
    )
    specs = lc.digest_specs(grouped, greeting=greeting, slot=slot)
    messenger.push(sub.push_target, specs)  # push_to(グループ等)があればそこへ、無ければ1:1
    if mark_delivered:
        sub.set_last_on(slot, local_date)
        session.add(sub)
        session.commit()
    return specs


def _build_messenger(settings: Settings):
    if not settings.line_channel_access_token:
        log.warning("LINE_CHANNEL_ACCESS_TOKEN が未設定。配信をスキップ。")
        return None
    return lc.LineMessenger(settings.line_channel_access_token)


def tick() -> None:
    """常駐スケジューラから定期実行される。例外は握り潰してデーモンを止めない。
    当日・当スロットのダイジェストが揃っている購読者にだけ配信する(未完なら次tickへ持ち越し)。"""
    settings = get_settings()
    try:
        with get_session() as session:
            due = due_subscribers(session)
            if not due:
                return
            messenger = _build_messenger(settings)
            if messenger is None:
                return
            for sub, slot in due:
                now_local = _now_in(sub.tz)
                if missing_for_delivery(session, sub, now_local.date(), slot):
                    continue  # キュレーション未完。次の点検まで待つ。
                try:
                    deliver_to_subscriber(session, sub, slot, messenger=messenger, now_local=now_local)
                    log.info("配信完了 user=%s slot=%s genres=%s",
                             sub.line_user_id, slot, sub.enabled_genres)
                except Exception:
                    log.exception("配信に失敗 user=%s slot=%s", sub.line_user_id, slot)
    except Exception:
        log.exception("tick で例外")


def run_now(line_user_id: str, settings: Settings | None = None) -> None:
    """「今すぐ配信」。現在時刻のスロット → 無ければ他スロットの当日ダイジェストを push。
    どちらも無ければ準備中を返す(別スレッドから呼ばれる)。配信日は記録しない。"""
    settings = settings or get_settings()
    messenger = _build_messenger(settings)
    if messenger is None:
        return
    try:
        with get_session() as session:
            sub = session.exec(
                select(Subscriber).where(Subscriber.line_user_id == line_user_id)
            ).first()
            if not sub or not sub.enabled_genres:
                messenger.push(line_user_id, [lc.text_spec("先にジャンルを設定してください。")])
                return
            now_local = _now_in(sub.tz)
            primary = slot_for_now(now_local)
            order = [primary, "evening" if primary == "morning" else "morning"]
            for slot in order:
                if not missing_for_delivery(session, sub, now_local.date(), slot):
                    deliver_to_subscriber(session, sub, slot, messenger=messenger,
                                          now_local=now_local, mark_delivered=False)
                    return
            messenger.push(line_user_id, [lc.text_spec(
                "本日のニュースはまだ準備中です。"
                f"朝 {sub.morning_hour:02d}:{sub.morning_minute:02d} / "
                f"夜 {sub.evening_hour:02d}:{sub.evening_minute:02d} 頃にお届けします。")])
    except Exception:
        log.exception("run_now で例外 user=%s", line_user_id)


def make_deliver_now(settings: Settings | None = None):
    """onboarding に渡す deliver_now。

    「今すぐ配信」は定刻配信と同じく **その場の最新** を届けたいので、収集→Claude
    キュレーション→送信を行う deliver.sh を、対象ユーザー指定で別プロセス起動する
    (webhook をブロックしない)。スクリプトが見つからない/起動に失敗した場合は、
    DB の当日分を即 push するフォールバック(run_now)に切り替える。"""
    def _deliver(sub: Subscriber) -> None:
        uid = sub.line_user_id
        if DELIVER_SH.exists():
            try:
                subprocess.Popen(
                    ["/bin/bash", str(DELIVER_SH), "--user", uid],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                return
            except Exception:
                log.exception("deliver.sh の起動に失敗。DBフォールバックへ user=%s", uid)
        threading.Thread(target=run_now, args=(uid,), daemon=True).start()
    return _deliver
