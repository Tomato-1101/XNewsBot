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
import logging
import re
import urllib.parse
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .genres import ALWAYS_KEYS, GENRES, SELECTABLE_KEYS
from .models import SLOT_LABEL, NewsItem, Subscriber

log = logging.getLogger(__name__)

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


# ---- ニュース配信(要点バブル + 主なニュース + 注目ニュース + その他の見出し) ----
#
# 1回の push は「要点バブル1通 + 主なニュース + 注目ニュース + その他の見出し(+溢れたら続き1通)」で
# 最大5メッセージ。LINE の通数は宛先人数で数えるので、5メッセージ以内なら通数は増えない。

# ヘッダー背景(白文字)と白背景の文字の両方に使うので、白とのコントラスト 4.5 以上の濃さにする
GENRE_COLORS = {"特大": "#D32F2F", "AI": "#4F46E5", "株": "#0F766E", "テクノロジー": "#0369A1",
                "暗号資産": "#B45309", "話題": "#A21CAF"}
OTHER_COLOR = "#475569"   # 上記以外のジャンル
TITLE_COLOR = "#111111"
TEXT_COLOR = "#222222"
SUB_COLOR = "#888888"
META_COLOR = "#999999"
RULE_COLOR = "#EEEEEE"
UP_COLOR = "#C62828"
DOWN_COLOR = "#1565C0"
POINTS_MAX = 5
SCHEDULE_MAX = 12        # 今日の予定の最大行数
# 時刻が「21:30」「翌03:00」でない予定(昼ごろ・寄り前・引け後)の at は近似なので、過ぎても3時間は出す
SCHEDULE_APPROX_GRACE = timedelta(hours=3)
SMALL_SUMMARY_MAX = 100  # 小ニュースの要約の表示上限(字)。超えたら「…」で切る
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
    elif kind == "crypto":
        close_s = f"${close:,.0f}"
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


def _schedule_row(ev: dict) -> dict | None:
    """予定1行: 左に時刻、右に名前(その下に予想・前回)。名前が無い行は出さない。"""
    name = str(ev.get("name") or "")
    if not name:
        return None
    right: list[dict] = [{"type": "text", "text": name, "size": "xs", "color": TEXT_COLOR,
                          "wrap": True}]
    figs = "｜".join(f"{lab} {v}" for lab, v in (("予想", ev.get("forecast")),
                                                  ("前回", ev.get("previous"))) if v)
    if figs:
        right.append({"type": "text", "text": figs, "size": "xxs", "color": META_COLOR,
                      "wrap": True})
    # 空文字の text は LINE が拒否し push 全体が落ちるので、時刻が無い行は「未定」にする
    return {"type": "box", "layout": "horizontal", "margin": "sm", "spacing": "md", "contents": [
        {"type": "text", "text": str(ev.get("time_label") or "未定"), "size": "xs",
         "color": SUB_COLOR, "flex": 1},
        {"type": "box", "layout": "vertical", "flex": 4, "contents": right},
    ]}


def _schedule_rows(schedule: list[dict] | None, now: datetime) -> list[dict]:
    """今日の予定の行(最大 SCHEDULE_MAX)。at が now より前の予定は出さない(at が無い予定は出す。
    時刻が近似の予定は SCHEDULE_APPROX_GRACE だけ猶予)。多いときは重要度の高いものを残し
    (FOMC などが早い時刻の決算に押し出されないように)、at 順・at 無しは最後に並べる。"""
    upcoming = []
    for ev in schedule or []:
        if not ev.get("name"):
            continue
        at = _parse_time(ev.get("at")) if ev.get("at") else None
        approx = not re.fullmatch(r"翌?\d{1,2}:\d{2}", str(ev.get("time_label") or ""))
        if at is None or at + (SCHEDULE_APPROX_GRACE if approx else timedelta(0)) >= now:
            upcoming.append((at, ev))
    upcoming.sort(key=lambda p: (p[0] is None, p[0] or now))
    # 重要度が同じなら時刻の早い順に残す(sorted は安定)
    keep = sorted(range(len(upcoming)), key=lambda i: -int(upcoming[i][1].get("importance") or 0))
    upcoming = [upcoming[i] for i in sorted(keep[:SCHEDULE_MAX])]
    return [r for r in (_schedule_row(ev) for _at, ev in upcoming) if r]


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


CREDITS_PER_USD = 100_000  # twitterapi.io: 1 USD = 100,000 クレジット
X_USAGE_WARN_DAYS = 7      # 残りがこの日数分(今回の使用量換算)を切ったら「要チャージ」
USAGE_FAILED = "取得できませんでした"


def _usage_row(label: str, text: str, low: bool) -> dict:
    """「残り使用量」の1行。警告時は赤系。"""
    return {"type": "text", "text": f"{label}  {text}", "size": "xs",
            "color": UP_COLOR if low else SUB_COLOR, "margin": "sm", "wrap": True}


def _x_usage_row(x_usage: dict | None) -> dict:
    """「X（ニュース取得）  残り…クレジット（約$…）・あと約N日／今回 …」の1行。

    データが無い/不正でも「取得できませんでした」で必ず出す(行の場所を毎回同じにして迷わせない)。
    「あと約N日」は 残り ÷ 今回の使用量(使用量0なら出さない)。残りが7日分未満なら赤系で「要チャージ: 」を付ける。"""
    label = "X（ニュース取得）"
    try:
        used, remaining = int(x_usage["used"]), int(x_usage["remaining"])
    except (TypeError, KeyError, ValueError):
        return _usage_row(label, USAGE_FAILED, False)
    text = f"残り {remaining:,}クレジット（約${remaining / CREDITS_PER_USD:.2f}）"
    low = False
    if used > 0:
        days = remaining // used
        text += f"・あと約{days}日"
        low = days < X_USAGE_WARN_DAYS
    text += f"／今回 {used:,}"
    if low:
        text = "要チャージ: " + text
    return _usage_row(label, text, low)


LINE_QUOTA_WARN_RUNS = 7   # 今月の残りがこの回数分(今回の配信コスト換算)を切ったら「要注意」


def _line_quota_row(line_quota: dict | None) -> dict:
    """「LINE（配信）  今月 残り…/…通・あと約N回／今回 …通」の1行。無い/不正なら「取得できませんでした」。

    残り = limit - used - cost(この配信を送った後の残り。負なら 0)。あと約N回 = 残り ÷ cost。
    残り回数が7回未満なら赤系で「要注意: 」を付ける。"""
    label = "LINE（配信）"
    try:
        limit, used, cost = (int(line_quota[k]) for k in ("limit", "used", "cost"))
    except (TypeError, KeyError, ValueError):
        return _usage_row(label, USAGE_FAILED, False)
    if cost <= 0:
        return _usage_row(label, USAGE_FAILED, False)
    remaining = max(limit - used - cost, 0)
    runs = remaining // cost
    text = f"今月 残り {remaining}/{limit}通・あと約{runs}回／今回 {cost}通"
    low = runs < LINE_QUOTA_WARN_RUNS
    if low:
        text = "要注意: " + text
    return _usage_row(label, text, low)


def _summary_bubble(grouped, points, market, schedule, heading: str, digest_date, slot,
                    now: datetime, guide: str | None, x_usage: dict | None = None,
                    line_quota: dict | None = None) -> dict:
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
        if any(m.get("kind") == "crypto" and _market_row(m) for m in market):
            contents.append({"type": "text", "text": "仮想通貨は直近値・24時間比", "size": "xxs",
                             "color": META_COLOR, "margin": "sm", "wrap": True})

    sched = _schedule_rows(schedule, now)
    if sched:
        contents.append(_sep("xl"))
        contents.append({"type": "text", "text": "今日の予定", "size": "sm", "weight": "bold",
                         "color": TITLE_COLOR, "margin": "lg"})
        contents += sched

    if rows or sched:
        contents.append({"type": "text", "text": "評価は一般的な傾向で、投資助言ではありません",
                         "size": "xxs", "color": META_COLOR, "margin": "lg", "wrap": True})

    # 残り使用量は他の見出しと同じ書式の節にし、データが無くても必ず出す(どこにあるか迷わせない)
    contents.append(_sep("xl"))
    contents.append({"type": "text", "text": "残り使用量", "size": "sm", "weight": "bold",
                     "color": TITLE_COLOR, "margin": "lg"})
    contents.append(_x_usage_row(x_usage))
    contents.append(_line_quota_row(line_quota))

    if guide:
        contents.append({"type": "text", "text": guide,
                         "size": "xxs", "color": META_COLOR, "margin": "xl", "wrap": True})
    bubble = {"type": "bubble", "size": "giga",
              "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": contents}}
    return _fit_component(bubble, BUBBLE_MAX_BYTES)


# -- 主なニュース/注目ニュース/その他の見出しのカルーセル --

# 主以外の記事を score で2段に分ける境目。curate_prompt の目安で 50台=押さえておきたい
NOTABLE_MIN_SCORE = 50
NOTABLE_PER_BUBBLE = 5   # 注目ニュースは要約つきなので1枚5件まで(縦に長くしない)
BRIEF_PER_BUBBLE = 8     # その他の見出しは見出しと出典だけで1行が軽いので1枚8件まで
MAIN_TITLE = "主なニュース"
NOTABLE_TITLE = "注目ニュース"
BRIEF_TITLE = "その他の見出し"


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


def _notable_row(item: NewsItem, action: dict, now: datetime) -> dict:
    """注目ニュースの1行(行全体のタップで詳細): 見出し(太字) → 要約(長ければ「…」) → 出典・時刻。"""
    contents: list[dict] = [
        {"type": "text", "text": item.title, "size": "sm", "weight": "bold", "color": TITLE_COLOR,
         "wrap": True},
    ]
    if item.summary:
        summary = item.summary
        if len(summary) > SMALL_SUMMARY_MAX:
            summary = summary[:SMALL_SUMMARY_MAX].rstrip() + "…"
        contents.append({"type": "text", "text": summary, "size": "xs", "color": "#555555",
                         "wrap": True, "margin": "xs"})
    meta = _meta_text(item, now)
    if meta:
        contents.append({"type": "text", "text": meta, "size": "xxs", "color": META_COLOR,
                         "margin": "xs"})
    return {"type": "box", "layout": "vertical", "margin": "md", "action": action,
            "contents": contents}


def _brief_row(item: NewsItem, action: dict, now: datetime) -> dict:
    """その他の見出しの1行: 見出し → 出典・時刻(1行)。要約は出さず、タップで詳細(要約と出典)を開く。
    1行を軽くして1枚に8件並べるため。"""
    contents: list[dict] = [
        {"type": "text", "text": item.title, "size": "sm", "color": TEXT_COLOR, "wrap": True},
    ]
    meta = _meta_text(item, now)
    if meta:
        contents.append({"type": "text", "text": meta, "size": "xxs", "color": META_COLOR})
    return {"type": "box", "layout": "vertical", "margin": "md", "action": action,
            "contents": contents}


def _split_main_others(grouped: dict[str, list[NewsItem]]
                       ) -> tuple[dict[str, list[NewsItem]], dict[str, list[NewsItem]]]:
    """grouped を「主なニュース」(big)と「ほかのニュース」(残り)に分ける。ジャンル順は grouped のまま、
    ジャンル内は rank 順。big が0件のジャンルは rank 最上位の1件を主へ上げ、その記事はほか側から除く
    (どのジャンルも主なニュースに顔を出すように)。0件のジャンルはどちらにも入れない。"""
    main: dict[str, list[NewsItem]] = {}
    others: dict[str, list[NewsItem]] = {}
    for genre, items in grouped.items():
        if not items:
            continue
        ordered = sorted(items, key=lambda it: it.rank)
        tops = [it for it in ordered if it.importance == "big"] or ordered[:1]
        # NewsItem は値で == 比較されるので、同じ記事かどうかは id() で見る
        top_ids = {id(it) for it in tops}
        main[genre] = tops
        rest = [it for it in ordered if id(it) not in top_ids]
        if rest:
            others[genre] = rest
    return main, others


def _split_tiers(others: dict[str, list[NewsItem]]
                 ) -> tuple[dict[str, list[NewsItem]], dict[str, list[NewsItem]]]:
    """主以外の記事を「注目ニュース」(score >= NOTABLE_MIN_SCORE)と「その他の見出し」(残り)に分ける。
    ジャンル順・ジャンル内の並びはそのまま。記事が無いジャンルはその段に入れない。"""
    notable: dict[str, list[NewsItem]] = {}
    brief: dict[str, list[NewsItem]] = {}
    for genre, items in others.items():
        hi = [it for it in items if (it.score or 0) >= NOTABLE_MIN_SCORE]
        lo = [it for it in items if (it.score or 0) < NOTABLE_MIN_SCORE]
        if hi:
            notable[genre] = hi
        if lo:
            brief[genre] = lo
    return notable, brief


def _pages(units: list[tuple[list, list]], per_page: int, measure) -> list[tuple[list[dict], int]]:
    """記事1件ごとの (先頭の区切り線, 本体) を、per_page 件以内かつ 28000B 以内のページ
    (本文, 載せた記事数) に記事単位で分ける。新しいページの先頭には区切り線を置かない。
    measure(本文) はその本文を入れたバブルのバイト数。単体で上限を超える記事は切り詰める。"""
    budget = BUBBLE_MAX_BYTES - measure([]) - 16  # 16: 配列の区切り文字ぶんの余裕
    pages: list[tuple[list[dict], int]] = []
    cur: list[dict] = []
    n = 0
    for lead, body in units:
        body = _fit_component(body, budget - _byte_size(lead))  # 単体で上限超過なら切り詰める
        if cur and (n >= per_page or measure(cur + lead + body) > BUBBLE_MAX_BYTES):
            pages.append((cur, n))
            cur, n = list(body), 1
        else:
            cur = (cur + lead + body) if cur else list(body)
            n += 1
    if cur:
        pages.append((cur, n))
    return pages


def _genre_bubble(genre: str, head: str, note: str, body: list[dict]) -> dict:
    """ジャンル色ヘッダー(左にジャンル名、右に「主なニュース」)のバブル。"""
    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "horizontal", "backgroundColor": _genre_color(genre),
                   "paddingAll": "16px", "contents": [
                       {"type": "text", "text": head, "size": "lg", "weight": "bold",
                        "color": "#FFFFFF", "flex": 1},
                       {"type": "text", "text": note, "size": "sm", "color": "#FFFFFF",
                        "align": "end", "gravity": "center"},
                   ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def _genre_bubbles(genre: str, items: list[NewsItem], digest_date, slot,
                   now: datetime) -> list[tuple[dict, int]]:
    """1ジャンルの主なニュースを (バブル, 載せた記事数) の列にする。
    件数は削らず、28000B を超えるときだけ記事単位で次のバブルへ送る(見出しは「AI (1/2)」)。"""
    label = _genre_label(genre)
    note = MAIN_TITLE
    color = _genre_color(genre)
    units = [([_sep("lg")] if i else [],
              [_big_block(it, color, _detail_action(it, digest_date, slot), now)])
             for i, it in enumerate(items)]
    pages = _pages(units, len(items),
                   lambda body: _byte_size(_genre_bubble(genre, f"{label} (00/00)", note, body)))
    if len(pages) == 1:
        return [(_genre_bubble(genre, label, note, pages[0][0]), pages[0][1])]
    return [(_genre_bubble(genre, f"{label} ({k}/{len(pages)})", note, body), cnt)
            for k, (body, cnt) in enumerate(pages, 1)]


def _list_bubble(genre: str, note: str, body: list[dict]) -> dict:
    """注目ニュース/その他の見出しの1ページ。ヘッダーは主なニュースより低くし、
    左にジャンル名・右に「注目 1/3」「その他 6件」を出す。"""
    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "horizontal", "backgroundColor": _genre_color(genre),
                   "paddingAll": "12px", "contents": [
                       {"type": "text", "text": _genre_label(genre), "size": "md", "weight": "bold",
                        "color": "#FFFFFF", "flex": 1},
                       {"type": "text", "text": note, "size": "sm", "color": "#FFFFFF",
                        "align": "end", "gravity": "center"},
                   ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def _tier_bubbles(genre: str, items: list[NewsItem], notable: bool, digest_date, slot,
                  now: datetime) -> list[tuple[dict, int]]:
    """1ジャンルの注目ニュース(notable=True)かその他の見出しを、ページ単位のバブル
    (バブル, 載せた記事数) の列にする。注目は1枚5件・その他は1枚8件まで(28000B も守る)。
    ヘッダー右は複数ページなら「注目 1/3」、1ページなら「注目 4件」。"""
    word, per, row = (("注目", NOTABLE_PER_BUBBLE, _notable_row) if notable
                      else ("その他", BRIEF_PER_BUBBLE, _brief_row))
    units = [([_sep("md", "#F2F2F2")] if i else [],
              [row(it, _detail_action(it, digest_date, slot), now)])
             for i, it in enumerate(items)]
    pages = _pages(units, per, lambda body: _byte_size(_list_bubble(genre, f"{word} 00/00", body)))
    if len(pages) == 1:
        return [(_list_bubble(genre, f"{word} {pages[0][1]}件", pages[0][0]), pages[0][1])]
    return [(_list_bubble(genre, f"{word} {k}/{len(pages)}", body), cnt)
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


# (バブル, 載せた記事数, ジャンル, 段の名前)。省略注記は (注記, 0, "", "")
Entry = tuple[dict, int, str, str]


def _chunk(entries: list[Entry]) -> list[list[Entry]]:
    """バブルを順に 12枚・48000B 以内のカルーセルに詰める(通数の上限は見ない)。"""
    cars: list[list[Entry]] = []
    cur: list[Entry] = []
    for entry in entries:
        if cur and (len(cur) >= CAROUSEL_MAX_BUBBLES
                    or _byte_size(_carousel([e[0] for e in cur + [entry]])) > CAROUSEL_MAX_BYTES):
            cars.append(cur)
            cur = []
        cur.append(entry)
    if cur:
        cars.append(cur)
    return cars


def _pack_carousels(entries: list[Entry], max_messages: int) -> list[list[Entry]]:
    """バブルを 12枚・48000B 以内のカルーセルに順に詰める。max_messages を超える分は送れないので、
    最後のカルーセル末尾に「ほか N 件は省略」を出す(黙って消さない)。"""
    cars = _chunk(entries)
    kept = cars[:max_messages]
    dropped = sum(e[1] for car in cars[max_messages:] for e in car)
    if dropped and kept:
        last = kept[-1]
        while True:
            note = (_note_bubble(f"ほか {dropped} 件は省略"), 0, "", "")
            if (len(last) < CAROUSEL_MAX_BUBBLES
                    and _byte_size(_carousel([e[0] for e in last + [note]])) <= CAROUSEL_MAX_BYTES):
                last.append(note)
                break
            dropped += last.pop()[1]
    return kept


def _tier_entries(section: dict[str, list[NewsItem]], notable: bool, digest_date, slot,
                  now: datetime) -> list[Entry]:
    title = NOTABLE_TITLE if notable else BRIEF_TITLE
    return [(b, n, genre, title) for genre, items in section.items()
            for b, n in _tier_bubbles(genre, items, notable, digest_date, slot, now)]


def _tier_carousels(tiers: list[list[Entry]], slots: int) -> list[list[Entry]]:
    """注目ニュース・その他の見出し(tiers は空でない段だけ、注目→その他の順)を slots 通以内にする。
    全部入るなら段ごとに順に並べる。入らないときは各段の1通目を出し、続きは段を混ぜて
    並び順(注目→その他)のまま残りの通数に詰める。それでも入らない分は最後に「ほか N 件は省略」。
    呼び出し側が slots >= len(tiers) を保証する(主なニュースに段の数だけ枠を残している)。"""
    chunked = [_chunk(entries) for entries in tiers]
    if sum(len(c) for c in chunked) <= slots:
        return [car for c in chunked for car in c]
    if slots <= len(tiers):  # 続きを出す枠が無い: 各段1通に収め、入らない分はその段の末尾で省略
        return [_pack_carousels(entries, 1)[0] for entries in tiers]
    rest = [e for c in chunked for car in c[1:] for e in car]
    return [c[0] for c in chunked] + _pack_carousels(rest, slots - len(tiers))


def digest_specs(
    grouped: dict[str, list[NewsItem]], greeting: bool = True, slot: str | None = None,
    digest_date: date | None = None, market: list[dict] | None = None,
    now: datetime | None = None, schedule: list[dict] | None = None,
    x_usage: dict | None = None, line_quota: dict | None = None,
) -> list[dict]:
    """購読ジャンルの NewsItem 群を配信メッセージ(spec列、最大5)に変換する。

    1通目は要点バブル(日付見出し・ジャンル別件数・今日の要点5本・市況・今日の予定・残り使用量)。
    2通目は「主なニュース」カルーセル(grouped の順=特大→各ジャンル、1ジャンル1枚に big をまとめる)。
    続いて主以外の記事を score で分けた「注目ニュース」(score >= 50、要約つき・1枚5件)と
    「その他の見出し」(見出しと出典だけ・1枚8件)のカルーセル。どちらもジャンル順→ページ順に横へ並べる。
    - big が0件のジャンルは rank 最上位の1件を主なニュースに上げる(どのジャンルも2通目に出る)。
    - 注目・その他が1通に収まらないときだけ、続きをもう1通(段を混ぜて注目→その他の順)。
    - 件数は黙って削らない: 入りきらない分は最後に「ほか N 件は省略」。記事が無い段は出さない。
    - 0件のジャンルは出さない。全ジャンル0件ならテキスト1通。
    - greeting は互換のため残している(見出しは常に同じ)。
    """
    total = sum(len(items) for items in grouped.values())
    if not total:
        return [text_spec("本日は対象ジャンルのニュースが見つかりませんでした。")]

    now = now or datetime.now(ZoneInfo(DEFAULT_TZ))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(DEFAULT_TZ))

    main, others = _split_main_others(grouped)
    notable, brief = _split_tiers(others)
    tiers = [entries for entries in (_tier_entries(notable, True, digest_date, slot, now),
                                     _tier_entries(brief, False, digest_date, slot, now)) if entries]
    budget = MAX_MESSAGES - 1  # 1通目は要点バブル
    main_entries = [(b, n, genre, MAIN_TITLE) for genre, items in main.items()
                    for b, n in _genre_bubbles(genre, items, digest_date, slot, now)]
    # 注目・その他は最低1通ずつ残す(主なニュースが送り枠を使い切って、下の段が全部消えないように)
    main_cars = _pack_carousels(main_entries, budget - len(tiers))
    tier_cars = _tier_carousels(tiers, budget - len(main_cars)) if tiers else []

    d = digest_date or now.date()
    slot_word = "夜" if slot == "evening" else "朝"
    heading = f"{d.month}月{d.day}日({_WEEKDAYS[d.weekday()]}) {slot_word}のニュース"
    points = _pick_points(grouped)
    alt = f"{slot_word}のニュース｜{points[0].title}" + (f" ほか{total - 1}件" if total > 1 else "")
    guide = None
    if main_cars:
        titles = [MAIN_TITLE] + [NOTABLE_TITLE] * bool(notable) + [BRIEF_TITLE] * bool(brief)
        if len(titles) == 1:
            guide = f"次: {MAIN_TITLE}（ジャンルごとに横へスワイプ）"
        else:
            guide = "次: " + " → ".join(titles) + "（どれもジャンルごとに横へスワイプ）"

    specs: list[dict] = [{
        "type": "flex", "alt": alt[:ALT_MAX_CHARS],
        "contents": _guard_flex(_summary_bubble(grouped, points, market, schedule, heading,
                                                digest_date, slot, now, guide=guide,
                                                x_usage=x_usage, line_quota=line_quota)),
    }]
    for car in main_cars + tier_cars:
        # 続きの1通は段が混ざることがあるので、含まれる段の名前を並べる
        titles = list(dict.fromkeys(e[3] for e in car if e[3]))
        genres = list(dict.fromkeys(e[2] for e in car if e[2]))
        alt_c = "・".join(titles) + "（" + "・".join(_genre_label(g) for g in genres) + "）"
        specs.append({"type": "flex", "alt": alt_c[:ALT_MAX_CHARS],
                      "contents": _guard_flex(_carousel([e[0] for e in car]))})
    return specs


# LINE のテキストメッセージ上限は5000字。余裕を持たせて切る。
DETAIL_MAX_CHARS = 4800


def detail_spec(item: NewsItem, payload: str | None = None) -> dict:
    """「詳細を読む」タップで返す本文。見出しの再掲で終わらせず長め解説(detail。
    無ければ summary)を載せる。出典は本文を載せず、媒体名/@ハンドルと URL だけ残す。

    下に「AI解説」のクイックリプライを付ける。payload は詳細タップと同じ記事キー
    (無ければ id)で、押すと postback `explain:<payload>` が来る(onboarding._handle_explain)。"""
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
    key = payload if payload is not None else str(item.id)
    return text_spec(text, [_qr("AI解説", f"explain:{key}")])


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


_QUOTA_TIMEOUT = (5, 10)  # (接続, 読み取り) 秒。linebot.v3 の _request_timeout にそのまま渡す


class LineMessenger:
    """実 LINE 送信。spec のリストを受けて reply/push する。"""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def _api(self):
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi
        return MessagingApi(ApiClient(Configuration(access_token=self._access_token)))

    def fetch_quota(self, to: str) -> dict | None:
        """今月の LINE 無料枠(上限・使用数)と、この配信の消費通数を返す。取得系は無料 API。

        消費通数は push 1回 × 宛先人数: グループ(C…)/ルーム(R…)は人数、ユーザーは1。
        上限なし(type=none)や取得失敗は None(行を出さないだけで、配信は止めない)。"""
        # 応答が止まると後続の push(配信本体)まで止まるので、接続5秒・読み取り10秒で打ち切る
        # (時間切れは下の except で None=残量の行を出さないだけ)。
        t = _QUOTA_TIMEOUT
        try:
            api = self._api()
            quota = api.get_message_quota(_request_timeout=t)
            if str(getattr(quota.type, "value", quota.type)) != "limited":
                return None
            used = api.get_message_quota_consumption(_request_timeout=t).total_usage
            if to.startswith("C"):
                cost = api.get_group_member_count(to, _request_timeout=t).count
            elif to.startswith("R"):
                cost = api.get_room_member_count(to, _request_timeout=t).count
            else:
                cost = 1
            return {"limit": int(quota.value), "used": int(used), "cost": int(cost)}
        except Exception:  # noqa: BLE001 — 残量表示のために配信を止めない
            log.warning("LINE の残り通数を取得できませんでした", exc_info=True)
            return None

    # 以下2つは AI解説の通数ガード用。fetch_quota を宛先ごとに呼ぶと上限・使用数を宛先の数だけ
    # 取り直すので、上限・使用数(1回)と人数(宛先ごと)を分けて取れるようにする。
    def fetch_limit_used(self) -> dict | None:
        """今月の上限と使用数 {"limit", "used"}。上限なし・取得失敗は None。"""
        t = _QUOTA_TIMEOUT
        try:
            api = self._api()
            quota = api.get_message_quota(_request_timeout=t)
            if str(getattr(quota.type, "value", quota.type)) != "limited":
                return None
            used = api.get_message_quota_consumption(_request_timeout=t).total_usage
            return {"limit": int(quota.value), "used": int(used)}
        except Exception:  # noqa: BLE001 — 取れなければ呼び出し側が安全側(送らない)に倒す
            log.warning("LINE の上限・使用数を取得できませんでした", exc_info=True)
            return None

    def fetch_member_count(self, to: str) -> int | None:
        """to へ push 1回で消費する通数(グループ/ルームは人数、ユーザーは1)。取得失敗は None。"""
        if not (to.startswith("C") or to.startswith("R")):
            return 1
        t = _QUOTA_TIMEOUT
        try:
            api = self._api()
            if to.startswith("C"):
                return int(api.get_group_member_count(to, _request_timeout=t).count)
            return int(api.get_room_member_count(to, _request_timeout=t).count)
        except Exception:  # noqa: BLE001
            log.warning("LINE の人数を取得できませんでした to=%s", to, exc_info=True)
            return None

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
