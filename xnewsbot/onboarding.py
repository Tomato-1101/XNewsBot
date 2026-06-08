"""LINE イベントを捌くオンボーディング/設定の状態機械。

入力は SDK 非依存の正規化イベント(dict)、出力は messenger 経由の spec。
重い配信(収集+キュレーション+push)は deliver_now コールバックに委譲する
(onboarding 自体は X/Claude/鍵に依存しない → テストしやすい)。

正規化イベント ev:
  {"kind": "follow"|"message"|"postback",
   "line_user_id": str, "display_name": str|None,
   "text": str, "data": str, "reply_token": str}
"""

from __future__ import annotations

import re
from typing import Callable

from sqlmodel import Session, select

from . import line_client as lc
from .genres import GENRE_KEYS, GENRES, is_valid_genre
from .models import Subscriber

# deliver_now(sub) : その購読者へ「今すぐ」配信する(非同期/別セッションで実行する想定)
DeliverNow = Callable[[Subscriber], None]

_TIME_COLON = re.compile(r"^\s*(\d{1,2})\s*[:：時]\s*(\d{1,2})?\s*分?\s*$")
_TIME_4DIGIT = re.compile(r"^\s*(\d{2})(\d{2})\s*$")


def parse_time(text: str) -> tuple[int, int] | None:
    """「7:30」「7時30分」「0730」等を (hour, minute) に。範囲外は None。"""
    m = _TIME_COLON.match(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
    else:
        m4 = _TIME_4DIGIT.match(text)
        if not m4:
            return None
        h, mi = int(m4.group(1)), int(m4.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return h, mi
    return None


def get_or_create_subscriber(
    session: Session, line_user_id: str, display_name: str | None = None
) -> Subscriber:
    sub = session.exec(
        select(Subscriber).where(Subscriber.line_user_id == line_user_id)
    ).first()
    if sub:
        if display_name and not sub.display_name:
            sub.display_name = display_name
            session.add(sub)
            session.commit()
        return sub
    sub = Subscriber(line_user_id=line_user_id, display_name=display_name)
    session.add(sub)
    session.commit()
    session.refresh(sub)
    return sub


def _save(session: Session, sub: Subscriber) -> None:
    session.add(sub)
    session.commit()


def handle_event(
    session: Session,
    messenger,
    ev: dict,
    *,
    deliver_now: DeliverNow | None = None,
) -> None:
    kind = ev.get("kind")
    sub = get_or_create_subscriber(session, ev["line_user_id"], ev.get("display_name"))
    reply_token = ev.get("reply_token", "")

    if kind == "follow":
        _start_onboarding(session, messenger, sub, reply_token)
    elif kind == "message":
        _handle_message(session, messenger, sub, ev.get("text", ""), reply_token, deliver_now)
    elif kind == "postback":
        _handle_postback(session, messenger, sub, ev.get("data", ""), reply_token, deliver_now)


# ----------------------------------------------------------------- handlers

def _start_onboarding(session, messenger, sub: Subscriber, reply_token: str) -> None:
    sub.onboarding_step = "genres"
    sub.pending_genres = list(sub.enabled_genres) if sub.is_onboarded else []
    _save(session, sub)
    welcome = lc.text_spec(
        "友だち追加ありがとうございます！\nXから毎日その日のニュースをお届けします。\n"
        "まず受け取るジャンルを選んでください。"
    )
    messenger.reply(reply_token, [welcome, lc.genre_select_spec(sub.pending_genres)])


def _handle_message(session, messenger, sub: Subscriber, text: str, reply_token: str,
                    deliver_now: DeliverNow | None) -> None:
    text = (text or "").strip()

    # 時刻入力待ち(初回 or 編集)
    if sub.onboarding_step == "time":
        t = parse_time(text)
        if t:
            sub.deliver_hour, sub.deliver_minute = t
            _finish_time(session, messenger, sub, reply_token)
        else:
            messenger.reply(reply_token, [
                lc.text_spec("時刻が読み取れません。「7:30」の形式で送ってください。"),
                lc.time_select_spec(),
            ])
        return

    # ジャンル選択待ちの最中にテキストが来たらボタンへ誘導
    if sub.onboarding_step == "genres" and not sub.is_onboarded:
        messenger.reply(reply_token, [
            lc.text_spec("下のボタンからジャンルを選んでください。"),
            lc.genre_select_spec(sub.pending_genres),
        ])
        return

    # オンボーディング済み: コマンド処理
    low = text.lower()
    if text in ("メニュー", "menu") or low == "menu":
        messenger.reply(reply_token, [lc.menu_spec("メニューです。操作を選んでください。")])
    elif text in ("ヘルプ", "help", "使い方") or low == "help":
        messenger.reply(reply_token, [_help_spec()])
    else:
        messenger.reply(reply_token, [
            lc.text_spec("メニューから操作できます。"),
            lc.menu_spec("メニュー"),
        ])


def _handle_postback(session, messenger, sub: Subscriber, data: str, reply_token: str,
                     deliver_now: DeliverNow | None) -> None:
    if data.startswith("genre:"):
        key = data.split(":", 1)[1]
        if is_valid_genre(key):
            pending = list(sub.pending_genres)
            if key in pending:
                pending.remove(key)
            else:
                pending.append(key)
            sub.pending_genres = pending
            _save(session, sub)
        messenger.reply(reply_token, [lc.genre_select_spec(sub.pending_genres)])

    elif data == "genre_all":
        sub.pending_genres = list(GENRE_KEYS)
        _save(session, sub)
        messenger.reply(reply_token, [lc.genre_select_spec(sub.pending_genres)])

    elif data == "genre_done":
        if not sub.pending_genres:
            messenger.reply(reply_token, [
                lc.text_spec("少なくとも1つのジャンルを選んでください。"),
                lc.genre_select_spec(sub.pending_genres),
            ])
            return
        # 表示順を GENRE_KEYS に合わせて確定
        sub.enabled_genres = [g for g in GENRE_KEYS if g in sub.pending_genres]
        if sub.is_onboarded:
            sub.onboarding_step = "done"
            _save(session, sub)
            messenger.reply(reply_token, [
                lc.text_spec("ジャンルを変更しました。\n" + lc.settings_summary_text(
                    sub.enabled_genres, sub.deliver_hour, sub.deliver_minute)),
                lc.menu_spec("メニュー"),
            ])
        else:
            sub.onboarding_step = "time"
            _save(session, sub)
            messenger.reply(reply_token, [
                lc.text_spec("ジャンルを設定しました: " + " / ".join(sub.enabled_genres)),
                lc.time_select_spec(),
            ])

    elif data.startswith("time:"):
        hhmm = data.split(":", 1)[1]
        if len(hhmm) == 4 and hhmm.isdigit():
            sub.deliver_hour, sub.deliver_minute = int(hhmm[:2]), int(hhmm[2:])
            _finish_time(session, messenger, sub, reply_token)

    elif data == "genre_edit":
        sub.onboarding_step = "genres"
        sub.pending_genres = list(sub.enabled_genres)
        _save(session, sub)
        messenger.reply(reply_token, [lc.genre_select_spec(sub.pending_genres)])

    elif data == "time_edit":
        sub.onboarding_step = "time"
        _save(session, sub)
        messenger.reply(reply_token, [lc.time_select_spec()])

    elif data == "show_settings":
        messenger.reply(reply_token, [
            lc.text_spec(lc.settings_summary_text(
                sub.enabled_genres, sub.deliver_hour, sub.deliver_minute)),
            lc.menu_spec("メニュー"),
        ])

    elif data == "help":
        messenger.reply(reply_token, [_help_spec()])

    elif data == "deliver_now":
        messenger.reply(reply_token, [
            lc.text_spec("今日のニュースを準備しています。少しお待ちください…")
        ])
        if deliver_now is not None:
            deliver_now(sub)

    else:
        messenger.reply(reply_token, [lc.menu_spec("メニュー")])


def _finish_time(session, messenger, sub: Subscriber, reply_token: str) -> None:
    was_onboarded = sub.is_onboarded
    sub.is_onboarded = True
    sub.onboarding_step = "done"
    _save(session, sub)
    summary = lc.settings_summary_text(sub.enabled_genres, sub.deliver_hour, sub.deliver_minute)
    if was_onboarded:
        messenger.reply(reply_token, [
            lc.text_spec(f"配信時刻を {sub.deliver_hour:02d}:{sub.deliver_minute:02d} に変更しました。\n" + summary),
            lc.menu_spec("メニュー"),
        ])
    else:
        messenger.reply(reply_token, [
            lc.text_spec("設定が完了しました！\n" + summary +
                         "\n\n毎日この時刻にニュースをお届けします。「今すぐ配信」で今すぐ試せます。"),
            lc.menu_spec("メニュー"),
        ])


def _help_spec() -> dict:
    return lc.text_spec(
        "【XNewsBotの使い方】\n"
        "毎日設定した時刻に、X(Twitter)から集めたニュースをお届けします。\n"
        "・大ニュースは要約付きで表示\n"
        "・そのほかは見出しのみ → タップで詳細\n\n"
        "「メニュー」と送るといつでも設定を変更できます。",
        lc.menu_quick_reply(),
    )
