"""LINE メッセージの構築(純粋)と送信(SDK)。

- 構築側(spec)は SDK 非依存の素の dict を返す → テスト・ロジックが SDK を要求しない。
  spec の型:
    text : {"type":"text","text":str,"quick_reply":[{"label","data"}]|None}
    flex : {"type":"flex","alt":str,"contents":dict}  # contents は LINE Flex の生JSON
- 送信側(LineMessenger)だけが line-bot-sdk(v3) を import し、spec を SDK オブジェクトに変換する。
- テストは FakeMessenger(specを記録)を注入する。

新規実装(XAgent に LINE 連携は無い)。
"""

from __future__ import annotations

import copy
import json
from datetime import date

from .genres import ALWAYS_KEYS, GENRES, SELECTABLE_KEYS
from .models import SLOT_LABEL, NewsItem, Subscriber

# LINE の上限
QUICK_REPLY_MAX = 13
# 1バブルあたりの目安サイズ(LINEのバブル上限~10KBに対し余裕を持たせる)。
# 日本語はUTF-8で1文字3バイトのため、文字数でなくバイト数で測る(文字数だと実サイズを
# 約1/3に過小評価し、7000「文字」のバブルが実際は~21KBになり上限を超えていた)。
# これを超えそうなら次のバブル(=次メッセージ)に送り、件数は削らず全部出す。
BUBBLE_MAX_BYTES = 7000
MAX_MESSAGES = 5       # LINE は1回の push/reply で最大5メッセージ
ACCENT = "#1565C0"


# ---------------------------------------------------------------- spec builders

def text_spec(text: str, quick_reply: list[dict] | None = None) -> dict:
    return {"type": "text", "text": text, "quick_reply": quick_reply}


def _qr(label: str, data: str) -> dict:
    # quick reply のラベルは20字以内
    return {"label": label[:20], "data": data}


def genre_select_spec(selected: list[str]) -> dict:
    sel = [g for g in selected if g in GENRES]
    if sel:
        head = "受け取るジャンルを選んでください(複数可)。\n現在の選択: " + " / ".join(sel)
    else:
        head = "受け取るジャンルを選んでください(複数可)。タップで追加できます。"
    toggles = [
        _qr(f"{'✓' if key in sel else '＋'}{GENRES[key]['label']}", f"genre:{key}")
        for key in SELECTABLE_KEYS
    ]
    # 操作ボタン(すべて/決定/キャンセル)は必ず残す。ジャンルが増えても末尾で切れないよう先に枠を確保。
    actions = [_qr("すべて", "genre_all"), _qr("これで決定", "genre_done"), _qr("キャンセル", "cancel")]
    items = toggles[:QUICK_REPLY_MAX - len(actions)] + actions
    return text_spec(head, items)


_TIME_CHOICES = {
    "morning": [("6:00", "0600"), ("7:00", "0700"), ("8:00", "0800"), ("9:00", "0900")],
    "evening": [("19:00", "1900"), ("20:00", "2000"), ("21:00", "2100"), ("22:00", "2200")],
}


def time_select_spec(slot: str = "morning") -> dict:
    label = SLOT_LABEL.get(slot, "")
    head = (f"{label}の配信時刻を選んでください。\n他の時刻は「7:30」のように送ってください。")
    items = [_qr(disp, f"time:{hhmm}") for disp, hhmm in _TIME_CHOICES.get(slot, _TIME_CHOICES["morning"])]
    items.append(_qr("キャンセル", "cancel"))
    return text_spec(head, items)


def menu_quick_reply() -> list[dict]:
    return [
        _qr("ジャンル変更", "genre_edit"),
        _qr("朝の時刻", "morning_edit"),
        _qr("夜の時刻", "evening_edit"),
        _qr("今すぐ配信", "deliver_now"),
        _qr("設定確認", "show_settings"),
        _qr("ヘルプ", "help"),
    ]


def menu_spec(text: str = "メニュー") -> dict:
    return text_spec(text, menu_quick_reply())


def settings_summary_text(sub: Subscriber) -> str:
    g = " / ".join(sub.enabled_genres) if sub.enabled_genres else "(未設定)"
    m = f"{sub.morning_hour:02d}:{sub.morning_minute:02d}" if sub.morning_enabled else "オフ"
    e = f"{sub.evening_hour:02d}:{sub.evening_minute:02d}" if sub.evening_enabled else "オフ"
    return f"現在の設定\n・ジャンル: {g}\n・朝の配信: {m}\n・夜の配信: {e}"


# ---- ニュース配信 ----

def _tag_labels(item: NewsItem) -> list[str]:
    """表示するジャンルタグの label 列。item.genres(複数)が無ければ主ジャンルのみ。"""
    keys = item.genres or [item.genre]
    return [GENRES.get(k, {}).get("label", k) for k in keys]


def _tag_text(item: NewsItem) -> str:
    """ジャンルタグを「経済/株/政治」の形に。横断話題がどのジャンルに関わるかを示す。"""
    return "/".join(_tag_labels(item))


def _detail_data(item: NewsItem, digest_date: date | None, slot: str | None) -> str:
    """「詳細を見る」postback の data。

    再収集(今すぐ配信など)で同一記事でも NewsItem.id が変わるため、id を直に使うと
    少し前に届いた見出しのタップが「見つかりません」になる。日付+スロット+ジャンル+rank の
    安定キーにして、再取り込み後も同じ記事を引けるようにする(rank はダイジェスト内で一意)。
    日付/スロットが無い文脈(モック表示)では従来どおり id を使う。"""
    if digest_date is None or slot is None:
        return f"detail:{item.id}"
    return f"detail:{digest_date:%Y%m%d}:{slot}:{item.genre}:{item.rank}"


def _big_item_block(item: NewsItem, detail_data: str) -> dict:
    """大ニュース1件分の縦ブロック(見出し+タイトル+要約+元ポストリンク)。
    複数件を1枚の縦長バブルに積み上げるための部品(スマホで横スクロール不要にする)。"""
    # 常時ジャンル(特大)は専用見出し・赤系アクセントで目立たせる。関連ジャンルがあれば併記。
    if item.genre in ALWAYS_KEYS:
        related = [GENRES.get(k, {}).get("label", k) for k in (item.genres or []) if k not in ALWAYS_KEYS]
        extra = ("  " + " / ".join(related)) if related else ""
        heading, accent = f"🚨 特大ニュース{extra}", "#D32F2F"
    else:
        heading, accent = f"【{_tag_text(item)}】大ニュース", ACCENT
    contents = [
        {"type": "text", "text": heading, "size": "sm",
         "color": accent, "weight": "bold"},
        {"type": "text", "text": item.title, "weight": "bold", "size": "lg",
         "wrap": True, "margin": "sm"},
    ]
    if item.summary:
        contents.append(
            {"type": "text", "text": item.summary, "size": "sm",
             "color": "#555555", "wrap": True, "margin": "md"}
        )
    # 大ニュースもタップで長文の詳細(detail)を開ける(小ニュースと同じ導線)。
    # 一覧では見出し+要約までにとどめ、詳しく読みたい人だけ詳細を開く。
    contents.append(
        {"type": "text", "text": "▶ 詳細を見る", "size": "xs",
         "color": accent, "weight": "bold", "margin": "md",
         "action": {"type": "postback", "data": detail_data, "displayText": "詳細を見る"}}
    )
    url = item.source_urls[0] if item.source_urls else ""
    if url:
        # 元ポストへのリンクは残す(リンクテキストにして縦に詰め、高さを抑える)
        contents.append(
            {"type": "text", "text": "▶ 元ポストを見る", "size": "xs",
             "color": "#888888", "margin": "sm",
             "action": {"type": "uri", "label": "元ポストを見る", "uri": url}}
        )
    return {"type": "box", "layout": "vertical", "contents": contents}


def _small_row(item: NewsItem, detail_data: str) -> dict:
    """小ニュース1件分のコンパクトな縦行(タップで詳細 postback)。
    横カルーセルをやめ縦1枚に同居させることで、配信を1メッセージに収めて通数を節約する。"""
    return {
        "type": "text", "text": f"▷ 【{_tag_text(item)}】{item.title}",
        "size": "sm", "color": "#333333", "wrap": True, "margin": "md",
        "action": {"type": "postback", "data": detail_data, "displayText": "詳細を見る"},
    }


def _sep(margin: str = "md", color: str = "#E5E5E5") -> dict:
    return {"type": "separator", "margin": margin, "color": color}


_GREETING = {"morning": "おはようございます。今朝のニュースです", "evening": "こんばんは。今夜のニュースです"}


def _byte_size(contents: list[dict]) -> int:
    """LINE が数えるのと同じ「JSON のバイト数」。日本語は1文字3バイトなので文字数では測れない。"""
    return len(json.dumps(contents, ensure_ascii=False).encode("utf-8"))


def _text_nodes(node) -> list[dict]:
    """コンポーネント木の中の text を持つ dict を集める(切り詰め対象)。"""
    found: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            found.append(node)
        for v in node.values():
            found += _text_nodes(v)
    elif isinstance(node, list):
        for v in node:
            found += _text_nodes(v)
    return found


def _fit_component(comp: dict) -> dict:
    """単体で BUBBLE_MAX_BYTES を超えるコンポーネントを、収まるまで長い本文から切り詰める。

    バブル分割はコンポーネント単位なので、1件が単体で上限を超えると分割しても収まらず、
    LINE が 400 を返してその回の push が丸ごと失敗する(=その日のダイジェストが全滅する)。
    要約の質を変える処理ではなく、異常に長い出力が来たときだけ働く最後の安全網。
    """
    if _byte_size([comp]) <= BUBBLE_MAX_BYTES:
        return comp
    comp = copy.deepcopy(comp)
    nodes = _text_nodes(comp)
    # 一番長い本文を半分にする、を収まるまで繰り返す(見出しなど短い要素は残る)
    for _ in range(20):
        target = max(nodes, key=lambda n: len(n["text"]), default=None)
        if target is None or len(target["text"]) <= 20:
            break
        target["text"] = target["text"][: max(20, len(target["text"]) // 2)].rstrip() + "…"
        if _byte_size([comp]) <= BUBBLE_MAX_BYTES:
            break
    return comp


def _pack_bubbles(components: list[dict], alt_first: str, alt_rest: str) -> list[dict]:
    """縦に並ぶ components を、1バブルが大きくなり過ぎない範囲で複数バブルに詰める。
    件数は削らず(=全部出す)、サイズ超過時のみ次のバブル(=次メッセージ)へ送る。"""
    bubbles: list[list[dict]] = []
    cur: list[dict] = []
    for comp in components:
        comp = _fit_component(comp)  # 単体で上限超過なら切り詰める(push 全滅の防止)
        if cur and _byte_size(cur + [comp]) > BUBBLE_MAX_BYTES:
            bubbles.append(cur)
            cur = [comp]
        else:
            cur.append(comp)
    if cur:
        bubbles.append(cur)

    # LINE は1回の push/reply で最大5メッセージ。超過分のバブルは送れないが、黙って捨てると
    # 後半ジャンルの見出しが無言で消える。最後のバブルに省略を明示する(件数=タップ可能な見出し数)。
    kept = bubbles[:MAX_MESSAGES]
    dropped = bubbles[MAX_MESSAGES:]
    if dropped and kept:
        n = sum(1 for b in dropped for c in b
                if isinstance(c, dict) and str(c.get("action", {}).get("data", "")).startswith("detail:"))
        note = (f"…ほか {n} 件の見出しは次回の配信でお届けします。" if n
                else "…一部の見出しは次回の配信でお届けします。")
        kept[-1] = kept[-1] + [{"type": "text", "text": note, "size": "xs",
                                "color": "#999999", "wrap": True, "margin": "md"}]

    specs: list[dict] = []
    for idx, body in enumerate(kept):
        specs.append({
            "type": "flex", "alt": alt_first if idx == 0 else alt_rest,
            "contents": {"type": "bubble", "size": "giga",
                         "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body}},
        })
    return specs


def digest_specs(
    grouped: dict[str, list[NewsItem]], greeting: bool = True, slot: str | None = None,
    digest_date: date | None = None,
) -> list[dict]:
    """購読ジャンルの NewsItem 群を配信メッセージ(spec列)に変換する。

    - 大ニュースは **ジャンル順にすべて** 積み上げる(特大→各ジャンル。1ジャンルが多くても
      他ジャンルが押し出されない=各ジャンル最低1件は必ず出る。無いジャンルは出さない)。
    - 小ニュースは見出し行(タップで詳細 postback)を **すべて** 並べる(省略しない)。
    - 見やすさ優先。1バブルが大きくなり過ぎる場合だけ複数メッセージに分割する
      (LINE無料枠で数えるのは push 数だが、本数に余裕があるので件数は削らない)。
    """
    # grouped は表示順(特大→各ジャンル)。その順序を保ったまま大/小に振り分ける
    # (=ビューワー数の全体ソートをやめ、ジャンルごとの公平な掲載にする)。
    bigs: list[NewsItem] = []
    smalls: list[NewsItem] = []
    for items in grouped.values():
        for it in items:
            (bigs if it.importance == "big" else smalls).append(it)

    if not bigs and not smalls:
        return [text_spec("本日は対象ジャンルのニュースが見つかりませんでした。")]

    components: list[dict] = []
    if greeting:
        head = _GREETING.get(slot or "", "今日のニュースです")
        active = [g for g, items in grouped.items() if items]
        components.append({"type": "text", "text": head, "weight": "bold", "size": "md",
                           "wrap": True, "color": "#222222"})
        components.append({"type": "text", "text": " / ".join(active),
                           "size": "xxs", "color": "#999999", "wrap": True})

    if bigs:
        if components:
            components.append(_sep("lg"))
        for i, it in enumerate(bigs):
            if i > 0:
                components.append(_sep("lg"))
            components.append(_big_item_block(it, _detail_data(it, digest_date, slot)))

    if smalls:
        if components:
            components.append(_sep("xl", "#CCCCCC"))
        components.append({"type": "text", "text": "そのほかの見出し(タップで詳細)",
                           "size": "xs", "color": "#888888", "weight": "bold"})
        for it in smalls:
            components.append(_small_row(it, _detail_data(it, digest_date, slot)))

    alt = _GREETING.get(slot or "", "今日のニュース")
    return _pack_bubbles(components, alt_first=alt, alt_rest="ニュースのつづき")


# LINE のテキストメッセージ上限は5000字。余裕を持たせて切る。
DETAIL_MAX_CHARS = 4800


def detail_spec(item: NewsItem) -> dict:
    """「詳細を見る」タップで返す本文。見出しの再掲で終わらせず長め解説(detail。
    無ければ summary)を載せる。元ポストは本文を載せず、見たい人向けにリンクだけ残す。"""
    lines = [f"【{_tag_text(item)}】{item.title}"]

    body = item.detail or item.summary
    if body:
        lines.append("")
        lines.append(body)

    if item.source_tweets:
        lines.append("")
        lines.append("元ポスト:")
        for s in item.source_tweets[:3]:
            url = s.get("url", "")
            lines.append(f"・@{s.get('author', '?')} {url}".rstrip())
    elif item.source_urls:
        lines.append("")
        lines.append("元ポスト: " + " ".join(item.source_urls[:3]))

    text = "\n".join(lines)
    if len(text) > DETAIL_MAX_CHARS:
        text = text[:DETAIL_MAX_CHARS] + "…"
    return text_spec(text)


# ---------------------------------------------------------------- SDK 送信

def _spec_to_message(spec: dict):
    """spec を line-bot-sdk(v3) の Message に変換。"""
    from linebot.v3.messaging import (
        FlexContainer,
        FlexMessage,
        PostbackAction,
        QuickReply,
        QuickReplyItem,
        TextMessage,
    )

    if spec["type"] == "text":
        qr = None
        if spec.get("quick_reply"):
            qr = QuickReply(items=[
                QuickReplyItem(action=PostbackAction(
                    label=q["label"], data=q["data"], displayText=q["label"]
                ))
                for q in spec["quick_reply"]
            ])
        return TextMessage(text=spec["text"], quickReply=qr)

    if spec["type"] == "flex":
        return FlexMessage(altText=spec["alt"],
                           contents=FlexContainer.from_dict(spec["contents"]))

    raise ValueError(f"未知の spec type: {spec['type']}")


class LineMessenger:
    """実 LINE 送信。spec のリストを受けて reply/push する。"""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def _api(self):
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
        return MessagingApi(ApiClient(Configuration(access_token=self._access_token)))

    def reply(self, reply_token: str, specs: list[dict]) -> None:
        from linebot.v3.messaging import ReplyMessageRequest
        if not specs:
            return
        messages = [_spec_to_message(s) for s in specs[:5]]
        self._api().reply_message(
            ReplyMessageRequest(replyToken=reply_token, messages=messages)
        )

    def push(self, to: str, specs: list[dict]) -> None:
        from linebot.v3.messaging import PushMessageRequest
        if not specs:
            return
        messages = [_spec_to_message(s) for s in specs[:5]]
        self._api().push_message(PushMessageRequest(to=to, messages=messages))
