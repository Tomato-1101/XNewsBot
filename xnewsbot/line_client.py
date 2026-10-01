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
import re
import urllib.parse
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from .genres import ALWAYS_KEYS, GENRES, SELECTABLE_KEYS
from .models import SLOT_LABEL, NewsItem, Subscriber

# LINE の上限
QUICK_REPLY_MAX = 13
# Flex の上限(バブル 30KB / カルーセル 50KB・12枚)に余裕を持たせた値。
# 日本語はUTF-8で1文字3バイトのため、文字数でなくバイト数で測る。
# 1ジャンルがバブル上限を超えそうなら記事単位で次のバブルに送り、件数は削らず全部出す。
BUBBLE_MAX_BYTES = 28000
CAROUSEL_MAX_BYTES = 48000
CAROUSEL_MAX_BUBBLES = 12
MAX_MESSAGES = 5       # LINE は1回の push/reply で最大5メッセージ
URI_MAX_CHARS = 1000   # URI action の uri の上限


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


# ---- ニュース配信(要点バブル + ジャンル別カルーセル) ----
#
# 1回の push は「要点バブル1通 + ジャンル別カルーセル」で最大5メッセージ。
# LINE の通数は宛先人数で数えるので、5メッセージ以内なら通数は増えない。

GENRE_COLORS = {"特大": "#D32F2F", "AI": "#4F46E5", "株": "#0F766E", "テクノロジー": "#0369A1"}
OTHER_COLOR = "#475569"   # 上記以外のジャンル
TITLE_COLOR = "#111111"
TEXT_COLOR = "#222222"
SUB_COLOR = "#888888"
META_COLOR = "#999999"
RULE_COLOR = "#EEEEEE"
UP_COLOR = "#C62828"
DOWN_COLOR = "#1565C0"
POINTS_MAX = 5
ALT_MAX_CHARS = 400
DEFAULT_TZ = "Asia/Tokyo"
_WEEKDAYS = "月火水木金土日"


def _tag_labels(item: NewsItem) -> list[str]:
    """表示するジャンルタグの label 列。item.genres(複数)が無ければ主ジャンルのみ。"""
    keys = item.genres or [item.genre]
    return [GENRES.get(k, {}).get("label", k) for k in keys]


def _tag_text(item: NewsItem) -> str:
    """ジャンルタグを「経済/株/政治」の形に。横断話題がどのジャンルに関わるかを示す。"""
    return "/".join(_tag_labels(item))


def _genre_label(genre: str) -> str:
    return GENRES.get(genre, {}).get("label", genre)


def _genre_color(genre: str) -> str:
    return GENRE_COLORS.get(genre, OTHER_COLOR)


def _detail_data(item: NewsItem, digest_date: date | None, slot: str | None) -> str:
    """「詳細を見る」postback の data。

    再収集(今すぐ配信など)で同一記事でも NewsItem.id が変わるため、id を直に使うと
    少し前に届いた見出しのタップが「見つかりません」になる。日付+スロット+ジャンル+rank の
    安定キーにして、再取り込み後も同じ記事を引けるようにする(rank はダイジェスト内で一意)。
    日付/スロットが無い文脈(モック表示)では従来どおり id を使う。"""
    if digest_date is None or slot is None:
        return f"detail:{item.id}"
    return f"detail:{digest_date:%Y%m%d}:{slot}:{item.genre}:{item.rank}"


def _detail_action(item: NewsItem, digest_date: date | None, slot: str | None) -> dict:
    return {"type": "postback", "data": _detail_data(item, digest_date, slot),
            "displayText": f"詳細: {item.title[:30]}"}


def _sep(margin: str = "md", color: str = RULE_COLOR) -> dict:
    return {"type": "separator", "margin": margin, "color": color}


def _source_name(s: dict) -> str:
    """出典の表示名。media が無い旧データは X なら @author、ニュースなら媒体名(author)。"""
    if s.get("media"):
        return str(s["media"])
    author = s.get("author") or ""
    if not author or author == "?":
        return ""
    return author if s.get("kind") == "news" else f"@{author}"


def _parse_time(raw) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _ago(item: NewsItem, now: datetime) -> str:
    """出典の最新時刻から now までを「N分前/N時間前/N日前」に。時刻が無ければ ""。"""
    times = [t for t in (_parse_time(s.get("created_at")) for s in item.source_tweets or []
                         if s.get("created_at")) if t]
    if not times:
        return ""
    minutes = int(max(0.0, (now - max(times)).total_seconds()) // 60)
    if minutes < 60:
        return f"{max(minutes, 1)}分前"
    if minutes < 60 * 24:
        return f"{minutes // 60}時間前"
    return f"{minutes // (60 * 24)}日前"


def _meta_text(item: NewsItem, now: datetime) -> str:
    """「日経・Bloomberg ほか2件・3時間前」。出典も時刻も無ければ ""。"""
    names = list(dict.fromkeys(n for n in (_source_name(s) for s in item.source_tweets or []) if n))
    parts: list[str] = []
    if names:
        src = "・".join(names[:2])
        if len(names) > 2:
            src += f" ほか{len(names) - 2}件"
        parts.append(src)
    ago = _ago(item, now)
    if ago:
        parts.append(ago)
    return "・".join(parts)


# RFC 3986 でパス・クエリにそのまま置ける記号。[] はホスト(IPv6)専用で、クエリにあると LINE が拒否する
_URI_SAFE = ":/?@!$&'()*+,;=%-._~"
_BAD_PCT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_HOST_RE = re.compile(r"[A-Za-z0-9.-]+")


def _safe_uri(url) -> str | None:
    """URI action に渡してよい形にして返す。直せなければ None(http/https・1000字以下・空白や制御文字なし)。

    元記事 URL は外部 RSS や X 由来で形が保証されない。LINE は uri が1つでも不正だと
    同じ push の全メッセージを 400 で拒否する(=その回のダイジェストが全滅する)ので、渡す前に直すか弾く。
    日本語や | [] を含む URL も拒否されることを validate API で確認済み(2026-10-01)なので、
    パス・クエリ・フラグメントだけをパーセントエンコードする(ホスト部はエンコードすると壊れる)。
    ホストは英数字・ドット・ハイフンの名前だけを通す(IPv6 リテラルは LINE が拒否するので省く。validate API で確認)。
    """
    if not isinstance(url, str) or not url:
        return None
    if not url.startswith(("http://", "https://")):
        return None
    if any(ch.isspace() or not ch.isprintable() for ch in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        host, _port = parts.hostname, parts.port  # port は不正な値(:bad・範囲外)で ValueError
    except ValueError:  # 壊れた IPv6 表記・不正ポートなど
        return None
    if not host or "@" in parts.netloc:
        return None
    # hostname は小文字化された値なので、K(U+212A)のように小文字化で ASCII になる文字が検査を素通りする。
    # 返すのは元の netloc なので、そちらも ASCII であることを確かめる。
    if not parts.netloc.isascii() or not _HOST_RE.fullmatch(host):
        return None
    path, query, frag = (urllib.parse.quote(p, safe=_URI_SAFE)
                         for p in (parts.path, parts.query, parts.fragment))
    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, frag))
    if _BAD_PCT.search(url) or len(url) > URI_MAX_CHARS:
        return None
    return url


def _byte_size(obj) -> int:
    """LINE が数えるのと同じ「JSON のバイト数」。日本語は1文字3バイトなので文字数では測れない。"""
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


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


def _fit_component(comp, limit: int):
    """単体で limit を超えるコンポーネント(dict か list)を、収まるまで長い本文から切り詰める。

    バブル分割は記事単位なので、1件が単体で上限を超えると分割しても収まらず、
    LINE が 400 を返してその回の push が丸ごと失敗する(=その日のダイジェストが全滅する)。
    要約の質を変える処理ではなく、異常に長い出力が来たときだけ働く最後の安全網。
    """
    if _byte_size(comp) <= limit:
        return comp
    comp = copy.deepcopy(comp)
    nodes = _text_nodes(comp)
    # 一番長い本文を半分にする、を収まるまで繰り返す(見出しなど短い要素は残る)
    for _ in range(20):
        target = max(nodes, key=lambda n: len(n["text"]), default=None)
        if target is None or len(target["text"]) <= 20:
            break
        target["text"] = target["text"][: max(20, len(target["text"]) // 2)].rstrip() + "…"
        if _byte_size(comp) <= limit:
            break
    return comp


# -- 要点バブル --

def _pick_points(grouped: dict[str, list[NewsItem]]) -> list[NewsItem]:
    """今日の要点(最大5本): 特大の big をすべて先頭 → 他ジャンルの big を score 降順
    → 足りなければ small を score 降順。同点はジャンル順・rank 順。"""
    pairs = [(gi, g, it) for gi, (g, items) in enumerate(grouped.items()) for it in items]

    def by_score(rows):
        return [it for _gi, _g, it in sorted(rows, key=lambda r: (-(r[2].score or 0), r[0], r[2].rank))]

    always = [it for _gi, g, it in pairs if g in ALWAYS_KEYS and it.importance == "big"]
    bigs = by_score([r for r in pairs if r[1] not in ALWAYS_KEYS and r[2].importance == "big"])
    smalls = by_score([r for r in pairs if r[2].importance != "big"])
    return (always + bigs + smalls)[:POINTS_MAX]


def _signed(v: float, unit: str) -> tuple[str, str]:
    """前日比を符号つき文字列と色に(▲▼は会計で負号の意味があるので使わない)。"""
    v = round(v, 2)
    if v == 0:
        v = 0.0  # -0.00 を出さない
    color = UP_COLOR if v > 0 else DOWN_COLOR if v < 0 else SUB_COLOR
    return f"{v:+.2f}{unit}", color


def _market_row(m: dict) -> dict | None:
    """市況1行(ラベル/終値/前日比)。終値やラベルが無い行は出さない。"""
    label = str(m.get("label") or m.get("key") or "")
    try:
        close = float(m.get("close"))
    except (TypeError, ValueError):
        return None
    if not label:
        return None
    kind = m.get("kind")
    if kind == "fx":
        close_s = f"{close:,.2f}円"
    elif kind == "yield":
        close_s = f"{close:.2f}%"
    else:
        close_s = f"{close:,.0f}"
    raw, unit = (m.get("change"), "pt") if kind == "yield" else (m.get("change_pct"), "%")
    try:
        chg_s, chg_color = _signed(float(raw), unit)
    except (TypeError, ValueError):
        chg_s, chg_color = "—", SUB_COLOR
    return {"type": "box", "layout": "horizontal", "margin": "sm", "contents": [
        {"type": "text", "text": label, "size": "xs", "color": "#555555", "flex": 5},
        {"type": "text", "text": close_s, "size": "xs", "color": TEXT_COLOR, "align": "end", "flex": 4},
        {"type": "text", "text": chg_s, "size": "xs", "weight": "bold", "color": chg_color,
         "align": "end", "flex": 3},
    ]}


def _point_row(no: int, item: NewsItem, digest_date: date | None, slot: str | None) -> dict:
    color = _genre_color(item.genre)
    return {
        "type": "box", "layout": "horizontal", "margin": "md", "spacing": "md",
        "action": _detail_action(item, digest_date, slot),
        "contents": [
            {"type": "text", "text": str(no), "size": "sm", "weight": "bold", "color": color, "flex": 0},
            {"type": "box", "layout": "vertical", "contents": [
                {"type": "text", "text": _genre_label(item.genre), "size": "xxs", "weight": "bold",
                 "color": color},
                {"type": "text", "text": item.title, "size": "sm", "color": TEXT_COLOR, "wrap": True},
            ]},
        ],
    }


def _summary_bubble(grouped, points, market, heading: str, digest_date, slot,
                    has_carousel: bool) -> dict:
    total = sum(len(items) for items in grouped.values())
    counts = "・".join(f"{_genre_label(g)} {len(items)}" for g, items in grouped.items() if items)
    contents: list[dict] = [
        {"type": "text", "text": heading, "size": "lg", "weight": "bold", "color": TITLE_COLOR},
        {"type": "text", "text": f"{counts}（計{total}件）", "size": "xs", "color": SUB_COLOR,
         "wrap": True},
        {"type": "text", "text": "今日の要点", "size": "sm", "weight": "bold", "color": TITLE_COLOR,
         "margin": "xl"},
    ]
    contents += [_point_row(i, it, digest_date, slot) for i, it in enumerate(points, 1)]

    rows = [r for r in (_market_row(m) for m in market or []) if r]
    if rows:
        contents.append(_sep("xl"))
        contents.append({"type": "text", "text": "市況（前日終値）", "size": "sm", "weight": "bold",
                         "color": TITLE_COLOR, "margin": "lg"})
        contents += rows

    if has_carousel:
        contents.append({"type": "text", "text": "ジャンル別の記事は次のカードを横にスワイプ →",
                         "size": "xxs", "color": META_COLOR, "margin": "xl", "wrap": True})
    bubble = {"type": "bubble", "size": "giga",
              "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": contents}}
    return _fit_component(bubble, BUBBLE_MAX_BYTES)


# -- ジャンル別カルーセル --

def _big_block(item: NewsItem, color: str, action: dict, now: datetime) -> dict:
    """大ニュース1件: 見出し → 要約 → 出典・時刻 → [詳細を読む][元記事]。"""
    contents: list[dict] = [
        {"type": "text", "text": item.title, "size": "md", "weight": "bold", "color": TITLE_COLOR,
         "wrap": True},
    ]
    if item.summary:
        contents.append({"type": "text", "text": item.summary, "size": "sm", "color": "#444444",
                         "wrap": True, "margin": "sm"})
    meta = _meta_text(item, now)
    if meta:
        contents.append({"type": "text", "text": meta, "size": "xxs", "color": META_COLOR,
                         "margin": "sm", "wrap": True})
    # 2つのリンクを左に寄せて並べる(flex 0。既定の flex 1 だと「元記事」が中央から始まる)
    links = [{"type": "text", "text": "詳細を読む", "size": "xs", "weight": "bold", "color": color,
              "flex": 0, "action": action}]
    # 不正な URL だけなら「元記事」リンクを省く(記事本体は出す)。先頭が不正でも他に使える URL があればそれを使う
    url = next((u for u in map(_safe_uri, item.source_urls or []) if u), None)
    if url:
        links.append({"type": "text", "text": "元記事", "size": "xs", "color": SUB_COLOR, "flex": 0,
                      "action": {"type": "uri", "label": "元記事", "uri": url}})
    contents.append({"type": "box", "layout": "horizontal", "margin": "md", "spacing": "xl",
                     "contents": links})
    return {"type": "box", "layout": "vertical", "contents": contents}


def _small_block(item: NewsItem, action: dict, now: datetime) -> dict:
    """見出し一覧の1行(タップで詳細)。出典・時刻を小さく添える。"""
    contents: list[dict] = [
        {"type": "text", "text": item.title, "size": "sm", "color": TEXT_COLOR, "wrap": True},
    ]
    meta = _meta_text(item, now)
    if meta:
        contents.append({"type": "text", "text": meta, "size": "xxs", "color": "#AAAAAA"})
    return {"type": "box", "layout": "vertical", "margin": "md", "action": action,
            "contents": contents}


def _genre_units(items: list[NewsItem], color: str, digest_date, slot, now) -> list[tuple[list, list]]:
    """記事1件ごとの (先頭の区切り線, 本体) の列。バブルを分けるときは記事単位で分け、
    新しいバブルの先頭には区切り線を置かない。"""
    ordered = sorted(items, key=lambda it: it.rank)
    bigs = [it for it in ordered if it.importance == "big"]
    smalls = [it for it in ordered if it.importance != "big"]
    units: list[tuple[list, list]] = []
    for i, it in enumerate(bigs):
        units.append(([_sep("lg")] if i else [],
                      [_big_block(it, color, _detail_action(it, digest_date, slot), now)]))
    for j, it in enumerate(smalls):
        row = _small_block(it, _detail_action(it, digest_date, slot), now)
        if j == 0 and bigs:
            # 見出しラベルは最初の1件と同じ単位にして、ラベルだけがバブル末尾に残らないようにする
            label = {"type": "text", "text": "ほかの見出し", "size": "xs", "weight": "bold",
                     "color": SUB_COLOR, "margin": "lg"}
            units.append(([_sep("xl")], [label, row]))
        elif j == 0:
            units.append(([], [row]))
        else:
            units.append(([_sep("md", "#F2F2F2")], [row]))
    return units


def _genre_bubble(genre: str, head: str, count: int, body: list[dict]) -> dict:
    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "horizontal", "backgroundColor": _genre_color(genre),
                   "paddingAll": "16px", "contents": [
                       {"type": "text", "text": head, "size": "lg", "weight": "bold",
                        "color": "#FFFFFF", "flex": 1},
                       {"type": "text", "text": f"{count}件", "size": "sm", "color": "#FFFFFF",
                        "align": "end", "gravity": "center"},
                   ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def _genre_bubbles(genre: str, items: list[NewsItem], digest_date, slot,
                   now: datetime) -> list[tuple[dict, int]]:
    """1ジャンルを (バブル, 載せた記事数) の列にする。件数は削らず、28000B を超えるときだけ
    記事単位で次のバブルへ送る(見出しは「AI (1/2)」)。"""
    label = _genre_label(genre)

    def measure(body: list[dict]) -> int:
        return _byte_size(_genre_bubble(genre, f"{label} (00/00)", len(items), body))

    budget = BUBBLE_MAX_BYTES - measure([]) - 16  # 16: 配列の区切り文字ぶんの余裕
    pages: list[tuple[list[dict], int]] = []
    cur: list[dict] = []
    n = 0
    for lead, body in _genre_units(items, _genre_color(genre), digest_date, slot, now):
        body = _fit_component(body, budget - _byte_size(lead))  # 単体で上限超過なら切り詰める
        if cur and measure(cur + lead + body) > BUBBLE_MAX_BYTES:
            pages.append((cur, n))
            cur, n = list(body), 1
        else:
            cur = (cur + lead + body) if cur else list(body)
            n += 1
    if cur:
        pages.append((cur, n))

    if len(pages) == 1:
        return [(_genre_bubble(genre, label, len(items), pages[0][0]), pages[0][1])]
    return [(_genre_bubble(genre, f"{label} ({k}/{len(pages)})", len(items), body), cnt)
            for k, (body, cnt) in enumerate(pages, 1)]


def _note_bubble(text: str) -> dict:
    return {"type": "bubble", "size": "giga",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                {"type": "text", "text": text, "size": "sm", "color": META_COLOR, "wrap": True}]}}


def _carousel(bubbles: list[dict]) -> dict:
    return {"type": "carousel", "contents": bubbles}


TOO_LARGE_NOTE = "長すぎるため表示できない記事がありました"


def _guard_flex(contents: dict) -> dict:
    """最後の安全網: 上限を超えたバブルだけ注記に差し替える。

    1通でも上限を超えると LINE は push 全体を 400 で拒否し、その回の全メッセージが届かない。
    通常は _fit_component の切り詰めで収まりここでは何も変わらない。切り詰めきれなかった
    異常な出力が来たときに、その1枚だけを諦めて残りを届けるためのもの。
    """
    if contents["type"] == "bubble":
        return contents if _byte_size(contents) <= BUBBLE_MAX_BYTES else _note_bubble(TOO_LARGE_NOTE)
    bubbles = [b if _byte_size(b) <= BUBBLE_MAX_BYTES else _note_bubble(TOO_LARGE_NOTE)
               for b in contents["contents"]]
    # 各バブルが上限内なら _pack_carousels の詰め方で 48000B 以内に収まる(差し替えは小さくなる方向のみ)
    return _carousel(bubbles)


def _pack_carousels(bubbles: list[tuple[dict, int, str]],
                    max_messages: int) -> list[tuple[list[dict], list[str]]]:
    """バブルを 12枚・48000B 以内のカルーセルに順に詰める。max_messages を超える分は送れないので、
    最後のカルーセル末尾に「ほか N 件は省略」を出す(黙って消さない)。"""
    cars: list[list[tuple[dict, int, str]]] = []
    cur: list[tuple[dict, int, str]] = []
    for entry in bubbles:
        if cur and (len(cur) >= CAROUSEL_MAX_BUBBLES
                    or _byte_size(_carousel([b for b, _, _ in cur + [entry]])) > CAROUSEL_MAX_BYTES):
            cars.append(cur)
            cur = []
        cur.append(entry)
    if cur:
        cars.append(cur)

    kept = cars[:max_messages]
    dropped = sum(n for car in cars[max_messages:] for _, n, _ in car)
    if dropped and kept:
        last = kept[-1]
        while True:
            note = (_note_bubble(f"ほか {dropped} 件は省略"), 0, "")
            if (len(last) < CAROUSEL_MAX_BUBBLES
                    and _byte_size(_carousel([b for b, _, _ in last + [note]])) <= CAROUSEL_MAX_BYTES):
                last.append(note)
                break
            dropped += last.pop()[1]

    out: list[tuple[list[dict], list[str]]] = []
    for car in kept:
        genres = list(dict.fromkeys(g for _, _, g in car if g))
        out.append(([b for b, _, _ in car], genres))
    return out


def digest_specs(
    grouped: dict[str, list[NewsItem]], greeting: bool = True, slot: str | None = None,
    digest_date: date | None = None, market: list[dict] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """購読ジャンルの NewsItem 群を配信メッセージ(spec列、最大5)に変換する。

    1通目は要点バブル(日付見出し・ジャンル別件数・今日の要点5本・市況)。2通目以降は
    ジャンル別カルーセル(grouped の順=特大→各ジャンル、1ジャンル1枚)。
    - 件数は削らない: 全記事がどれかのカルーセルに必ず出る(各ジャンル最低1件も保たれる)。
    - 0件のジャンルは出さない。全ジャンル0件ならテキスト1通。
    - greeting は互換のため残している(見出しは常に同じ)。
    """
    total = sum(len(items) for items in grouped.values())
    if not total:
        return [text_spec("本日は対象ジャンルのニュースが見つかりませんでした。")]

    now = now or datetime.now(ZoneInfo(DEFAULT_TZ))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(DEFAULT_TZ))

    bubbles: list[tuple[dict, int, str]] = []
    for genre, items in grouped.items():
        if items:
            bubbles += [(b, n, genre) for b, n in _genre_bubbles(genre, items, digest_date, slot, now)]
    carousels = _pack_carousels(bubbles, MAX_MESSAGES - 1)

    d = digest_date or now.date()
    slot_word = "夜" if slot == "evening" else "朝"
    heading = f"{d.month}月{d.day}日({_WEEKDAYS[d.weekday()]}) {slot_word}のニュース"
    points = _pick_points(grouped)
    alt = f"{slot_word}のニュース｜{points[0].title}" + (f" ほか{total - 1}件" if total > 1 else "")

    specs: list[dict] = [{
        "type": "flex", "alt": alt[:ALT_MAX_CHARS],
        "contents": _guard_flex(_summary_bubble(grouped, points, market, heading, digest_date, slot,
                                                has_carousel=bool(carousels))),
    }]
    for bubble_list, genres in carousels:
        alt_c = "ジャンル別ニュース（" + "・".join(_genre_label(g) for g in genres) + "）"
        specs.append({"type": "flex", "alt": alt_c[:ALT_MAX_CHARS],
                      "contents": _guard_flex(_carousel(bubble_list))})
    return specs


# LINE のテキストメッセージ上限は5000字。余裕を持たせて切る。
DETAIL_MAX_CHARS = 4800


def detail_spec(item: NewsItem) -> dict:
    """「詳細を読む」タップで返す本文。見出しの再掲で終わらせず長め解説(detail。
    無ければ summary)を載せる。出典は本文を載せず、媒体名/@ハンドルと URL だけ残す。"""
    lines = [f"【{_tag_text(item)}】{item.title}"]

    body = item.detail or item.summary
    if body:
        lines.append("")
        lines.append(body)

    if item.source_tweets:
        src = [" ".join(p for p in (_source_name(s), s.get("url", "")) if p)
               for s in item.source_tweets[:3]]
    else:
        src = list(item.source_urls[:3])
    src = [s for s in src if s]
    if src:
        lines.append("")
        lines.append("出典")
        lines += [f"・{s}" for s in src]

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
