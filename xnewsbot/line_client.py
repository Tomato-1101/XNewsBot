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

from .feedback import title_hash
from .genres import ALWAYS_KEYS, GENRES, PRIVATE_KEYS, SELECTABLE_KEYS, empty_note
from .models import SLOT_LABEL, NewsItem, Subscriber

log = logging.getLogger(__name__)

# LINE の上限
QUICK_REPLY_MAX = 13
# Flex の上限(バブル 30KB)に余裕を持たせた値。
# 日本語はUTF-8で1文字3バイトのため、文字数でなくバイト数で測る。
# カルーセル(横スワイプ)は使わない。1通=縦に長い1枚のバブル(2026-11-11 本人「スマホでは縦スクロールでしか見ない」)。
# 1ジャンルが1枚に入りきらなければ記事単位で次の通に送る。
BUBBLE_MAX_BYTES = 28000
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
                "暗号資産": "#B45309", "ロボティクス": "#6D28D9", "競合": "#BE123C"}
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
POINTS_EXCLUDE = set(PRIVATE_KEYS)  # 今日の要点に入れないジャンル(genres.toml の private=true。競合など)
SCHEDULE_MAX = 12        # 今日の予定の最大行数
# 注目決算・決算サプライズの1国あたりの最大行数(マーケットのカードを PC で縦に長くしすぎないため)
EARNINGS_MAX_PER_COUNTRY = 8
# 日本の決算予定は時刻ごとに社名を「、」でつなぐ1行にするので社数が多くても縦に伸びにくい。超えた分は「ほか N 社」
JP_EARNINGS_NAMES_MAX = 150
SURPRISE_MAX_PER_COUNTRY = 5
_COUNTRY_LABELS = {"JP": "日本", "US": "米国"}
# 時刻が「21:30」「翌03:00」でない予定(昼ごろ・寄り前・引け後)の at は近似なので、過ぎても3時間は出す
SCHEDULE_APPROX_GRACE = timedelta(hours=3)
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


def _genre_mark(genre: str) -> str:
    """ジャンルの一番上の1件の左に付ける短い印(英字の名前はそのまま=AI、ほかは先頭1文字=仮・論・株)。"""
    label = _genre_label(genre)
    return label[:2] if label.isascii() else label[:1]


def _mark_badge(genre: str) -> dict:
    """ジャンル色の小さな四角に白い印。11-20 本人指示でジャンルの色帯ヘッダーの代わりに、大きいニュースの横にだけ付ける
    (全行に付けたら「キモい」と却下。付けるのはジャンルの一番上の1件だけ)。"""
    return {"type": "box", "layout": "vertical", "width": "20px", "height": "20px", "flex": 0,
            "backgroundColor": _genre_color(genre), "cornerRadius": "4px", "justifyContent": "center",
            "contents": [{"type": "text", "text": _genre_mark(genre), "size": "xxs", "weight": "bold",
                          "color": "#FFFFFF", "align": "center"}]}


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


RATE_LABEL = "評価する"


def _rate_key(item: NewsItem, digest_date: date | None, slot: str | None) -> str | None:
    """評価の記事キー `YYYYMMDD:slot:genre:<見出しハッシュ8桁>`(onboarding._resolve_feedback_item)。

    詳細の rank キーは同じ日に取り込み直すと同じ rank が別の記事を指すので、評価は見出しで引く。
    日付/スロットが無い文脈(モック表示)は架空の記事なので None(評価を出さない)。"""
    if digest_date is None or slot is None:
        return None
    return f"{digest_date:%Y%m%d}:{slot}:{item.genre}:{title_hash(item.title)}"


def _meta_row(item: NewsItem, action: dict, now: datetime, rate_key: str | None = None,
              **style) -> dict | None:
    """出典・時刻の行。rate_key があれば右端に控えめな「評価する」(押すと postback `rate:<記事キー>`、
    onboarding._handle_feedback)。

    行全体のタップ(詳細)とは別に、この文字だけ押せる。displayText は付けない(グループのトークに
    「評価する」と流さない)。rate_key が無ければ従来どおり出典・時刻の文字だけ(無ければ None)。
    style は出典・時刻の文字に足す属性(大きいニュースは wrap、注目は margin を行の側へ)。"""
    meta = _meta_text(item, now)
    if rate_key is None:
        return {"type": "text", "text": meta, "size": "xxs", "color": META_COLOR, **style} if meta else None
    margin = style.pop("margin", None)
    rate = {"type": "text", "text": RATE_LABEL, "size": "xxs", "color": META_COLOR, "flex": 0,
            "action": {"type": "postback", "data": f"rate:{rate_key}"}}
    if meta:
        # 横並びの text は既定で flex 1(出典・時刻が残りの幅を取り、「評価する」は右端に寄る)
        row = {"type": "box", "layout": "horizontal", "contents": [
            {"type": "text", "text": meta, "size": "xxs", "color": META_COLOR, **style}, rate]}
    else:  # 出典も時刻も無い記事は「評価する」だけを右端に
        row = {**rate, "flex": 1, "align": "end"}
    if margin:
        row["margin"] = margin
    return row


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
    2026-10-02 に特大を廃止して常時ジャンルは無くなったので、今は big の score 順 → small の score 順。
    非公開ジャンル(POINTS_EXCLUDE)は使わない(本人向けに広く拾った記事で、要点の枠を埋めないため)。"""
    pairs = [(gi, g, it) for gi, (g, items) in enumerate(grouped.items()) if g not in POINTS_EXCLUDE
             for it in items]

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


MARKET_HINT = "市況・今日の予定・決算はこの下 ↓"
MARKET_ALT = "マーケット・今日の予定・決算"  # マーケットを2通目に独立させた日の通の代替テキスト
DISCLAIMER = "評価は一般的な傾向で、投資助言ではありません"


def _section_title(text: str) -> list[dict]:
    """節の見出し(区切り線つき)。"""
    return [_sep("xl"), {"type": "text", "text": text, "size": "sm", "weight": "bold",
                         "color": TITLE_COLOR, "margin": "lg", "wrap": True}]


def _sub_title(text: str) -> dict:
    """節の中の小見出し(日本/米国)。"""
    return {"type": "text", "text": text, "size": "xs", "weight": "bold", "color": SUB_COLOR,
            "margin": "md"}


def _summary_bubble(points: list[NewsItem], heading: str, total: int, digest_date, slot,
                    has_market: bool, x_usage: dict | None = None,
                    line_quota: dict | None = None, empty_genres: list[str] | None = None) -> dict:
    """1通目の1枚目: 見出し → 今日の要点 → 残り使用量。目次は 11-19 に本人指示で廃止。
    empty_genres(今日は0件だったジャンル)のうち genres.toml の empty_note があるものは、末尾に1行ずつ出す。"""
    contents: list[dict] = [
        {"type": "text", "text": heading, "size": "lg", "weight": "bold", "color": TITLE_COLOR},
        {"type": "text", "text": f"計{total}件", "size": "xs", "color": SUB_COLOR},
    ]
    if has_market:
        contents.append({"type": "text", "text": MARKET_HINT, "size": "xxs", "color": META_COLOR,
                         "margin": "md", "wrap": True})

    contents += _section_title("今日の要点")
    contents += [_point_row(i, it, digest_date, slot) for i, it in enumerate(points, 1)]

    # 残り使用量は他の見出しと同じ書式の節にし、データが無くても必ず出す(どこにあるか迷わせない)
    contents += _section_title("残り使用量")
    contents.append(_x_usage_row(x_usage))
    contents.append(_line_quota_row(line_quota))

    for g in empty_genres or []:
        if empty_note(g):
            contents.append({"type": "text", "text": f"{_genre_label(g)} {empty_note(g)}", "size": "xxs",
                             "color": META_COLOR, "margin": "md", "wrap": True})

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


_JP_SHORT = (("ホールディングス", "HD"), ("フィナンシャルグループ", "FG"))
_JP_GROUPS = (("intraday", "場中（〜15:30・時刻は目安）"), ("after", "引け後・時刻未定"))


def _jp_short(ev: dict) -> str:
    name = re.sub(r"\s*(決算|説明会)$", "", str(ev.get("name") or ""))
    for long, short in _JP_SHORT:
        name = name.replace(long, short)
    return ("★" if ev.get("star") else "") + name


def _jp_earnings_rows(earn: list[dict], ir: list[dict]) -> list[dict]:
    """日本: 場中 → 引け後・未定 → 説明会 の小見出しごとに、時刻1つにつき1行(社名を「、」でつなぐ)。
    社名は JP_EARNINGS_NAMES_MAX 社まで(場中を優先)。超えた分は最後に「ほか N 社」。"""
    groups = [(cap, [ev for ev in earn if (ev.get("session") == "intraday") == (key == "intraday")])
              for key, cap in _JP_GROUPS] + [("説明会", ir)]
    out: list[dict] = []
    left, rest = JP_EARNINGS_NAMES_MAX, 0
    for cap, evs in groups:
        rest += max(0, len(evs) - left)
        evs = evs[:left]
        left -= len(evs)
        if not evs:
            continue
        by_label: dict[str, list[str]] = {}
        for ev in evs:
            by_label.setdefault(str(ev.get("time_label") or "未定"), []).append(_jp_short(ev))
        out.append({"type": "text", "text": cap, "size": "xxs", "color": META_COLOR, "margin": "sm"})
        out += [r for r in (_schedule_row({"time_label": lab, "name": "、".join(names)})
                            for lab, names in by_label.items()) if r]
    if rest:
        out.append(_rest_line(rest, "社"))
    return out


def _earnings_section(schedule: list[dict], now: datetime) -> list[dict]:
    """決算予定(kind="earnings")と日本の当日の説明会(kind="ir")。日本 → 米国。
    日本は場中・引け後に分けて時刻ごとにまとめる(★=株探の注目)。米国は1社1行で
    EARNINGS_MAX_PER_COUNTRY 行を超える分は「ほか N 社」にする(黙って消さない)。"""
    up = [ev for _at, ev in _upcoming([ev for ev in schedule if ev.get("kind") in ("earnings", "ir")], now)]
    if not up:
        return []
    out = _section_title("決算予定")
    for country, evs in _by_country(up):
        out.append(_sub_title(_country_label(country)))
        if country == "JP":
            out += _jp_earnings_rows([ev for ev in evs if ev.get("kind") == "earnings"],
                                     [ev for ev in evs if ev.get("kind") == "ir"])
            continue
        # 節の見出しが「決算予定」なので、行の名前の末尾の「決算」は省く
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
    """1通目の2枚目「マーケット」: 市況 → 今日の予定 → 決算予定 → 決算サプライズ → 注記。
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
    sched = _schedule_rows([ev for ev in schedule if ev.get("kind") not in ("earnings", "ir", "surprise")],
                           now)
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
TIER_HEADINGS = {"notable": "注目", "brief": "その他の見出し"}


CHEVRON_COLOR = "#BBBBBB"
BIG_BG = "#F5F6F8"


def _title_row(item: NewsItem, action: dict, now: datetime, rate_key: str | None,
               title_style: dict, badge: bool = False, **box) -> dict:
    """見出しだけの1件(11-19 本人指示: 要約は出さず、タップで詳細=解説を開く)。
    (大きいニュースだけ左端にジャンルの印)、見出し → 出典・時刻(右端に「評価する」)、右端に「›」。行全体のタップで詳細。"""
    # 見出しは2行まで(11-20 本人「2行が限界、3行は無し」)。プロンプトの幅36で収まらなかった分は LINE が「…」で切る
    left: list[dict] = [{"type": "text", "text": item.title, "wrap": True, "maxLines": 2, **title_style}]
    meta = _meta_row(item, action, now, rate_key)
    if meta:
        left.append(meta)
    return {"type": "box", "layout": "horizontal", "spacing": "md", "action": action, **box,
            "contents": [
                *([_mark_badge(item.genre)] if badge else []),
                {"type": "box", "layout": "vertical", "flex": 1, "contents": left},
                {"type": "text", "text": "›", "size": "lg", "color": CHEVRON_COLOR, "flex": 0,
                 "gravity": "center"},
            ]}


def _big_block(item: NewsItem, color: str, action: dict, now: datetime,
               rate_key: str | None = None, badge: bool = False) -> dict:
    """大きいニュース1件: 薄い灰色の札に大きめの太字の見出し。badge=ジャンルの先頭の1件だけ左にジャンルの印。"""
    row = _title_row(item, action, now, rate_key,
                     {"size": "md", "weight": "bold", "color": TITLE_COLOR}, badge=badge)
    row.pop("action")  # タップは札全体で受ける(同じ詳細の action を二重に持たない)
    return {"type": "box", "layout": "vertical", "backgroundColor": BIG_BG,
            "cornerRadius": "md", "paddingAll": "10px", "action": action, "contents": [row]}


def _notable_row(item: NewsItem, action: dict, now: datetime, rate_key: str | None = None) -> dict:
    """注目ニュースの1行: 太字の見出し。"""
    return _title_row(item, action, now, rate_key,
                      {"size": "sm", "weight": "bold", "color": TITLE_COLOR}, margin="sm")


def _brief_row(item: NewsItem, action: dict, now: datetime, rate_key: str | None = None) -> dict:
    """その他の見出しの1行: 細字の見出し。"""
    return _title_row(item, action, now, rate_key, {"size": "sm", "color": TEXT_COLOR}, margin="sm")


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
                body.append(_sep("lg"))
            if tier in TIER_HEADINGS:
                head = {"type": "text", "text": TIER_HEADINGS[tier], "size": "xs", "weight": "bold",
                        "color": color}
                if body:
                    head["margin"] = "md"
                body.append(head)
        elif tier == "big":  # 大きいニュースは札そのものが区切りなので線は引かず間を空ける
            comp = {**comp, "margin": "sm"}
        else:
            body.append(_sep("sm", "#F2F2F2"))
        body.append(comp)
        prev = tier
    return body


def _genre_bubble(genre: str, head: str, note: str, body: list[dict]) -> dict:
    """ジャンルのカード。11-20 本人指示で色帯ヘッダー(ジャンル名・件数)をやめ、ジャンルの一番上の1件の印で示す
    (head・note は呼び出し側の互換のため受け取るだけ)。ジャンルの境目は _stack の区切り線。"""
    return {"type": "bubble", "size": "giga",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "12px", "contents": body}}


# 「評価する」を付ける範囲(段)。重い日は上の段から順に落として、記事の省略を増やさない
RATE_NONE, RATE_TOP, RATE_ALL = 0, 1, 2   # なし / big と notable の行だけ / 全行


def _genre_pages(genre: str, units: list[tuple[str, NewsItem]], total: int, digest_date, slot,
                 now: datetime, rate: int = RATE_ALL, first_room: int | None = None) -> list[Entry]:
    """1ジャンルの (段, 記事) 列を、1通(28000B)に収まるカードに記事単位で分ける(縦に長くてよい)。
    ヘッダー右は「{total}件 k/n」(1枚なら「{total}件」)。total は省略前のジャンルの件数。
    単体で上限を超える記事は切り詰める(_fit_component)。rate は「評価する」を付ける段(RATE_*)。
    first_room: 1枚目を前のジャンルと同じ通の残り(バイト)に入れるときの空き。2枚目以降は1通分。"""
    label = _genre_label(genre)
    color = _genre_color(genre)

    def rk(it: NewsItem, level: int) -> str | None:
        return _rate_key(it, digest_date, slot) if rate >= level else None

    # ジャンルの印はジャンルの一番上の1件にだけ付ける(11-20 本人指示。全行・全 big に付けたら却下)
    builders = {"big": lambda it, a: _big_block(it, color, a, now, rk(it, RATE_TOP),
                                                badge=it is units[0][1]),
                "notable": lambda it, a: _notable_row(it, a, now, rk(it, RATE_TOP)),
                "brief": lambda it, a: _brief_row(it, a, now, rk(it, RATE_ALL))}
    if not units:
        return []

    def bubble(page, note):
        return _genre_bubble(genre, label, note, _page_body(genre, [(t, c) for t, c, _it in page]))

    base = _byte_size(_stack([bubble([], f"{total}件 00/00")]))
    # 小見出し・区切り線のぶん(200B)も残して、1件だけのカードが必ず上限に収まるようにする
    budget = BUBBLE_MAX_BYTES - base - 200
    comps = [(tier, _fit_component(builders[tier](it, _detail_action(it, digest_date, slot)), budget), it)
             for tier, it in units]
    # バイト数は部品ごとの大きさ + 区切り線(1件ごと)・小見出し(段ごと、多くて2つ)のぶんで見積もる(実測より大きめ)
    byte_room = BUBBLE_MAX_BYTES - base - 2 * 250
    room = byte_room if first_room is None else first_room - base - 2 * 250
    pages: list[list[tuple[str, dict, NewsItem]]] = []
    cur: list[tuple[str, dict, NewsItem]] = []
    nbytes = 0
    for unit in comps:
        size = _byte_size(unit[1]) + 120
        if cur and nbytes + size > (room if not pages else byte_room):
            pages.append(cur)
            cur, nbytes = [], 0
        cur.append(unit)
        nbytes += size
    if cur:
        pages.append(cur)
    n = len(pages)
    return [(bubble(page, f"{total}件" if n == 1 else f"{total}件 {k}/{n}"),
             len(page), genre, page[0][2].title)
            for k, page in enumerate(pages, 1)]


def _note_bubble(text: str) -> dict:
    return {"type": "bubble", "size": "giga",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                {"type": "text", "text": text, "size": "sm", "color": META_COLOR, "wrap": True}]}}


def _stack(cards: list[dict]) -> dict:
    """カード(ヘッダー/本文を持つバブル形の dict)を縦に積んで1枚のバブルにする。
    ジャンルのカードは色帯のヘッダーがそのまま区切りになる。ヘッダーの無いカード(要点・マーケット・注記)の
    前には区切り線を入れる。"""
    contents: list[dict] = []
    for card in cards:
        if contents and "header" not in card:
            contents.append({"type": "separator", "color": RULE_COLOR})
        contents.append({"type": "box", "layout": "vertical",
                         "contents": ([card["header"]] if "header" in card else []) + [card["body"]]})
    return {"type": "bubble", "size": "giga",
            "body": {"type": "box", "layout": "vertical", "paddingAll": "0px", "contents": contents}}


TOO_LARGE_NOTE = "長すぎるため表示できない記事がありました"


def _guard_flex(cards: list[dict]) -> dict:
    """カードを積んだ1枚(_stack)を返す。最後の安全網として、上限を超えたカードは注記に差し替え、
    それでも1枚が上限を超えるなら注記1枚にする。

    1通でも上限を超えると LINE は push 全体を 400 で拒否し、その回の全メッセージが届かない。
    通常は _fit_component の切り詰めと _fits の詰め方で収まりここでは何も変わらない。
    """
    cards = [c if _byte_size(c) <= BUBBLE_MAX_BYTES else _note_bubble(TOO_LARGE_NOTE) for c in cards]
    out = _stack(cards)
    return out if _byte_size(out) <= BUBBLE_MAX_BYTES else _note_bubble(TOO_LARGE_NOTE)


# (バブル, 載せた記事数, ジャンル, 先頭の記事の見出し)。省略注記は (注記, 0, "", "")
Entry = tuple[dict, int, str, str]


def _fits(car: list[Entry], extra: list[Entry]) -> bool:
    """car に extra を足して縦に積んでも1通(28000B)に収まるか。"""
    return _byte_size(_stack([e[0] for e in car + extra])) <= BUBBLE_MAX_BYTES


def _pack_seq(blocks: list[list[Entry]]) -> list[list[Entry]]:
    """カード列を順にメッセージに詰める(1つのメッセージにまとめると決めたジャンルの並び用)。
    ジャンルのカードが今のメッセージに丸ごと入るなら相乗りし、入らなければ次のメッセージから始める。
    1通に収まらない大きいジャンルは、今のメッセージの残りから順に詰めて次へ続ける
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
    """ジャンルごとのカード列をメッセージ(縦に積んだ1枚)に割り付ける。
    原則1ジャンル=1メッセージ(ジャンル単位で追える。2026-10-02 実画面で確認)。
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
                     slots: int, rate: int = RATE_ALL) -> list[list[Entry]]:
    """ジャンルごとのカードを slots 通以内のメッセージに割り付ける。
    入りきらないときは「その他の見出し」を末尾から削る(残りの多いジャンルから1件ずつ。どのジャンルも
    見出しが偏って消えないように)。削った件数は最後のメッセージ末尾に「ほか N 件は省略」と出す
    (黙って消さない)。見出しを全部削っても入らないときだけ、後ろのカードごと落とす。
    1ジャンル=1通の詰め方(_pack_genres)で通数が足りない日は、ジャンルを前の通の残りから
    記事単位で続けて詰める(flow)。見出しを削るのはそれでも入らないときだけ。"""
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
                cache[key] = _genre_pages(g, us[:len(us) - cut[g]], totals[g], digest_date, slot, now,
                                          rate)
            blocks.append(cache[key])
        cars = _pack_genres(blocks, slots)
        # 省略の注記まで入るときだけ1ジャンル1通を採る。注記が入らないのに採ると、二分探索が
        # 見出しをさらに削った側に進み、通数が余ったまま掲載が減る(件数が増えると載る件数が減る逆転)。
        k = sum(cut.values())
        if len(cars) <= slots and (k == 0 or _fits(cars[-1], [_omit_note(k)])):
            return cars
        return flow(cut)

    def flow(cut: dict[str, int]) -> list[list[Entry]]:
        msgs: list[list[Entry]] = []
        cur: list[Entry] = []
        for g, us in units.items():
            kept = us[:len(us) - cut[g]]
            room = BUBBLE_MAX_BYTES - _byte_size(_stack([e[0] for e in cur])) if cur else None
            pages = _genre_pages(g, kept, totals[g], digest_date, slot, now, rate, first_room=room)
            if cur and not _fits(cur, pages[:1]):  # 残りに1件も入らない: 次の通から始める
                msgs.append(cur)
                cur = []
                pages = _genre_pages(g, kept, totals[g], digest_date, slot, now, rate)
            for i, page in enumerate(pages):
                if i:
                    msgs.append(cur)
                    cur = []
                cur.append(page)
        if cur:
            msgs.append(cur)
        return msgs

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
    empty_genres: list[str] | None = None,
) -> list[dict]:
    """購読ジャンルの NewsItem 群を配信メッセージ(spec列、最大 max_messages)に変換する。

    どの通も縦に長い1枚のバブル(カルーセル=横スワイプは使わない)。
    1通目: 要点(日付見出し・今日の要点5本・残り使用量)の下にマーケット(市況・今日の予定・
    注目決算・決算サプライズ)。マーケットの中身が無ければ要点だけ。
    2通目以降はジャンルごとのカード(grouped の順=genres.toml の順)を縦に積む。
    カードの中は 大きいニュース → 注目(score >= 50)→ その他の見出し。どれも見出しと出典だけで、
    タップで詳細(解説)を開く。
    - big が0件のジャンルは rank 最上位の1件を大きいニュースに上げる。
    - 原則1ジャンル=1通。通数が足りない日だけ隣り合う小さいジャンルを1通にまとめる(_pack_genres)。
    - 件数は黙って削らない: 入りきらない分は最後に「ほか N 件は省略」(_genre_carousels)。
    - 0件のジャンルは出さない。全ジャンル0件ならテキスト1通。
    - greeting は互換のため残している(見出しは常に同じ)。
    - max_messages: 前に別のメッセージを足して送るとき(モック返信の警告文など)に減らす。
    - empty_genres: 今日のダイジェストはあるが記事0件だったジャンル。empty_note のあるものだけ1通目の末尾に1行出す。
    """
    total = sum(len(items) for items in grouped.values())
    if not total:
        return [text_spec("本日は対象ジャンルのニュースが見つかりませんでした。")]

    now = now or datetime.now(ZoneInfo(DEFAULT_TZ))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo(DEFAULT_TZ))

    def shown(cs: list[list[Entry]]) -> int:
        return sum(e[1] for car in cs for e in car)

    # 「評価する」は記事キーの分だけ重い。省略が出る日は「全行」→「big と notable の行だけ」→「なし」の順に
    # 組み直し、最も多く載る段のうち評価リンクが最も多い段を採る(評価のために記事の省略を増やさない)。
    # 日付/スロットが無い文脈(モック)は架空なので評価を出さない。
    rate = digest_date is not None and slot is not None

    def layout(slots: int) -> list[list[Entry]]:
        """ジャンルを slots 通に詰める。"""
        cars = _genre_carousels(grouped, digest_date, slot, now, slots, RATE_ALL if rate else RATE_NONE)
        if rate:
            for level in (RATE_TOP, RATE_NONE):
                if shown(cars) >= total:
                    break
                trial = _genre_carousels(grouped, digest_date, slot, now, slots, level)
                if shown(trial) > shown(cars):  # 同数なら評価リンクの多い上の段のまま
                    cars = trial
        return cars

    d = digest_date or now.date()
    slot_word = "夜" if slot == "evening" else "朝"
    heading = f"{d.month}月{d.day}日({_WEEKDAYS[d.weekday()]}) {slot_word}のニュース"
    points = _pick_points(grouped)
    alt = f"{slot_word}のニュース｜{points[0].title}" + (f" ほか{total - 1}件" if total > 1 else "")
    market_card = _market_bubble(market, schedule, now)

    summary = _summary_bubble(points, heading, total, digest_date, slot, market_card is not None,
                              x_usage=x_usage, line_quota=line_quota, empty_genres=empty_genres)
    cars = layout(max_messages - 1)
    firsts = [summary]
    if market_card is not None:
        stacked = _stack([summary, market_card])
        if _byte_size(stacked) <= BUBBLE_MAX_BYTES:
            firsts = [stacked]
        else:
            # 要点の下に入らない日(予定・決算が多い日)はマーケットを2通目に独立させる。
            # 黙って外すと市況・予定・決算が丸ごと消えるため、ジャンルの通を1通減らしてでも出す。
            cars = layout(max_messages - 2)
            mcard = market_card if _byte_size(_stack([market_card])) <= BUBBLE_MAX_BYTES else \
                _fit_component(market_card, BUBBLE_MAX_BYTES - _byte_size(_stack([{"body": {}}])))
            firsts = [summary, _stack([mcard])]

    specs: list[dict] = []
    for i, b in enumerate(firsts):
        specs.append({"type": "flex", "alt": (alt if i == 0 else MARKET_ALT)[:ALT_MAX_CHARS],
                      "contents": b if _byte_size(b) <= BUBBLE_MAX_BYTES else _note_bubble(TOO_LARGE_NOTE)})
    for car in cars:
        genres = list(dict.fromkeys(e[2] for e in car if e[2]))
        first_title = next((e[3] for e in car if e[3]), "")
        alt_c = "・".join(f"{_genre_label(g)} {len(grouped[g])}件" for g in genres) + f"｜{first_title}"
        specs.append({"type": "flex", "alt": alt_c[:ALT_MAX_CHARS],
                      "contents": _guard_flex([e[0] for e in car])})
    return specs


# LINE のテキストメッセージ上限は5000字。余裕を持たせて切る。
DETAIL_MAX_CHARS = 4800


def detail_spec(item: NewsItem, payload: str | None = None) -> dict:
    """「詳細を読む」タップで返す本文。見出しの再掲で終わらせず長め解説(detail。
    無ければ summary)を載せる。出典は本文を載せず、媒体名/@ハンドルと URL だけ残す。

    下に「AI解説」のクイックリプライを付ける。payload は詳細タップと同じ記事キー
    (無ければ id)で、押すと postback `explain:<payload>` が来る(onboarding._handle_explain)。
    続けて評価の「興味あり」「興味なし」(onboarding._handle_feedback)。"""
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
    qr = [_qr("AI解説", f"explain:{key}")]
    parts = key.split(":")
    if len(parts) == 4:  # 安定キー(日付:スロット:ジャンル:rank)のときだけ。id 直指定はモック・旧ボタン
        qr += feedback_quick_reply(":".join(parts[:3] + [title_hash(item.title)]))
    return text_spec(text, qr)


def feedback_quick_reply(key: str) -> list[dict]:
    """「興味あり」「興味なし」のクイックリプライ(postback `fb:<評価キー>:good|bad`。キーは _rate_key と同じ)。"""
    return [_qr("興味あり", f"fb:{key}:good"), _qr("興味なし", f"fb:{key}:bad")]


FEEDBACK_NOTE = "今後の配信の選び方に反映します"


def feedback_spec(title: str, key: str) -> dict:
    """「評価する」への返事: 見出しと「興味あり」「興味なし」のボタンの小さなバブル(reply なので無料)。

    クイックリプライは LINE PC に出ず、グループで誰かが発言すると消えるので、メッセージに残るボタンにする。
    displayText は付けない(押してもグループのトークに文字を流さない)。"""
    def button(label: str, verdict: str, style: str) -> dict:
        return {"type": "button", "style": style, "height": "sm", "flex": 1,
                "action": {"type": "postback", "label": label, "data": f"fb:{key}:{verdict}"}}

    bubble = {"type": "bubble", "size": "kilo",
              "body": {"type": "box", "layout": "vertical", "paddingAll": "16px", "contents": [
                  {"type": "text", "text": "この記事の評価", "size": "xxs", "color": META_COLOR},
                  {"type": "text", "text": title, "size": "sm", "weight": "bold", "color": TITLE_COLOR,
                   "wrap": True, "margin": "sm"},
                  {"type": "text", "text": FEEDBACK_NOTE, "size": "xxs", "color": META_COLOR,
                   "wrap": True, "margin": "md"},
                  {"type": "box", "layout": "horizontal", "spacing": "sm", "margin": "md", "contents": [
                      button("興味あり", "good", "primary"), button("興味なし", "bad", "secondary")]}]}}
    return {"type": "flex", "alt": f"この記事の評価: {title}"[:ALT_MAX_CHARS], "contents": bubble}


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

    def push(self, to: str, specs: list[dict], *, retry_key: str | None = None) -> None:
        from linebot.v3.messaging import PushMessageRequest
        if not specs:
            return
        messages = [_spec_to_message(s) for s in specs[:5]]
        kwargs = {"x_line_retry_key": retry_key} if retry_key else {}
        self._api().push_message(PushMessageRequest(to=to, messages=messages), **kwargs)
