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

import logging
import re
from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from . import digest, explain, mockdata, quota
from . import line_client as lc
from .genres import GENRE_KEYS, SELECTABLE_KEYS
from .models import SLOT_LABEL, GenreDigest, NewsItem, Subscriber

log = logging.getLogger("xnewsbot.onboarding")

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
    explain_start: explain.ExplainStart | None = None,
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
        _handle_postback(session, messenger, sub, ev.get("data", ""), reply_token, deliver_now,
                         explain_start, target_id)


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
                        [lc.text_spec(mockdata.WARNING)]
                        + lc.digest_specs(grouped, greeting=False, market=mockdata.MOCK_MARKET,
                                          schedule=mockdata.MOCK_SCHEDULE,
                                          x_usage=mockdata.MOCK_X_USAGE,
                                          line_quota=mockdata.MOCK_LINE_QUOTA))
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
                     deliver_now: DeliverNow | None,
                     explain_start: explain.ExplainStart | None = None,
                     target_id: str | None = None) -> None:
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
        # 時刻設定中(初回 or 編集)以外で、履歴に残った古い時刻ボタンをタップされても
        # 勝手に朝の時刻を書き換えない(設定が意図せず変わるのを防ぐ)。
        if sub.onboarding_step not in ("morning", "evening"):
            messenger.reply(reply_token, [lc.menu_spec("メニュー")])
            return
        hhmm = data.split(":", 1)[1]
        slot = sub.onboarding_step
        if len(hhmm) == 4 and hhmm.isdigit() and int(hhmm[:2]) < 24 and int(hhmm[2:]) < 60:
            _apply_slot_time(session, messenger, sub, slot, int(hhmm[:2]), int(hhmm[2:]), reply_token)
        else:
            # 履歴に残った旧形式など想定外の data でも必ず何か返す
            # (無反応だとタップしても動かないように見え、reply_token も無駄になる)
            messenger.reply(reply_token, [lc.menu_spec("メニュー")])

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

    elif data.startswith("explain:"):
        # 詳細の下の「AI解説」。押されたトーク(グループならグループ)へ解説を届ける。
        _handle_explain(session, messenger, sub, data.split(":", 1)[1], reply_token,
                        explain_start, target_id or sub.line_user_id)

    else:
        messenger.reply(reply_token, [lc.menu_spec("メニュー")])


def _handle_detail(session, messenger, payload: str, reply_token: str) -> None:
    """見出しタップ(postback)に、その記事の詳細を reply する。

    payload は 2 形式を受ける:
      - 安定キー "YYYYMMDD:slot:genre:rank"(現行。再収集で id が変わっても引ける)
      - 整数 id(履歴に残る旧ボタン。後方互換)"""
    item = _resolve_detail_item(session, payload)
    if item is None:
        messenger.reply(reply_token, [lc.text_spec(
            "この記事は見つかりませんでした(配信が更新された可能性があります)。")])
        return
    messenger.reply(reply_token, [lc.detail_spec(item, payload.strip())])


EXPLAIN_RUNNING_MSG = "いま作成中です。届かないときは、少し後にもう一度押すとすぐ表示します。"
EXPLAIN_BUSY_MSG = "いまほかのAI解説を作っています。少し後にもう一度押してください。"


def _is_mock_item(session, item: NewsItem) -> bool:
    """レイアウト確認用のモック記事(架空の内容)か。"""
    gd = session.get(GenreDigest, item.genre_digest_id)
    return gd is not None and gd.digest_date == mockdata.MOCK_DATE


def _handle_explain(session, messenger, sub: Subscriber, payload: str, reply_token: str,
                    explain_start: explain.ExplainStart | None, to: str) -> None:
    """「AI解説」の postback。作成済みは reply(無料)で返し、無ければ裏で作って push で届ける。

    push は LINE 無料枠(200通/月)を使うので、作成中の分と今回の分を引いても
    月末までの朝の配信に要る通数を残せるときだけ受ける。断るときは reply(無料)で理由を伝える。"""
    def say(text: str) -> None:
        messenger.reply(reply_token, [lc.text_spec(text)])

    item = _resolve_detail_item(session, payload)
    if item is None:
        say("この記事は見つかりませんでした(配信が更新された可能性があります)。")
        return
    if _is_mock_item(session, item):
        say("この記事はレイアウト確認用のサンプル(架空の内容)なので、AI解説は作れません。")
        return
    row = explain.get(session, item.id, item.title)
    st = explain.status_of(row)
    if st == "done":
        say(explain.line_text(item.title, row.text))
        return
    if st == "running":
        say(EXPLAIN_RUNNING_MSG)
        return
    if explain_start is None:
        say("いまはAI解説を使えません。")
        return
    now_local = datetime.now(ZoneInfo(sub.tz))
    if explain.in_quiet_hours(now_local):
        say("朝の配信の準備中(7:00〜8:30)はAI解説を受け付けていません。8:30以降にもう一度押してください。")
        return
    if explain.running_count(session) >= explain.MAX_RUNNING:  # 通数の API を叩く前に断れる分は断る
        say(EXPLAIN_BUSY_MSG)
        return

    q = quota.fetch_extra_push(messenger, session, to, now_local)
    if q is None:
        say("LINEの送信枠の残りを確認できなかったため、朝の配信を守るためにAI解説は使えません。"
            "時間をおいてもう一度お試しください。")
        return

    # 判定と確保の間に別の押下が確保すると、その分を数え漏らす。webhook 内は1区間にする
    with explain.LINE_CLAIM_LOCK:
        reserved = explain.reserved_push_cost(session)
        token = None
        if quota.push_allowed(q["remaining"], reserved, q["cost"], q["need"]):
            token = explain.claim(session, item.id, title=item.title, push_to=to,
                                  push_cost=q["cost"])
    if token is None:
        if not quota.push_allowed(q["remaining"], reserved, q["cost"], q["need"]):
            say("今月のLINEの送信枠が残り少ないため、朝の配信を守るためにAI解説は使えません"
                f"（残り{q['remaining'] - reserved}通・月末までの朝配信に{q['need']}通必要）。")
        elif explain.status_of(explain.get(session, item.id, item.title)) == "running":
            say(EXPLAIN_RUNNING_MSG)  # 確認の直後に別の押下(別プロセス含む)が先に確保した
        else:
            say(EXPLAIN_BUSY_MSG)     # 同時に作れる数の上限
        return
    try:
        say("AI解説を作っています。1〜2分お待ちください。")
    except Exception:  # noqa: BLE001 — 確保済みなので、受付の返信に失敗しても生成と push は進める
        log.exception("AI解説の受付返信に失敗 item=%s", item.id)
    explain_start(item.id, token, to)


def _resolve_detail_item(session, payload: str) -> NewsItem | None:
    payload = (payload or "").strip()
    if payload.isdigit():  # 旧形式: NewsItem.id 直指定
        return session.get(NewsItem, int(payload))
    parts = payload.split(":")
    if len(parts) != 4:
        return None
    ymd, slot, genre, rank_s = parts
    try:
        d = date(int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8]))
        rank = int(rank_s)
    except (ValueError, IndexError):
        return None
    dg = digest.get_genre_digest(session, genre, d, slot)
    if dg is None:
        return None
    return session.exec(
        select(NewsItem).where(
            NewsItem.genre_digest_id == dg.id, NewsItem.rank == rank
        )
    ).first()


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
        # 実際に何回届くかは運用中の配信スロット(launchd)次第(現在は朝のみ)。
        # 「朝と夜の2回」と約束すると夜が来ない日に故障と誤解されるため、時刻を約束しない文言にする。
        messenger.reply(reply_token, [
            lc.text_spec("設定が完了しました！\n" + lc.settings_summary_text(sub) +
                         "\n\n上の設定した時間にニュースをお届けします。「今すぐ配信」で今すぐ試せます。"),
            lc.menu_spec("メニュー"),
        ])


def _help_spec() -> dict:
    return lc.text_spec(
        "【XNewsBotの使い方】\n"
        "毎日、設定した時間に X(Twitter)から集めたニュースをお届けします。\n"
        "・1通目: 今日の要点・市況（仮想通貨を含む）・今日の予定（経済指標や決算など）・残り使用量\n"
        "・2通目: 主なニュース（ジャンルごとに横へスワイプ。要約付き）\n"
        "・3通目: ほかのニュース（同じジャンル順。短い要約付き）→ タップで詳細\n"
        "・ジャンルは「仮想通貨」「話題」なども選べます\n\n"
        "「メニュー」と送ると、ジャンルや朝/夜の時刻をいつでも変更できます。",
        lc.menu_quick_reply(),
    )
