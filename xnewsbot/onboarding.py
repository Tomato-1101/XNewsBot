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
from . import mockdata
from .genres import GENRE_KEYS, SELECTABLE_KEYS
from .models import SLOT_LABEL, NewsItem, Subscriber

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
    reply_token = ev.get("reply_token", "")
    target_id = ev.get("target_id")  # グループ/ルームID(1:1なら None)

    if kind == "join":  # ボットがグループ/ルームに追加された(送信者は不明)
        messenger.reply(reply_token, [lc.text_spec(
            "グループに追加ありがとうございます。\n"
            "このトークに毎日のニュースを配信するには、受け取りたい方が"
            "(まず XNewsBot と1:1で初期設定を済ませたうえで)ここで「このグループに配信」と送ってください。")])
        return

    uid = ev.get("line_user_id")
    if not uid:  # 友だち未追加メンバーの発言などは user_id が来ない → 無視
        return
    sub = get_or_create_subscriber(session, uid, ev.get("display_name"))

    if kind == "follow":
        _start_onboarding(session, messenger, sub, reply_token)
    elif kind == "message":
        _handle_message(session, messenger, sub, ev.get("text", ""), reply_token,
                        deliver_now, target_id)
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


_BIND_WORDS = ("このグループに配信", "ここに配信", "グループ配信", "ここに配信して")
_UNBIND_WORDS = ("個別に配信", "個別配信", "1対1に配信", "個人に配信")
# ボタンを押さずテキストでも「今すぐ最新を収集して送る」を起動できる合言葉
_DELIVER_WORDS = ("今すぐ", "今すぐ配信", "最新", "最新ニュース", "配信", "ニュース配信")
# レイアウト確認用。収集せず DB のモック(架空)を現行レイアウトで返す合言葉
_MOCK_WORDS = ("テスト", "test", "てすと", "モック", "mock", "サンプル", "レイアウト")
# 選択/編集の途中で中断する合言葉(対話は無料なのでいつでも中断できる)
_CANCEL_WORDS = ("キャンセル", "やめる", "中止", "cancel", "やめ")


def _try_command(session, messenger, sub: Subscriber, text: str, reply_token: str,
                 deliver_now: DeliverNow | None) -> bool:
    """1:1でもグループ/ルームでも効く明示コマンドを処理する。処理したら True。

    グループでは雑談に逐一反応させたくないので、ここで True になったものだけ応答する。"""
    low = text.lower()
    if low in _MOCK_WORDS:
        # レイアウト確認用。収集せず DB のモック(架空)を現行レイアウトで即返す(API/時間を使わない)。
        grouped = mockdata.get_or_seed(session)
        messenger.reply(reply_token,
                        [lc.text_spec(mockdata.WARNING)] + lc.digest_specs(grouped, greeting=False))
        return True
    if text in _DELIVER_WORDS:
        # 「今すぐ配信」ボタンと同じ。今この瞬間の最新を収集→キュレーション→送信する。
        messenger.reply(reply_token, [lc.text_spec(
            "最新のニュースを今すぐお送りします。収集とキュレーションに10分ほどかかります。"
            "できあがり次第このトークにお届けします。")])
        if deliver_now is not None:
            deliver_now(sub)
        return True
    if text in ("メニュー", "menu") or low == "menu":
        messenger.reply(reply_token, [lc.menu_spec("メニューです。操作を選んでください。")])
        return True
    if text in ("ヘルプ", "help", "使い方") or low == "help":
        messenger.reply(reply_token, [_help_spec()])
        return True
    return False


def _handle_message(session, messenger, sub: Subscriber, text: str, reply_token: str,
                    deliver_now: DeliverNow | None, target_id: str | None = None) -> None:
    text = (text or "").strip()

    # 配信先の切り替え(グループ/ルームでも1:1でも受け付ける)
    if text in _BIND_WORDS:
        if target_id:
            sub.push_to = target_id
            _save(session, sub)
            messenger.reply(reply_token, [lc.text_spec(
                "これ以降、ニュースはこのトークに配信します。\n"
                "(個別トークに戻すには「個別に配信」と送ってください)")])
        else:
            messenger.reply(reply_token, [lc.text_spec(
                "ここは1:1トークです。配信先にしたいグループ/複数人トークで「このグループに配信」と送ってください。")])
        return
    if text in _UNBIND_WORDS:
        sub.push_to = None
        _save(session, sub)
        messenger.reply(reply_token, [lc.text_spec("配信先を個別トークに戻しました。")])
        return

    # グループ/ルーム: 明示コマンド(今すぐ/テスト/メニュー/ヘルプ)のみ応答する。
    # オンボーディング(ジャンル/時刻設定)は1:1専用。雑談には無反応(荒らさない)。
    if target_id:
        _try_command(session, messenger, sub, text, reply_token, deliver_now)
        return

    # 選択/編集の途中で「キャンセル」(対話は無料なので中断は自由)
    if text in _CANCEL_WORDS:
        _do_cancel(session, messenger, sub, reply_token)
        return

    # 時刻入力待ち(朝/夜。初回 or 編集)
    if sub.onboarding_step in ("morning", "evening"):
        slot = sub.onboarding_step
        t = parse_time(text)
        if t:
            _apply_slot_time(session, messenger, sub, slot, t[0], t[1], reply_token)
        else:
            messenger.reply(reply_token, [
                lc.text_spec("時刻が読み取れません。「7:30」の形式で送ってください。"),
                lc.time_select_spec(slot),
            ])
        return

    # ジャンル選択待ちの最中にテキストが来たらボタンへ誘導
    if sub.onboarding_step == "genres" and not sub.is_onboarded:
        messenger.reply(reply_token, [
            lc.text_spec("下のボタンからジャンルを選んでください。"),
            lc.genre_select_spec(sub.pending_genres),
        ])
        return

    # オンボーディング済み: 明示コマンド → 該当なければメニューへ誘導
    if _try_command(session, messenger, sub, text, reply_token, deliver_now):
        return
    messenger.reply(reply_token, [
        lc.text_spec("メニューから操作できます。"),
        lc.menu_spec("メニュー"),
    ])


def _handle_postback(session, messenger, sub: Subscriber, data: str, reply_token: str,
                     deliver_now: DeliverNow | None) -> None:
    if data.startswith("genre:"):
        key = data.split(":", 1)[1]
        if key in SELECTABLE_KEYS:
            pending = list(sub.pending_genres)
            if key in pending:
                pending.remove(key)
            else:
                pending.append(key)
            sub.pending_genres = pending
            _save(session, sub)
        messenger.reply(reply_token, [lc.genre_select_spec(sub.pending_genres)])

    elif data == "genre_all":
        sub.pending_genres = list(SELECTABLE_KEYS)
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
                lc.text_spec("ジャンルを変更しました。\n" + lc.settings_summary_text(sub)),
                lc.menu_spec("メニュー"),
            ])
        else:
            sub.onboarding_step = "morning"
            _save(session, sub)
            messenger.reply(reply_token, [
                lc.text_spec("ジャンルを設定しました: " + " / ".join(sub.enabled_genres)),
                lc.time_select_spec("morning"),
            ])

    elif data.startswith("time:"):
        hhmm = data.split(":", 1)[1]
        slot = sub.onboarding_step if sub.onboarding_step in ("morning", "evening") else "morning"
        if len(hhmm) == 4 and hhmm.isdigit():
            _apply_slot_time(session, messenger, sub, slot, int(hhmm[:2]), int(hhmm[2:]), reply_token)

    elif data == "genre_edit":
        sub.onboarding_step = "genres"
        sub.pending_genres = list(sub.enabled_genres)
        _save(session, sub)
        messenger.reply(reply_token, [lc.genre_select_spec(sub.pending_genres)])

    elif data in ("morning_edit", "evening_edit"):
        slot = "morning" if data == "morning_edit" else "evening"
        sub.onboarding_step = slot
        _save(session, sub)
        messenger.reply(reply_token, [lc.time_select_spec(slot)])

    elif data == "show_settings":
        messenger.reply(reply_token, [
            lc.text_spec(lc.settings_summary_text(sub)),
            lc.menu_spec("メニュー"),
        ])

    elif data == "help":
        messenger.reply(reply_token, [_help_spec()])

    elif data == "deliver_now":
        messenger.reply(reply_token, [
            lc.text_spec("今日のニュースをお送りします。収集とキュレーションに10分ほどかかります。"
                         "できあがり次第お届けします。")
        ])
        if deliver_now is not None:
            deliver_now(sub)

    elif data == "cancel":
        _do_cancel(session, messenger, sub, reply_token)

    elif data.startswith("detail:"):
        # 「そのほかの見出し」の行タップ。当該ニュースの詳細(要約+元ポスト)を返す。
        _handle_detail(session, messenger, data.split(":", 1)[1], reply_token)

    else:
        messenger.reply(reply_token, [lc.menu_spec("メニュー")])


def _handle_detail(session, messenger, item_id_str: str, reply_token: str) -> None:
    """小ニュースの見出しタップ(postback detail:<id>)に、その記事の詳細を reply する。"""
    try:
        item_id = int(item_id_str)
    except ValueError:
        messenger.reply(reply_token, [lc.menu_spec("メニュー")])
        return
    item = session.get(NewsItem, item_id)
    if item is None:
        messenger.reply(reply_token, [lc.text_spec(
            "この記事は見つかりませんでした(配信が更新された可能性があります)。")])
        return
    messenger.reply(reply_token, [lc.detail_spec(item)])


def _do_cancel(session, messenger, sub: Subscriber, reply_token: str) -> None:
    """選択/編集を中断する。設定済みなら変更を破棄して done に戻しメニューへ。
    初回設定の途中なら中断を伝える(やり直しは次の操作で再開)。応答は reply=無料。"""
    if sub.is_onboarded:
        sub.pending_genres = list(sub.enabled_genres)  # 編集中の選択を破棄
        sub.onboarding_step = "done"
        _save(session, sub)
        messenger.reply(reply_token, [
            lc.text_spec("操作をキャンセルしました。設定は変更していません。"),
            lc.menu_spec("メニュー"),
        ])
    else:
        messenger.reply(reply_token, [lc.text_spec(
            "設定を中断しました。続けるときは何かメッセージを送ってください。")])


def _apply_slot_time(session, messenger, sub: Subscriber, slot: str,
                     hour: int, minute: int, reply_token: str) -> None:
    """朝/夜の配信時刻を確定する。初回オンボーディングは朝→夜→完了の順に進む。
    編集時(オンボーディング済み)は当該スロットだけ更新して done に戻す。"""
    sub.set_slot_time(slot, hour, minute)
    label = SLOT_LABEL[slot]

    if sub.is_onboarded:
        sub.onboarding_step = "done"
        _save(session, sub)
        messenger.reply(reply_token, [
            lc.text_spec(f"{label}の配信時刻を {hour:02d}:{minute:02d} に変更しました。\n"
                         + lc.settings_summary_text(sub)),
            lc.menu_spec("メニュー"),
        ])
        return

    if slot == "morning":
        # 初回: 朝を設定したら次は夜
        sub.onboarding_step = "evening"
        _save(session, sub)
        messenger.reply(reply_token, [
            lc.text_spec(f"朝の配信時刻を {hour:02d}:{minute:02d} にしました。\n"
                         "次に夜の配信時刻を選んでください。"),
            lc.time_select_spec("evening"),
        ])
    else:
        # 初回: 夜を設定したら完了
        sub.is_onboarded = True
        sub.onboarding_step = "done"
        _save(session, sub)
        messenger.reply(reply_token, [
            lc.text_spec("設定が完了しました！\n" + lc.settings_summary_text(sub) +
                         "\n\n毎日 朝と夜の2回ニュースをお届けします。「今すぐ配信」で今すぐ試せます。"),
            lc.menu_spec("メニュー"),
        ])


def _help_spec() -> dict:
    return lc.text_spec(
        "【XNewsBotの使い方】\n"
        "毎日 朝と夜の2回、X(Twitter)から集めたニュースをお届けします。\n"
        "・大ニュースは要約付きで表示\n"
        "・そのほかは見出しのみ → タップで詳細\n\n"
        "「メニュー」と送ると、ジャンルや朝/夜の時刻をいつでも変更できます。",
        lc.menu_quick_reply(),
    )
