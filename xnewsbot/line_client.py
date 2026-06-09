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

from .genres import GENRES
from .models import SLOT_LABEL, NewsItem, Subscriber

# LINE の上限
QUICK_REPLY_MAX = 13
CAROUSEL_MAX = 12
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
    items = []
    for key in GENRES:
        mark = "✓" if key in sel else "＋"
        items.append(_qr(f"{mark}{GENRES[key]['label']}", f"genre:{key}"))
    items.append(_qr("すべて", "genre_all"))
    items.append(_qr("これで決定", "genre_done"))
    return text_spec(head, items[:QUICK_REPLY_MAX])


_TIME_CHOICES = {
    "morning": [("6:00", "0600"), ("7:00", "0700"), ("8:00", "0800"), ("9:00", "0900")],
    "evening": [("19:00", "1900"), ("20:00", "2000"), ("21:00", "2100"), ("22:00", "2200")],
}


def time_select_spec(slot: str = "morning") -> dict:
    label = SLOT_LABEL.get(slot, "")
    head = (f"{label}の配信時刻を選んでください。\n他の時刻は「7:30」のように送ってください。")
    items = [_qr(disp, f"time:{hhmm}") for disp, hhmm in _TIME_CHOICES.get(slot, _TIME_CHOICES["morning"])]
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

def _big_bubble(item: NewsItem) -> dict:
    label = GENRES.get(item.genre, {}).get("label", item.genre)
    body = {
        "type": "box", "layout": "vertical", "contents": [
            {"type": "text", "text": f"【{label}】大ニュース", "size": "xs",
             "color": ACCENT, "weight": "bold"},
            {"type": "text", "text": item.title, "weight": "bold", "size": "md",
             "wrap": True, "margin": "sm"},
        ],
    }
    if item.summary:
        body["contents"].append(
            {"type": "text", "text": item.summary, "size": "sm",
             "color": "#555555", "wrap": True, "margin": "md"}
        )
    bubble = {"type": "bubble", "size": "mega", "body": body}
    url = item.source_urls[0] if item.source_urls else ""
    if url:
        bubble["footer"] = {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "button", "style": "link", "height": "sm",
                 "action": {"type": "uri", "label": "元ポストを見る", "uri": url}}
            ],
        }
    return bubble


def _small_bubble(item: NewsItem) -> dict:
    label = GENRES.get(item.genre, {}).get("label", item.genre)
    return {
        "type": "bubble", "size": "micro",
        "body": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": f"【{label}】", "size": "xxs",
                 "color": ACCENT, "weight": "bold"},
                {"type": "text", "text": item.title, "size": "sm", "wrap": True, "margin": "sm"},
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "contents": [
                {"type": "button", "style": "primary", "height": "sm",
                 "action": {"type": "postback", "label": "詳細を見る",
                            "data": f"detail:{item.id}", "displayText": "詳細を見る"}}
            ],
        },
    }


_GREETING = {"morning": "おはようございます。今朝のニュースです", "evening": "こんばんは。今夜のニュースです"}


def digest_specs(
    grouped: dict[str, list[NewsItem]], greeting: bool = True, slot: str | None = None
) -> list[dict]:
    """購読ジャンルの NewsItem 群を配信メッセージ(spec列)に変換する。
    構成: 挨拶+大ニュースFlex+小ニュースFlex(タップで詳細)。slot で朝/夜の挨拶を切替。"""
    bigs: list[NewsItem] = []
    smalls: list[NewsItem] = []
    for items in grouped.values():
        for it in items:
            (bigs if it.importance == "big" else smalls).append(it)
    bigs.sort(key=lambda i: i.top_view_count, reverse=True)
    smalls.sort(key=lambda i: i.top_view_count, reverse=True)

    specs: list[dict] = []
    if not bigs and not smalls:
        specs.append(text_spec("本日は対象ジャンルのニュースが見つかりませんでした。"))
        return specs

    if greeting:
        genres = " / ".join(grouped.keys())
        head = _GREETING.get(slot or "", "今日のニュースです")
        specs.append(text_spec(f"{head}({genres})。"))

    if bigs:
        bubbles = [_big_bubble(i) for i in bigs[:CAROUSEL_MAX]]
        specs.append({"type": "flex", "alt": "大ニュース",
                      "contents": {"type": "carousel", "contents": bubbles}})

    if smalls:
        shown = smalls[:CAROUSEL_MAX]
        bubbles = [_small_bubble(i) for i in shown]
        specs.append({"type": "flex", "alt": "そのほかのニュース(タップで詳細)",
                      "contents": {"type": "carousel", "contents": bubbles}})
        if len(smalls) > CAROUSEL_MAX:
            specs.append(text_spec(f"※ ほか {len(smalls) - CAROUSEL_MAX} 件は省略しました。"))
    return specs


def detail_spec(item: NewsItem) -> dict:
    label = GENRES.get(item.genre, {}).get("label", item.genre)
    lines = [f"【{label}】{item.title}"]
    if item.summary:
        lines.append("")
        lines.append(item.summary)
    if item.source_tweets:
        lines.append("")
        lines.append("元ポスト:")
        for s in item.source_tweets[:3]:
            url = s.get("url", "")
            lines.append(f"・@{s.get('author','?')} {url}".rstrip())
    elif item.source_urls:
        lines.append("")
        lines.append("元ポスト: " + " ".join(item.source_urls[:3]))
    return text_spec("\n".join(lines))


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
