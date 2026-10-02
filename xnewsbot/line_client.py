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


# ---- ニュース配信(1通目=要点・マーケット、2通目以降=ジャンルごとのカルーセル) ----
#
# 1回の push は「1通目(要点＋マーケットの2枚) + ジャンルごとにまとめたカルーセル」で最大5メッセージ。
# LINE の通数は push 1回 × 宛先人数で数えるので、5メッセージ以内なら通数は増えない。
# 本人は主に LINE の PC 版で読み、PC 版のカルーセルは矢印クリックで1枚ずつ送る(スワイプ不可)ので、
# 1ジャンルのカードは横に連続して並べ、ジャンル内で 大きいニュース → 注目 → その他の見出し と途切れず流す。

# ヘッダー背景(白文字)と白背景の文字の両方に使うので、白とのコントラスト 4.5 以上の濃さにする。
# 廃止したジャンル(特大など)の過去記事は OTHER_COLOR で出す。
GENRE_COLORS = {"AI": "#4F46E5", "株": "#0F766E", "テクノロジー": "#0369A1",
                "暗号資産": "#B45309", "話題": "#A21CAF"}
OTHER_COLOR = "#475569"   # 上記以外のジャンル
TITLE_COLOR = "#111111"
TEXT_COLOR = "#222222"
SUB_COLOR = "#888888"
META_COLOR = "#999999"
RULE_COLOR = "#EEEEEE"
UP_COLOR = "#C62828"
DOWN_COLOR = "#1565C0"
# 決算サプライズの値動き(本人の指定で上昇=緑系・下落=赤系。市況の前日比とは別の配色)
SURPRISE_UP_COLOR = "#15803D"
SURPRISE_DOWN_COLOR = "#B91C1C"
POINTS_MAX = 5
SCHEDULE_MAX = 12        # 今日の予定の最大行数
# 注目決算・決算サプライズの1国あたりの最大行数(マーケットのカードを PC で縦に長くしすぎないため)
EARNINGS_MAX_PER_COUNTRY = 8
SURPRISE_MAX_PER_COUNTRY = 5
_COUNTRY_LABELS = {"JP": "日本", "US": "米国"}
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
    """今日の要点(最大5本): 常時ジャンル(genres.toml の selectable=false)の big をすべて先頭
    → 他ジャンルの big を score 降順 → 足りなければ small を score 降順。同点はジャンル順・rank 順。
    2026-10-02 に特大を廃止して常時ジャンルは無くなったので、今は big の score 順 → small の score 順。"""
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


def _upcoming(schedule: list[dict] | None, now: datetime) -> list[tuple[datetime | None, dict]]:
    """(at, 予定) を at 順(at 無しは最後)に。at が now より前の予定は出さない(at が無い予定は出す。
    時刻が近似の予定は SCHEDULE_APPROX_GRACE だけ猶予)。名前の無い予定は除く。"""
    upcoming = []
    for ev in schedule or []:
        if not ev.get("name"):
            continue
        at = _parse_time(ev.get("at")) if ev.get("at") else None
        approx = not re.fullmatch(r"翌?\d{1,2}:\d{2}", str(ev.get("time_label") or ""))
        if at is None or at + (SCHEDULE_APPROX_GRACE if approx else timedelta(0)) >= now:
            upcoming.append((at, ev))
    upcoming.sort(key=lambda p: (p[0] is None, p[0] or now))
    return upcoming


def _schedule_rows(schedule: list[dict] | None, now: datetime) -> list[dict]:
    """今日の予定の行(最大 SCHEDULE_MAX。並びと時刻の絞り込みは _upcoming)。多いときは重要度の
    高いものを残す(FOMC などが早い時刻の予定に押し出されないように)。"""
    upcoming = _upcoming(schedule, now)
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


MARKET_HINT = "市況・今日の予定・決算は右のカード →"
DISCLAIMER = "評価は一般的な傾向で、投資助言ではありません"


def _section_title(text: str) -> list[dict]:
    """節の見出し(区切り線つき)。"""
    return [_sep("xl"), {"type": "text", "text": text, "size": "sm", "weight": "bold",
                         "color": TITLE_COLOR, "margin": "lg", "wrap": True}]


def _sub_title(text: str) -> dict:
    """節の中の小見出し(日本/米国)。"""
    return {"type": "text", "text": text, "size": "xs", "weight": "bold", "color": SUB_COLOR,
            "margin": "md"}


def _msg_range(nos: list[int]) -> str:
    """目次の行き先: 「→ 2通目」「→ 2〜3通目」。全部省略されたジャンルは「→ 省略」。"""
    if not nos:
        return "→ 省略"
    lo, hi = min(nos), max(nos)
    return f"→ {lo}通目" if lo == hi else f"→ {lo}〜{hi}通目"


def _toc_row(genre: str, count: int, nos: list[int], dropped: int) -> dict:
    """目次1行: ●(ジャンル色) ジャンル名 / N件 / → k通目(省略があれば件数も)。"""
    color = _genre_color(genre)
    where = _msg_range(nos) + (f"（{dropped}件省略）" if dropped and nos else "")
    return {"type": "box", "layout": "horizontal", "margin": "sm", "spacing": "sm", "contents": [
        {"type": "text", "text": "●", "size": "xs", "color": color, "flex": 0, "gravity": "center"},
        {"type": "text", "text": _genre_label(genre), "size": "sm", "weight": "bold", "color": color,
         "flex": 3},
        {"type": "text", "text": f"{count}件", "size": "sm", "color": TEXT_COLOR, "align": "end",
         "flex": 2},
        {"type": "text", "text": where, "size": "sm", "color": SUB_COLOR, "align": "end", "flex": 5,
         "wrap": True},
    ]}


def _summary_bubble(toc: list[tuple[str, int, list[int], int]], points: list[NewsItem],
                    heading: str, total: int, digest_date, slot, has_market: bool,
                    x_usage: dict | None = None, line_quota: dict | None = None) -> dict:
    """1通目の1枚目: 見出し → 目次(ジャンルごとの件数と何通目か) → 今日の要点 → 残り使用量。"""
    contents: list[dict] = [
        {"type": "text", "text": heading, "size": "lg", "weight": "bold", "color": TITLE_COLOR},
        {"type": "text", "text": f"計{total}件", "size": "xs", "color": SUB_COLOR},
        {"type": "text", "text": "目次", "size": "sm", "weight": "bold", "color": TITLE_COLOR,
         "margin": "xl"},
    ]
    contents += [_toc_row(*row) for row in toc]
    if has_market:
        contents.append({"type": "text", "text": MARKET_HINT, "size": "xxs", "color": META_COLOR,
                         "margin": "md", "wrap": True})

    contents += _section_title("今日の要点")
    contents += [_point_row(i, it, digest_date, slot) for i, it in enumerate(points, 1)]

    # 残り使用量は他の見出しと同じ書式の節にし、データが無くても必ず出す(どこにあるか迷わせない)
    contents += _section_title("残り使用量")
    contents.append(_x_usage_row(x_usage))
    contents.append(_line_quota_row(line_quota))

    bubble = {"type": "bubble", "size": "giga",
              "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": contents}}
    return _fit_component(bubble, BUBBLE_MAX_BYTES)


def _by_country(items: list[dict]) -> list[tuple[str, list[dict]]]:
    """国ごとに分ける(日本 → 米国 → その他は出てきた順)。国の中の並びは items のまま。"""
    order = ["JP", "US"] + [c for c in dict.fromkeys(str(ev.get("country") or "") for ev in items)
                            if c not in ("JP", "US")]
    groups = [(c, [ev for ev in items if str(ev.get("country") or "") == c]) for c in order]
    return [(c, evs) for c, evs in groups if evs]


def _country_label(code: str) -> str:
    return _COUNTRY_LABELS.get(code, code or "その他")


def _rest_line(n: int, unit: str) -> dict:
    return {"type": "text", "text": f"ほか {n} {unit}", "size": "xxs", "color": META_COLOR,
            "margin": "sm"}


def _earnings_section(schedule: list[dict], now: datetime) -> list[dict]:
    """注目決算(kind="earnings")を日本・米国に分けて「引け後  Apple（AAPL）」の行に。
    IRBANK の上位で代用した項目(fallback)があれば見出しに「（時価総額上位で代用）」を付ける。
    1国 EARNINGS_MAX_PER_COUNTRY 行を超える分は「ほか N 社」にする(黙って消さない)。"""
    earn = [ev for _at, ev in _upcoming([ev for ev in schedule if ev.get("kind") == "earnings"], now)]
    if not earn:
        return []
    title = "注目決算" + ("（時価総額上位で代用）" if any(ev.get("fallback") for ev in earn) else "")
    out = _section_title(title)
    for country, evs in _by_country(earn):
        out.append(_sub_title(_country_label(country)))
        # 節の見出しが「注目決算」なので、行の名前の末尾の「決算」は省く
        rows = [_schedule_row({**ev, "name": re.sub(r"\s*決算$", "", str(ev["name"])) or ev["name"]})
                for ev in evs[:EARNINGS_MAX_PER_COUNTRY]]
        out += [r for r in rows if r]
        if len(evs) > EARNINGS_MAX_PER_COUNTRY:
            out.append(_rest_line(len(evs) - EARNINGS_MAX_PER_COUNTRY, "社"))
    return out


def _surprise_row(ev: dict) -> dict | None:
    """決算サプライズ1行: 「▲ +14.8%  グラファイトデザイン（7847）  PTS」、下に小さく決算見出し。
    上昇は緑・下落は赤。騰落率か名前が無い行は出さない。"""
    name = str(ev.get("name") or "")
    try:
        pct = float(ev.get("move_pct"))
    except (TypeError, ValueError):
        return None
    if not name:
        return None
    mark, color = (("▲ ", SURPRISE_UP_COLOR) if pct > 0 else ("▼ ", SURPRISE_DOWN_COLOR) if pct < 0
                   else ("", SUB_COLOR))
    line = [
        {"type": "text", "text": f"{mark}{pct:+.1f}%", "size": "xs", "weight": "bold", "color": color,
         "flex": 0},
        {"type": "text", "text": name, "size": "xs", "color": TEXT_COLOR, "wrap": True, "flex": 1},
    ]
    if ev.get("move_label"):
        line.append({"type": "text", "text": str(ev["move_label"]), "size": "xxs", "color": META_COLOR,
                     "flex": 0, "gravity": "center"})
    contents: list[dict] = [{"type": "box", "layout": "horizontal", "spacing": "md", "contents": line}]
    if ev.get("headline"):
        contents.append({"type": "text", "text": str(ev["headline"]), "size": "xxs",
                         "color": META_COLOR, "wrap": True, "margin": "xs"})
    return {"type": "box", "layout": "vertical", "margin": "sm", "contents": contents}


def _surprise_section(schedule: list[dict]) -> list[dict]:
    """決算サプライズ(kind="surprise"。前営業日の決算への反応)。日本 → 米国、国の中は受け取った順。"""
    rows_by_country = [(c, [r for r in (_surprise_row(ev) for ev in evs) if r])
                       for c, evs in _by_country([ev for ev in schedule if ev.get("kind") == "surprise"])]
    rows_by_country = [(c, rows) for c, rows in rows_by_country if rows]
    if not rows_by_country:
        return []
    out = _section_title("決算サプライズ")
    for country, rows in rows_by_country:
        out.append(_sub_title(_country_label(country)))
        out += rows[:SURPRISE_MAX_PER_COUNTRY]
        if len(rows) > SURPRISE_MAX_PER_COUNTRY:
            out.append(_rest_line(len(rows) - SURPRISE_MAX_PER_COUNTRY, "件"))
    return out


def _market_bubble(market: list[dict] | None, schedule: list[dict] | None,
                   now: datetime) -> dict | None:
    """1通目の2枚目「マーケット」: 市況 → 今日の予定 → 注目決算 → 決算サプライズ → 注記。
    中身の無い節は出さない。どの節も無ければ None(1通目は要点の1枚だけになる)。"""
    schedule = schedule or []
    body: list[dict] = []
    rows = [r for r in (_market_row(m) for m in market or []) if r]
    if rows:
        body += _section_title("市況（前日終値）")
        body += rows
        if any(m.get("kind") == "crypto" and _market_row(m) for m in market):
            body.append({"type": "text", "text": "仮想通貨は直近値・24時間比", "size": "xxs",
                         "color": META_COLOR, "margin": "sm", "wrap": True})
    sched = _schedule_rows([ev for ev in schedule if ev.get("kind") not in ("earnings", "surprise")], now)
    if sched:
        body += _section_title("今日の予定")
        body += sched
    body += _earnings_section(schedule, now)
    body += _surprise_section(schedule)
    if not body:
        return None
    contents = [{"type": "text", "text": "マーケット", "size": "lg", "weight": "bold",
                 "color": TITLE_COLOR}] + body
    contents.append({"type": "text", "text": DISCLAIMER, "size": "xxs", "color": META_COLOR,
                     "margin": "xl", "wrap": True})
    bubble = {"type": "bubble", "size": "giga",
              "body": {"type": "box", "layout": "vertical", "paddingAll": "20px", "contents": contents}}
    return _fit_component(bubble, BUBBLE_MAX_BYTES)


# -- ジャンルごとのカード(大きいニュース → 注目 → その他の見出し) --

# 主以外の記事を score で2段に分ける境目。curate_prompt の目安で 50台=押さえておきたい
NOTABLE_MIN_SCORE = 50
# 1枚に載せる重さの上限(大きいニュース=3・注目=1.5・その他の見出し=1)。バイト数(BUBBLE_MAX_BYTES)も別に守る。
# 縦に長めにして横の枚数を減らす(2026-10-02 実画面の確認: PC 版は横に約5.5枚しか見えず、6枚目以降は
# 矢印を押さないと見えないので見落とす。縦スクロールの方が楽で、縦長の要点カードは読みやすかった)。
PAGE_WEIGHT_MAX = 20.0
# ページ分割の重さを 0.5 刻みで探す(TIER_WEIGHT がすべて 0.5 の倍数なので、これで最小の均し方が見つかる)
_WEIGHT_STEP = 0.5
TIER_WEIGHT = {"big": 3.0, "notable": 1.5, "brief": 1.0}
TIER_HEADINGS = {"notable": "注目", "brief": "その他の見出し"}


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


def _genre_units(items: list[NewsItem]) -> list[tuple[str, NewsItem]]:
    """1ジャンルの記事を (段, 記事) の列に: 大きいニュース(big。無ければ rank 最上位の1件)→
    注目(score >= NOTABLE_MIN_SCORE)→ その他の見出し。段の中は rank 順。"""
    main, others = _split_main_others({"_": items})
    rest = others.get("_", [])
    return ([("big", it) for it in main.get("_", [])]
            + [("notable", it) for it in rest if (it.score or 0) >= NOTABLE_MIN_SCORE]
            + [("brief", it) for it in rest if (it.score or 0) < NOTABLE_MIN_SCORE])


def _page_body(genre: str, units: list[tuple[str, dict]]) -> list[dict]:
    """1枚の本文。段が変わるところ(とページ先頭)に小見出し「注目」「その他の見出し」を置く。
    大きいニュースには小見出しを付けない(ヘッダーのジャンル名がそのまま見出しになる)。"""
    color = _genre_color(genre)
    body: list[dict] = []
    prev = None
    for tier, comp in units:
        if tier != prev:
            if body:
                body.append(_sep("xl"))
            if tier in TIER_HEADINGS:
                head = {"type": "text", "text": TIER_HEADINGS[tier], "size": "xs", "weight": "bold",
                        "color": color}
                if body:
                    head["margin"] = "lg"
                body.append(head)
        else:
            body.append(_sep("lg") if tier == "big" else _sep("md", "#F2F2F2"))
        body.append(comp)
        prev = tier
    return body


def _genre_bubble(genre: str, head: str, note: str, body: list[dict]) -> dict:
    """ジャンル色ヘッダー(左にジャンル名、右に「52件 1/5」)のバブル。"""
    return {
        "type": "bubble", "size": "giga",
        "header": {"type": "box", "layout": "horizontal", "backgroundColor": _genre_color(genre),
                   "paddingAll": "12px", "contents": [
                       {"type": "text", "text": head, "size": "md", "weight": "bold",
                        "color": "#FFFFFF", "flex": 1},
                       {"type": "text", "text": note, "size": "sm", "color": "#FFFFFF",
                        "align": "end", "gravity": "center", "flex": 0},
                   ]},
        "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": body},
    }


def _genre_pages(genre: str, units: list[tuple[str, NewsItem]], total: int, digest_date, slot,
                 now: datetime) -> list[Entry]:
    """1ジャンルの (段, 記事) 列を、重さ PAGE_WEIGHT_MAX・28000B 以内のカードに記事単位で分ける。
    枚数は最小のまま、各カードの重さをなるべく均す(カルーセルのバブルは一番高いバブルに高さが揃うので、
    最後の1枚だけ短いと下が大きな白い空白になる。2026-10-02 実画面で確認)。
    ヘッダー右は「{total}件 k/n」(1枚なら「{total}件」)。total は省略前のジャンルの件数。
    単体で上限を超える記事は切り詰める(_fit_component)。"""
    label = _genre_label(genre)
    color = _genre_color(genre)
    builders = {"big": lambda it, a: _big_block(it, color, a, now),
                "notable": lambda it, a: _notable_row(it, a, now),
                "brief": lambda it, a: _brief_row(it, a, now)}
    if not units:
        return []

    def bubble(page, note):
        return _genre_bubble(genre, label, note, _page_body(genre, [(t, c) for t, c, _it in page]))

    base = _byte_size(bubble([], f"{total}件 00/00"))
    # 小見出し・区切り線のぶん(200B)も残して、1件だけのカードが必ず上限に収まるようにする
    budget = BUBBLE_MAX_BYTES - base - 200
    comps = [(tier, _fit_component(builders[tier](it, _detail_action(it, digest_date, slot)), budget), it)
             for tier, it in units]
    # バイト数は部品ごとの大きさ + 区切り線(1件ごと)・小見出し(段ごと、多くて2つ)のぶんで見積もる
    # (重さの探索で何十回も分け直すので、毎回 JSON 全体を測らない。見積もりは実測より大きめ)
    sizes = [_byte_size(c) + 120 for _t, c, _it in comps]
    byte_room = BUBBLE_MAX_BYTES - base - 2 * 250

    def split(cap: float) -> list[list[tuple[str, dict, NewsItem]]]:
        pages: list[list[tuple[str, dict, NewsItem]]] = []
        cur: list[tuple[str, dict, NewsItem]] = []
        weight = 0.0
        nbytes = 0
        for unit, size in zip(comps, sizes):
            w = TIER_WEIGHT[unit[0]]
            if cur and (weight + w > cap or nbytes + size > byte_room):
                pages.append(cur)
                cur, weight, nbytes = [], 0.0, 0
            cur.append(unit)
            weight += w
            nbytes += size
        if cur:
            pages.append(cur)
        return pages

    pages = split(PAGE_WEIGHT_MAX)
    n = len(pages)
    if n > 1:
        # 同じ枚数に収まる一番小さい重さの上限で分け直す(= 各カードの重さが均される)
        weights = [TIER_WEIGHT[t] for t, _c, _it in comps]
        cap = max(max(weights), sum(weights) / n)
        cap = _WEIGHT_STEP * -(-cap // _WEIGHT_STEP)
        while cap < PAGE_WEIGHT_MAX:
            trial = split(cap)
            if len(trial) <= n:
                pages = trial
                break
            cap += _WEIGHT_STEP
    n = len(pages)
    return [(bubble(page, f"{total}件" if n == 1 else f"{total}件 {k}/{n}"),
             len(page), genre, page[0][2].title)
            for k, page in enumerate(pages, 1)]


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
    # 各バブルが上限内なら _pack_genres の詰め方で 48000B 以内に収まる(差し替えは小さくなる方向のみ)
    return _carousel(bubbles)


# (バブル, 載せた記事数, ジャンル, 先頭の記事の見出し)。省略注記は (注記, 0, "", "")
Entry = tuple[dict, int, str, str]


def _fits(car: list[Entry], extra: list[Entry]) -> bool:
    """car に extra を足しても 12枚・48000B 以内か。"""
    return (len(car) + len(extra) <= CAROUSEL_MAX_BUBBLES
            and _byte_size(_carousel([e[0] for e in car + extra])) <= CAROUSEL_MAX_BYTES)


def _pack_seq(blocks: list[list[Entry]]) -> list[list[Entry]]:
    """カード列を順にカルーセルに詰める(1つのメッセージにまとめると決めたジャンルの並び用)。
    ジャンルのカードが今のカルーセルに丸ごと入るなら相乗りし、入らなければ次のカルーセルから始める。
    1カルーセルに収まらない大きいジャンルは、今のカルーセルの残りから順に詰めて次へ続ける
    (カードは連番なので通をまたいでも続きと分かる)。"""
    cars: list[list[Entry]] = []
    cur: list[Entry] = []
    for block in blocks:
        if _fits(cur, block):
            cur += block
            continue
        if _fits([], block):
            cars.append(cur)
            cur = list(block)
            continue
        for e in block:
            if not _fits(cur, [e]):
                cars.append(cur)
                cur = []
            cur.append(e)
    if cur:
        cars.append(cur)
    return [car for car in cars if car]


def _pack_genres(blocks: list[list[Entry]], slots: int) -> list[list[Entry]]:
    """ジャンルごとのカード列をメッセージ(カルーセル)に割り付ける。
    原則1ジャンル=1メッセージ(縦に並ぶのでジャンル単位で追え、横送りも短く、件数の違うジャンル同士で
    バブルの高さが揃えられて空白が出ることもない。2026-10-02 実画面で確認)。
    通数が slots を超えるときだけ、隣り合うジャンルを合わせて1通にする。合わせるのは、合わせて通数が
    減る組のうちカードの合計が一番少ない組(=小さいジャンル同士。同数なら後ろの組)。
    それでも超えるときはそのまま返す(呼び出し側が見出しを削る)。"""
    groups = [[b] for b in blocks if b]
    packed = [_pack_seq(g) for g in groups]
    while sum(len(p) for p in packed) > slots:
        best = None
        for i in range(len(groups) - 1):
            merged = _pack_seq(groups[i] + groups[i + 1])
            if len(merged) >= len(packed[i]) + len(packed[i + 1]):
                continue
            key = (sum(len(b) for b in groups[i] + groups[i + 1]), -i)
            if best is None or key < best[0]:
                best = (key, i, merged)
        if best is None:
            break
        _key, i, merged = best
        groups[i:i + 2] = [groups[i] + groups[i + 1]]
        packed[i:i + 2] = [merged]
    return [car for p in packed for car in p]


def _omit_note(n: int) -> Entry:
    return (_note_bubble(f"ほか {n} 件は省略"), 0, "", "")


def _genre_carousels(grouped: dict[str, list[NewsItem]], digest_date, slot, now: datetime,
                     slots: int) -> list[list[Entry]]:
    """ジャンルごとのカードを slots 通以内のカルーセルにする。
    入りきらないときは「その他の見出し」を末尾から削る(残りの多いジャンルから1件ずつ。どのジャンルも
    見出しが偏って消えないように)。削った件数は最後のカルーセル末尾に「ほか N 件は省略」と出す
    (黙って消さない)。見出しを全部削っても入らないときだけ、後ろのカードごと落とす。"""
    units = {g: _genre_units(items) for g, items in grouped.items() if items}
    totals = {g: len(grouped[g]) for g in units}
    remaining = {g: sum(1 for t, _ in us if t == "brief") for g, us in units.items()}
    order: list[str] = []   # 削る順(ジャンル名を1件ずつ)
    while any(remaining.values()):
        g = max(reversed(list(remaining)), key=lambda k: remaining[k])  # 同数なら後ろのジャンルから
        remaining[g] -= 1
        order.append(g)

    cache: dict[tuple[str, int], list[Entry]] = {}

    def build(k: int) -> list[list[Entry]]:
        cut = {g: order[:k].count(g) for g in units}
        blocks = []
        for g, us in units.items():
            key = (g, cut[g])
            if key not in cache:
                cache[key] = _genre_pages(g, us[:len(us) - cut[g]], totals[g], digest_date, slot, now)
            blocks.append(cache[key])
        return _pack_genres(blocks, slots)

    def ok(k: int) -> bool:
        cars = build(k)
        return len(cars) <= slots and (k == 0 or _fits(cars[-1], [_omit_note(k)]))

    if ok(0):
        return build(0)
    if ok(len(order)):
        lo, hi = 1, len(order)
        while lo < hi:
            mid = (lo + hi) // 2
            if ok(mid):
                hi = mid
            else:
                lo = mid + 1
        cars = build(hi)
        cars[-1].append(_omit_note(hi))
        return cars
    # 見出しを全部削っても入らない: 後ろのカードごと落として、落とした件数も数える
    cars = build(len(order))
    kept = cars[:slots]
    dropped = len(order) + sum(e[1] for car in cars[slots:] for e in car)
    last = kept[-1]
    while not _fits(last, [_omit_note(dropped)]):
        dropped += last.pop()[1]
    last.append(_omit_note(dropped))
    return kept


def digest_specs(
    grouped: dict[str, list[NewsItem]], greeting: bool = True, slot: str | None = None,
    digest_date: date | None = None, market: list[dict] | None = None,
    now: datetime | None = None, schedule: list[dict] | None = None,
    x_usage: dict | None = None, line_quota: dict | None = None, max_messages: int = MAX_MESSAGES,
) -> list[dict]:
    """購読ジャンルの NewsItem 群を配信メッセージ(spec列、最大 max_messages)に変換する。

    1通目は2枚のカルーセル: 要点(日付見出し・目次・今日の要点5本・残り使用量)と
    マーケット(市況・今日の予定・注目決算・決算サプライズ)。マーケットの中身が無ければ要点の1枚だけ。
    2通目以降はジャンルごとのカード(grouped の順=genres.toml の順)。1ジャンルのカードは横に連続し、
    中は 大きいニュース → 注目(score >= 50、要約つき)→ その他の見出し(見出しと出典)。
    - big が0件のジャンルは rank 最上位の1件を大きいニュースに上げる。
    - 原則1ジャンル=1通。通数が足りない日だけ隣り合う小さいジャンルを1通にまとめる(_pack_genres)。
    - 件数は黙って削らない: 入りきらない分は最後に「ほか N 件は省略」(_genre_carousels)。
    - 目次の「→ k通目」は実際に載ったメッセージの番号。
    - 0件のジャンルは出さない。全ジャンル0件ならテキスト1通。
    - greeting は互換のため残している(見出しは常に同じ)。
    - max_messages: 前に別のメッセージを足して送るとき(モック返信の警告文など)に減らす。
    """
    total = sum(len(items) for items in grouped.values())
    if not total:
        return [text_spec("本日は対象ジャンルのニュースが見つかりませんでした。")]

    now = now or datetime.now(ZoneInfo(DEFAULT_TZ))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(DEFAULT_TZ))

    cars = _genre_carousels(grouped, digest_date, slot, now, max_messages - 1)
    toc = []
    for g, items in grouped.items():
        if not items:
            continue
        # max_messages を減らす=前に別のメッセージが入るので、その分だけ通番をずらす
        nos = sorted({ci + 2 + MAX_MESSAGES - max_messages for ci, car in enumerate(cars) for e in car if e[2] == g})
        kept = sum(e[1] for car in cars for e in car if e[2] == g)
        toc.append((g, len(items), nos, len(items) - kept))

    d = digest_date or now.date()
    slot_word = "夜" if slot == "evening" else "朝"
    heading = f"{d.month}月{d.day}日({_WEEKDAYS[d.weekday()]}) {slot_word}のニュース"
    points = _pick_points(grouped)
    alt = f"{slot_word}のニュース｜{points[0].title}" + (f" ほか{total - 1}件" if total > 1 else "")
    market_card = _market_bubble(market, schedule, now)
    summary = _summary_bubble(toc, points, heading, total, digest_date, slot, market_card is not None,
                              x_usage=x_usage, line_quota=line_quota)
    if market_card is None:
        first = summary
    else:
        # 2枚で 48000B を超えるときはマーケットの方を切り詰める(要点と目次を優先)
        room = CAROUSEL_MAX_BYTES - _byte_size(_carousel([summary])) - 16
        first = _carousel([summary, _fit_component(market_card, min(room, BUBBLE_MAX_BYTES))])
        if _byte_size(first) > CAROUSEL_MAX_BYTES:  # 要点が極端に長い日。2枚で超えるならマーケットを外す
            first = summary

    specs: list[dict] = [{"type": "flex", "alt": alt[:ALT_MAX_CHARS], "contents": _guard_flex(first)}]
    for car in cars:
        genres = list(dict.fromkeys(e[2] for e in car if e[2]))
        first_title = next((e[3] for e in car if e[3]), "")
        alt_c = "・".join(f"{_genre_label(g)} {len(grouped[g])}件" for g in genres) + f"｜{first_title}"
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
