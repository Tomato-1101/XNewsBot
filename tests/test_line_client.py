from __future__ import annotations

import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from xnewsbot import line_client as lc
from xnewsbot.models import NewsItem

JST = ZoneInfo("Asia/Tokyo")
D = date(2026, 6, 8)  # 月曜
NOW = datetime(2026, 6, 8, 8, 0, tzinfo=JST)
DETAIL_RE = re.compile(r"^detail:(\d+|None|\d{8}:(morning|evening):[^:]+:\d+)$")


def _big(genre: str = "AI", rank: int = 0, score: int = 80, title: str = "大きな出来事") -> NewsItem:
    return NewsItem(genre=genre, importance="big", rank=rank, title=title, score=score,
                    summary="重要な要約", source_urls=["https://x.com/u/status/1"],
                    source_tweets=[{"author": "u", "url": "https://x.com/u/status/1", "views": 9000}],
                    top_view_count=9000)


def _small(i: int, genre: str = "株", score: int = 40) -> NewsItem:
    return NewsItem(genre=genre, importance="small", rank=i, title=f"小さな話題{i}", score=score,
                    summary="小要約", source_urls=[f"https://x.com/u/status/{i}"],
                    source_tweets=[{"author": "v", "url": f"https://x.com/u/status/{i}", "views": 100}],
                    top_view_count=100)


def _texts(node) -> list[str]:
    return [n["text"] for n in lc._text_nodes(node)]


def _actions(node) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("action"), dict):
            found.append(node["action"])
        for v in node.values():
            found += _actions(v)
    elif isinstance(node, list):
        for v in node:
            found += _actions(v)
    return found


def _point_titles(spec: dict) -> list[str]:
    """要点バブルの要点行(数字+ジャンル+見出し)の見出しだけを順に取り出す。"""
    out = []
    for c in spec["contents"]["body"]["contents"]:
        if c.get("type") == "box" and c.get("action", {}).get("type") == "postback":
            out.append(c["contents"][1]["contents"][1]["text"])
    return out


def _bytes(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _carousels(specs: list[dict]) -> list[dict]:
    return [s["contents"] for s in specs if s["type"] == "flex"
            and s["contents"]["type"] == "carousel"]


def test_genre_select_marks_selected():
    spec = lc.genre_select_spec(["AI"])
    labels = [q["label"] for q in spec["quick_reply"]]
    assert any(l.startswith("✓") and "AI" in l for l in labels)
    assert any(l.startswith("＋") for l in labels)


# ---- 要点バブル ----

GUIDE_BRIEF = "次: 主なニュース → その他の見出し（どれもジャンルごとに横へスワイプ）"
GUIDE_ALL = "次: 主なニュース → 注目ニュース → その他の見出し（どれもジャンルごとに横へスワイプ）"
GUIDE_MAIN = "次: 主なニュース（ジャンルごとに横へスワイプ）"


def test_digest_specs_summary_then_main_then_others():
    # _small は score 40(< 50)なので、主に上がらなかった株の1件は「その他の見出し」に入る
    grouped = {"特大": [], "AI": [_big()], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert [s["type"] for s in specs] == ["flex", "flex", "flex"]
    summary = specs[0]["contents"]
    assert summary["type"] == "bubble" and summary["size"] == "giga"
    texts = _texts(summary)
    assert texts[0] == "6月8日(月) 朝のニュース"
    assert texts[1] == "AI 1・株 2（計3件）"  # 0件のジャンル(特大)は出さない
    assert "今日の要点" in texts
    assert texts[-1] == GUIDE_BRIEF  # 注目ニュースが無い日は案内にも出さない

    main = specs[1]["contents"]
    assert main["type"] == "carousel"
    heads = [_texts(b["header"]) for b in main["contents"]]
    assert heads == [["AI", "主なニュース"], ["株", "主なニュース"]]  # grouped の順、1ジャンル1枚
    assert main["contents"][0]["header"]["backgroundColor"] == "#4F46E5"
    assert main["contents"][1]["header"]["backgroundColor"] == "#0F766E"
    assert specs[1]["alt"] == "主なニュース（AI・株）"

    others = specs[2]["contents"]
    assert others["type"] == "carousel"
    assert [_texts(b["header"]) for b in others["contents"]] == [["株", "その他 1件"]]
    assert others["contents"][0]["header"]["backgroundColor"] == "#0F766E"
    assert others["contents"][0]["header"]["paddingAll"] == "12px"  # 主なニュースより低いヘッダー
    assert specs[2]["alt"] == "その他の見出し（株）"


def test_heading_same_without_greeting_and_evening_word():
    grouped = {"AI": [_big()]}
    a = lc.digest_specs(grouped, greeting=True, slot="evening", digest_date=D, now=NOW)
    b = lc.digest_specs(grouped, greeting=False, slot="evening", digest_date=D, now=NOW)
    assert _texts(a[0]["contents"])[0] == _texts(b[0]["contents"])[0] == "6月8日(月) 夜のニュース"
    assert a[0]["alt"].startswith("夜のニュース｜大きな出来事")


def test_points_order_always_first_then_score():
    grouped = {
        "特大": [_big("特大", 0, score=10, title="特大A")],          # score が低くても先頭
        "AI": [_big("AI", 0, score=70, title="AI大"), _small(1, "AI", score=90)],
        "株": [_big("株", 0, score=85, title="株大"), _small(1, "株", score=60)],
        "テクノロジー": [_big("テクノロジー", 0, score=70, title="テック大"),
                       _small(1, "テクノロジー", score=60)],
    }
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    # 特大 → big を score 降順(同点はジャンル順) → small を score 降順(同点はジャンル順)、最大5本
    assert _point_titles(specs[0]) == ["特大A", "株大", "AI大", "テック大", "小さな話題1"]
    rows = [c for c in specs[0]["contents"]["body"]["contents"]
            if c.get("type") == "box" and c.get("action")]
    assert rows[0]["contents"][0] == {"type": "text", "text": "1", "size": "sm", "weight": "bold",
                                      "color": "#D32F2F", "flex": 0}
    assert rows[1]["contents"][1]["contents"][0]["text"] == "株"
    # 5本目は AI の small(score 90)。同じ small の株(60)/テック(60)より上
    assert rows[4]["contents"][1]["contents"][0]["text"] == "AI"


def test_alt_text_first_point_and_count():
    grouped = {"AI": [_big(title="見出しA")], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert specs[0]["alt"] == "朝のニュース｜見出しA ほか2件"
    long = {"AI": [_big(title="長" * 1000)]}
    alt = lc.digest_specs(long, slot="morning", digest_date=D, now=NOW)[0]["alt"]
    assert len(alt) <= 400


def test_market_block_format_sign_unit_color():
    market = [
        {"key": "nikkei", "label": "日経平均", "close": 45210.35, "change": 540.1,
         "change_pct": 1.214, "asof": "2026-06-05", "kind": "index"},
        {"key": "sp500", "label": "S&P500", "close": 6512.4, "change": -34.7,
         "change_pct": -0.53, "asof": "2026-06-05", "kind": "index"},
        {"key": "usdjpy", "label": "ドル円", "close": 149.5, "change": 0.0,
         "change_pct": -0.001, "asof": "2026-06-05", "kind": "fx"},
        {"key": "us10y", "label": "米10年債", "close": 4.25, "change": 0.03,
         "change_pct": 0.71, "asof": "2026-06-05", "kind": "yield"},
    ]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, market=market, now=NOW)
    body = specs[0]["contents"]["body"]["contents"]
    assert "市況（前日終値）" in _texts(body)
    rows = [c for c in body if c.get("type") == "box" and not c.get("action")]
    got = [[(t["text"], t["color"]) for t in r["contents"]] for r in rows]
    assert got == [
        [("日経平均", "#555555"), ("45,210", "#222222"), ("+1.21%", "#C62828")],
        [("S&P500", "#555555"), ("6,512", "#222222"), ("-0.53%", "#1565C0")],
        [("ドル円", "#555555"), ("149.50円", "#222222"), ("+0.00%", "#888888")],  # -0.00 にしない
        [("米10年債", "#555555"), ("4.25%", "#222222"), ("+0.03pt", "#C62828")],
    ]
    blob = json.dumps(specs, ensure_ascii=False)
    assert "▲" not in blob and "▼" not in blob


def test_no_market_block_when_empty():
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, market=[], now=NOW)
    assert "市況（前日終値）" not in _texts(specs[0]["contents"])


CRYPTO_NOTE = "仮想通貨は直近値・24時間比"
ADVICE_NOTE = "評価は一般的な傾向で、投資助言ではありません"
_NIKKEI = {"key": "nikkei", "label": "日経平均", "close": 45210.35, "change": 540.1,
           "change_pct": 1.21, "asof": "2026-06-05", "kind": "index"}


def _section(spec: dict, heading: str) -> list[dict]:
    """要点バブルで見出し heading の後ろ、次の区切り線までの要素。"""
    body = spec["contents"]["body"]["contents"]
    start = next(i for i, c in enumerate(body) if c.get("text") == heading)
    out = []
    for c in body[start + 1:]:
        if c["type"] == "separator":
            break
        out.append(c)
    return out


def test_market_crypto_row_in_dollars_with_note():
    market = [_NIKKEI,
              {"key": "btc", "label": "ビットコイン", "close": 118234.7, "change": 2668.1,
               "change_pct": 2.31, "asof": "2026-06-08T07:50:00+09:00", "kind": "crypto"},
              {"key": "eth", "label": "イーサリアム", "close": 4321.8, "change": -52.4,
               "change_pct": -1.2, "asof": "2026-06-08T07:50:00+09:00", "kind": "crypto"}]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, market=market, now=NOW)
    sec = _section(specs[0], "市況（前日終値）")
    rows = [_texts(c) for c in sec if c["type"] == "box"]
    assert rows == [["日経平均", "45,210", "+1.21%"],
                    ["ビットコイン", "$118,235", "+2.31%"],
                    ["イーサリアム", "$4,322", "-1.20%"]]
    # 注記は市況ブロックの末尾(最後の行の直後)に1つだけ
    last_row = max(i for i, c in enumerate(sec) if c["type"] == "box")
    assert sec[last_row + 1]["text"] == CRYPTO_NOTE and sec[last_row + 1]["size"] == "xxs"
    assert _texts(specs[0]["contents"]).count(CRYPTO_NOTE) == 1
    # 仮想通貨が無ければ注記は出さない
    plain = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, market=[_NIKKEI], now=NOW)
    assert CRYPTO_NOTE not in _texts(plain[0]["contents"])


def _ev(at, label: str, name: str, **kw) -> dict:
    ev = {"at": at, "time_label": label, "kind": "indicator", "country": "US", "name": name,
          "forecast": "", "previous": "", "result": "", "importance": 3}
    ev.update(kw)
    return ev


def _schedule_lines(spec: dict) -> list[tuple[str, list[str]]]:
    """予定ブロックの各行を (時刻, [名前, 予想・前回]) に。"""
    return [(c["contents"][0]["text"], _texts(c["contents"][1]))
            for c in _section(spec, "今日の予定") if c["type"] == "box"]


def test_schedule_block_order_past_and_figures():
    schedule = [
        _ev(None, "未定", "時刻未定A"),
        _ev("2026-06-08T21:30:00+09:00", "21:30", "米 雇用統計", forecast="3.0%", previous="2.9%"),
        _ev("2026-06-08T07:59:00+09:00", "07:59", "過ぎた予定"),          # now より前は出さない
        _ev("2026-06-08T08:00:00+09:00", "08:00", "ちょうど今", previous="1.0%"),
        _ev(None, "寄り前", "時刻未定B", forecast="10円"),
        _ev("2026-06-09T03:00:00+09:00", "翌03:00", "FOMC"),
        _ev("2026-06-08T00:00:00+00:00", "09:00", "UTC表記"),            # = 09:00 JST
    ]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, schedule=schedule,
                            now=NOW)
    assert _schedule_lines(specs[0]) == [
        ("08:00", ["ちょうど今", "前回 1.0%"]),
        ("09:00", ["UTC表記"]),
        ("21:30", ["米 雇用統計", "予想 3.0%｜前回 2.9%"]),
        ("翌03:00", ["FOMC"]),
        ("未定", ["時刻未定A"]),          # at が無い予定は最後、元の順のまま
        ("寄り前", ["時刻未定B", "予想 10円"]),
    ]
    # 見出しは市況と同じ体裁で、前に区切り線
    body = specs[0]["contents"]["body"]["contents"]
    i = next(k for k, c in enumerate(body) if c.get("text") == "今日の予定")
    assert body[i - 1]["type"] == "separator"
    assert (body[i]["size"], body[i]["weight"]) == ("sm", "bold")


def test_schedule_block_max_12_and_none_when_empty():
    timed = [_ev(f"2026-06-08T{9 + k:02d}:00:00+09:00", f"{9 + k:02d}:00", f"予定{k:02d}")
             for k in range(13)]
    untimed = [_ev(None, "未定", f"未定{k}") for k in range(3)]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D,
                            schedule=untimed + timed, now=NOW)
    lines = _schedule_lines(specs[0])
    assert [names[0] for _t, names in lines] == [f"予定{k:02d}" for k in range(12)]
    # 0件・全部過ぎた予定ならブロックごと出さない
    for sched in ([], None, [_ev("2026-06-08T07:00:00+09:00", "07:00", "過去")]):
        s = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, schedule=sched, now=NOW)
        assert "今日の予定" not in _texts(s[0]["contents"])


def test_advice_note_only_with_market_or_schedule():
    future = [_ev("2026-06-08T21:30:00+09:00", "21:30", "米 雇用統計")]
    past = [_ev("2026-06-08T07:00:00+09:00", "07:00", "過去")]
    cases = [([_NIKKEI], None, True), ([], future, True), ([_NIKKEI], future, True),
             ([], None, False), (None, past, False)]
    for market, schedule, shown in cases:
        specs = lc.digest_specs({"AI": [_big()], "株": [_small(0)]}, slot="morning", digest_date=D,
                                market=market, schedule=schedule, now=NOW)
        texts = _texts(specs[0]["contents"])
        assert (ADVICE_NOTE in texts) is shown, (market, schedule)
        if shown:  # 市況・予定の後、残り使用量の前に極小で
            assert texts.index(ADVICE_NOTE) == texts.index("残り使用量") - 1
            note = next(c for c in specs[0]["contents"]["body"]["contents"]
                        if c.get("text") == ADVICE_NOTE)
            assert note["size"] == "xxs"


def test_new_genre_colors_use_genre_label():
    grouped = {"暗号資産": [_big("暗号資産")], "話題": [_big("話題")]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    heads = [b["header"] for b in specs[1]["contents"]["contents"]]
    assert [h["backgroundColor"] for h in heads] == ["#B45309", "#A21CAF"]
    assert [_texts(h)[0] for h in heads] == [lc._genre_label("暗号資産"), lc._genre_label("話題")]


def test_digest_specs_empty():
    specs = lc.digest_specs({"AI": []}, greeting=True)
    assert len(specs) == 1 and specs[0]["type"] == "text"


# ---- 主なニュース/ほかのニュースのカルーセル ----

def test_big_block_detail_and_source_links():
    # 大ニュースは見出し+要約+出典/時刻+[詳細を読む](postback)+[元記事](uri)
    item = _big()
    item.id = 42
    item.source_tweets = [
        {"author": "nikkei", "media": "日経", "kind": "news", "url": "https://a/1",
         "created_at": "2026-06-07T20:00:00+00:00"},          # NOW(=6/7 23:00 UTC)の3時間前
        {"author": "bb", "media": "Bloomberg", "kind": "news", "url": "https://a/2",
         "created_at": "2026-06-07T19:00:00+00:00"},
        {"author": "nikkei", "media": "日経", "kind": "news", "url": "https://a/3"},  # 重複は1つに
        {"author": "x1", "kind": "x", "url": "https://x.com/x1/status/1"},          # 旧データ: @author
        {"author": "x2", "kind": "x", "url": "https://x.com/x2/status/2"},
    ]
    specs = lc.digest_specs({"AI": [item]}, greeting=False, now=NOW)
    bubble = specs[1]["contents"]["contents"][0]
    texts = _texts(bubble["body"])
    assert texts[:3] == ["大きな出来事", "重要な要約", "日経・Bloomberg ほか2件・3時間前"]
    acts = _actions(bubble["body"])
    assert {"type": "postback", "data": "detail:42", "displayText": "詳細: 大きな出来事"} in acts
    assert {"type": "uri", "label": "元記事", "uri": "https://x.com/u/status/1"} in acts


def test_small_rows_listed_with_label_and_meta():
    big = _big("株", 0)
    s0, s1 = _small(1), _small(2)
    s0.source_tweets = [{"author": "v", "kind": "x", "url": "u",
                         "created_at": "2026-06-05T23:00:00+00:00"}]  # NOW の2日前
    specs = lc.digest_specs({"株": [big, s0, s1]}, slot="morning", digest_date=D, now=NOW)
    bubble = specs[2]["contents"]["contents"][0]
    assert _texts(bubble["header"]) == ["株", "その他 2件"]
    body = bubble["body"]["contents"]
    assert "@v・2日前" in _texts(body)
    smalls = [c for c in body if c.get("type") == "box" and c.get("action")]
    assert [c["action"]["data"] for c in smalls] == ["detail:20260608:morning:株:1",
                                                    "detail:20260608:morning:株:2"]
    # 主なニュース側には small を出さない
    assert "小さな話題1" not in _texts(specs[1]["contents"])
    # small 1件だけのジャンルはそれが主なニュースに上がり、ほかのニュースは出さない
    only_small = lc.digest_specs({"株": [_small(0)]}, slot="morning", digest_date=D, now=NOW)
    assert len(only_small) == 2
    assert "小さな話題0" in _texts(only_small[1]["contents"])


def test_notable_row_shows_summary_truncated_and_keeps_tap():
    """注目ニュース(score >= 50)は 見出し(太字) → 要約(100字超は「…」) → 出典・時刻 の順。要約が空なら出さない。"""
    s_short, s_long, s_exact, s_empty = (_small(i, score=60) for i in (1, 2, 3, 4))
    s_short.summary = "短い要約"
    s_long.summary = "あ" * 100 + "いう"
    s_exact.summary = "え" * 100
    s_empty.summary = ""
    specs = lc.digest_specs({"株": [_big("株", 0), s_short, s_long, s_exact, s_empty]},
                            slot="morning", digest_date=D, now=NOW)
    body = specs[2]["contents"]["contents"][0]["body"]["contents"]
    rows = [c for c in body if c.get("type") == "box" and c.get("action")]
    assert [_texts(r) for r in rows] == [
        ["小さな話題1", "短い要約", "@v"],
        ["小さな話題2", "あ" * 100 + "…", "@v"],
        ["小さな話題3", "え" * 100, "@v"],          # ちょうど100字は切らない
        ["小さな話題4", "@v"],                       # 要約が空なら行ごと出さない
    ]
    title, summary, meta = rows[0]["contents"]
    assert (title["size"], title["weight"], title["color"]) == ("sm", "bold", lc.TITLE_COLOR)
    assert (summary["size"], summary["color"], summary["wrap"]) == ("xs", "#555555", True)
    assert meta["size"] == "xxs"
    # 行のタップで詳細を開く動作は残す
    assert [r["action"]["data"] for r in rows] == [f"detail:20260608:morning:株:{i}" for i in (1, 2, 3, 4)]
    assert s_long.summary == "あ" * 100 + "いう"     # 元データは変えない


def test_big_block_has_detail_tap():
    # 一覧は見出し+要約までで、タップ(postback detail:)で長文詳細を開ける
    item = _big()
    item.id = 42
    blob = json.dumps(lc.digest_specs({"AI": [item]}, greeting=False), ensure_ascii=False)
    assert "detail:42" in blob


def test_digest_specs_uses_stable_detail_key_with_date():
    """digest_date を渡すと postback は安定キー(日付:slot:genre:rank)になる(id 直指定でない)。"""
    item = _big()
    item.id = 7
    item.rank = 0
    specs = lc.digest_specs({"AI": [item]}, greeting=False, slot="morning", digest_date=D)
    blob = json.dumps(specs, ensure_ascii=False)
    assert "detail:20260608:morning:AI:0" in blob
    assert '"detail:7"' not in blob  # id 直指定にフォールバックしていない


def _bulk(genres: list[str], n: int, summary_len: int = 60) -> dict[str, list[NewsItem]]:
    """各ジャンル n 件(先頭3件 big)。見出し40字・要約 summary_len 字・出典2件の実寸に近いデータ。"""
    out: dict[str, list[NewsItem]] = {}
    for g in genres:
        items = []
        for r in range(n):
            items.append(NewsItem(
                genre=g, importance="big" if r < 3 else "small", rank=r, score=90 - r,
                title=f"{g}の見出し{r:02d}" + "あ" * 30, summary="要約" * (summary_len // 2),
                source_urls=[f"https://example.com/{g}/{r}"],
                source_tweets=[
                    {"author": "媒体A", "media": "媒体A", "kind": "news",
                     "url": f"https://example.com/{g}/{r}", "created_at": "2026-06-07T21:00:00+00:00"},
                    {"author": "acc", "media": "@acc", "kind": "x",
                     "url": f"https://x.com/acc/status/{r}", "created_at": "2026-06-07T22:00:00+00:00"},
                ]))
        out[g] = items
    return out


def _check_limits_and_coverage(grouped, specs) -> None:
    assert len(specs) <= lc.MAX_MESSAGES
    assert specs[0]["contents"]["type"] == "bubble"
    assert _bytes(specs[0]["contents"]) <= lc.BUBBLE_MAX_BYTES
    for s in specs:
        assert len(s["alt"]) <= 400
        lc._spec_to_message(s)  # SDK が受け付ける形であること
    for car in _carousels(specs):
        assert len(car["contents"]) <= lc.CAROUSEL_MAX_BUBBLES
        assert _bytes(car) <= lc.CAROUSEL_MAX_BYTES
        for b in car["contents"]:
            assert b["size"] == "giga"
            assert _bytes(b) <= lc.BUBBLE_MAX_BYTES
    datas = [a["data"] for a in _actions(specs) if a["type"] == "postback"]
    assert datas and all(DETAIL_RE.match(d) for d in datas)


def test_bulk_4_genres_x20_fits_and_keeps_every_item():
    grouped = _bulk(["特大", "AI", "株", "テクノロジー"], 20)
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    # 取りこぼしなし: 全記事がカルーセルのどこかに(タップで詳細を引ける形で)出る
    car_datas = {a["data"] for car in _carousels(specs) for a in _actions(car)
                 if a["type"] == "postback"}
    for g, items in grouped.items():
        for it in items:
            assert f"detail:20260608:morning:{g}:{it.rank}" in car_datas
    assert "件は省略" not in json.dumps(specs, ensure_ascii=False)


def _heavy() -> dict[str, list[NewsItem]]:
    """重い日の実例: 特大3・AI35(big5+small30)・株25・暗号資産20・テクノロジー25・話題25。
    見出し40字前後・big の要約150字・small の要約100字・出典つき(big 3件・small 2件)。"""
    plan = [("特大", 3, 0), ("AI", 5, 30), ("株", 3, 22), ("暗号資産", 3, 17),
            ("テクノロジー", 3, 22), ("話題", 3, 22)]
    out: dict[str, list[NewsItem]] = {}
    for g, n_big, n_small in plan:
        items = []
        for r in range(n_big + n_small):
            big = r < n_big
            srcs = [{"author": "媒体A", "media": "サンプル経済新聞", "kind": "news",
                     "url": f"https://example.com/news/{r}/article-{r:04d}",
                     "created_at": "2026-06-07T21:00:00+00:00"},
                    {"author": "acc", "media": "@sample_acc", "kind": "x",
                     "url": f"https://x.com/sample_acc/status/19{r:017d}",
                     "created_at": "2026-06-07T22:00:00+00:00"}]
            if big:
                srcs.append({"author": "w", "media": "Sample Wire", "kind": "news",
                             "url": f"https://example.org/{r}", "created_at": "2026-06-07T20:00:00+00:00"})
            items.append(NewsItem(
                genre=g, importance="big" if big else "small", rank=r, score=90 - r,
                title=f"{g}見出し{r:02d}" + "あ" * (35 - len(g)),
                summary=("大要約" * 50) if big else ("要" * 100),
                source_urls=[s["url"] for s in srcs], source_tweets=srcs))
        out[g] = items
    return out


_HEAVY_MARKET = [
    {"key": k, "label": label, "close": close, "change": 1.0, "change_pct": 0.5,
     "asof": "2026-06-05", "kind": kind}
    for k, label, close, kind in [
        ("nikkei", "日経平均", 45210.35, "index"), ("sp500", "S&P500", 6512.4, "index"),
        ("nasdaq", "ナスダック総合", 21512.4, "index"), ("dow", "NYダウ", 45210.3, "index"),
        ("usdjpy", "ドル円", 149.5, "fx"), ("us10y", "米10年債", 4.25, "yield"),
        ("btc", "ビットコイン", 118234.5, "crypto"), ("eth", "イーサリアム", 4321.8, "crypto")]]
_HEAVY_SCHEDULE = [
    _ev(f"2026-06-08T{9 + k:02d}:30:00+09:00", f"{9 + k:02d}:30",
        f"米 経済指標その{k:02d}（前月比・季節調整済み）", forecast="0.3%", previous="0.2%")
    for k in range(12)]


def test_heavy_day_fits_5_messages_without_omission():
    """要約つきの小ニュースが大量の日でも、5メッセージ以内・省略なし・各上限内に収まる。"""
    grouped = _heavy()
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                            schedule=_HEAVY_SCHEDULE, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert "件は省略" not in json.dumps(specs, ensure_ascii=False)
    car_datas = {a["data"] for car in _carousels(specs) for a in _actions(car)
                 if a["type"] == "postback"}
    assert car_datas == {f"detail:20260608:morning:{g}:{it.rank}"
                         for g, items in grouped.items() for it in items}
    # 小ニュースの要約も切り詰められず全部出る
    texts = _texts(_carousels(specs))
    assert texts.count("要" * 100) == sum(1 for v in grouped.values() for it in v
                                          if it.importance != "big")
    # 要点バブル: 予定12行・市況8行を載せても上限内(切り詰め・差し替えなし)
    assert len(_schedule_lines(specs[0])) == 12
    assert len([c for c in _section(specs[0], "市況（前日終値）") if c["type"] == "box"]) == 8
    assert _bytes(specs[0]["contents"]) <= lc.BUBBLE_MAX_BYTES


def test_genre_kept_whole_when_greedy_packing_fits():
    """1ジャンルの件数が1ページ分(注目5件)以下なら、主/注目とも1ジャンル1枚。"""
    grouped = _bulk(["特大", "AI", "株", "テクノロジー"], 8)
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert len(specs) == 3
    for spec in specs[1:]:
        heads = [_texts(b["header"])[0] for b in spec["contents"]["contents"]]
        assert heads == ["特大", "AI", "株", "テクノロジー"]


def test_large_genre_splits_into_numbered_bubbles():
    # 1ジャンル40件・長い要約 → 28000B を超えるので「AI (1/N)」に分割され、件数は削らない
    grouped = _bulk(["AI"], 40, summary_len=400)
    for it in grouped["AI"]:
        it.importance = "big"
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    bubbles = [b for car in _carousels(specs) for b in car["contents"]]
    heads = [_texts(b["header"])[0] for b in bubbles]
    assert len(heads) > 1
    assert heads == [f"AI ({k}/{len(heads)})" for k in range(1, len(heads) + 1)]
    assert all(_texts(b["header"])[1] == "主なニュース" for b in bubbles)
    # 分割後のバブル先頭は区切り線で始めない
    assert all(b["body"]["contents"][0]["type"] != "separator" for b in bubbles)
    datas = {a["data"] for b in bubbles for a in _actions(b) if a["type"] == "postback"}
    assert datas == {f"detail:20260608:morning:AI:{r}" for r in range(40)}


def test_overflow_beyond_5_messages_shows_omitted_count():
    # 基本的に起きない量。入りきらない分は黙って消さず「ほか N 件は省略」を最後に出す
    grouped = _bulk(["特大", "AI", "株", "テクノロジー"], 150, summary_len=400)
    for items in grouped.values():
        for it in items:
            it.importance = "big"
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == lc.MAX_MESSAGES
    last_bubble = _carousels(specs)[-1]["contents"][-1]
    note = _texts(last_bubble)[0]
    m = re.fullmatch(r"ほか (\d+) 件は省略", note)
    assert m
    shown = {a["data"] for car in _carousels(specs) for a in _actions(car) if a["type"] == "postback"}
    total = sum(len(v) for v in grouped.values())
    assert len(shown) + int(m.group(1)) == total  # 表示件数 + 省略件数 = 全件


def test_oversized_single_item_is_truncated_not_dropped():
    """単体で上限を超える記事は切り詰めて必ず送れる形にする
    (分割しても収まらず LINE が 400 を返すと、その回の push が丸ごと落ちるため)。"""
    item = _big(title="見出し")
    item.summary = "長すぎる要約。" * 3000
    specs = lc.digest_specs({"AI": [item]}, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage({"AI": [item]}, specs)
    bubble = _carousels(specs)[0]["contents"][0]
    assert "見出し" in _texts(bubble)  # 見出しは残る(本文だけ削る)
    assert len(item.summary) == len("長すぎる要約。") * 3000  # 元データは壊さない


def _split_specs(specs) -> tuple[list[dict], list[dict]]:
    """2通目以降を alt で「主なニュース」とそれ以外(注目ニュース・その他の見出し)のカルーセルに分ける。"""
    main = [s["contents"] for s in specs[1:] if s["alt"].startswith("主なニュース（")]
    others = [s["contents"] for s in specs[1:]
              if s["alt"].startswith(("注目ニュース", "その他の見出し（"))]
    assert len(main) + len(others) == len(specs) - 1
    return main, others


def _bubble_datas(bubble) -> list[str]:
    return [a["data"] for a in _actions(bubble) if a["type"] == "postback"]


def test_main_and_others_are_separate_carousels_in_same_genre_order():
    """主なニュース(big)と注目ニュース(small・score >= 50)は別カルーセル。ジャンル順は両方とも grouped の順。"""
    grouped = _bulk(["特大", "AI", "株", "テクノロジー", "暗号資産", "話題"], 6)
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    main, others = _split_specs(specs)
    assert main and others
    # 主なニュースの後に注目ニュースが続く(混ざらない)
    alts = [s["alt"] for s in specs[1:]]
    assert alts == sorted(alts, key=lambda a: not a.startswith("主なニュース"))
    for b in (b for car in main for b in car["contents"]):
        assert _texts(b["header"])[1] == "主なニュース"
        big_ranks = {int(d.rsplit(":", 1)[1]) for d in _bubble_datas(b)}
        assert big_ranks <= {0, 1, 2}  # _bulk は先頭3件が big
    for b in (b for car in others for b in car["contents"]):
        assert re.fullmatch(r"注目 \d+件", _texts(b["header"])[1])  # _bulk の small は score 84〜87
        assert all(int(d.rsplit(":", 1)[1]) >= 3 for d in _bubble_datas(b))
    order = [lc._genre_label(g) for g in grouped]
    main_heads = [_texts(b["header"])[0] for car in main for b in car["contents"]]
    other_heads = [_texts(b["header"])[0] for car in others for b in car["contents"]]
    assert main_heads == order
    assert other_heads == order


def test_genre_without_big_promotes_top_item_to_main():
    """big が0件のジャンルは rank 最上位の1件を主なニュースへ(大きい見た目で)上げ、下の段からは除く。"""
    small_genre = [_small(2, "株"), _small(0, "株"), _small(1, "株")]  # 並びは rank 順でなくてよい
    grouped = {"AI": [_big()], "株": small_genre}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    main, others = _split_specs(specs)
    stock_main = main[0]["contents"][1]
    assert _texts(stock_main["header"]) == ["株", "主なニュース"]
    assert _bubble_datas(stock_main) == ["detail:20260608:morning:株:0"]
    # 大ニュースと同じ見た目(見出しが md・太字、「詳細を読む」リンク)
    title = next(n for n in lc._text_nodes(stock_main["body"]) if n["text"] == "小さな話題0")
    assert (title["size"], title.get("weight")) == ("md", "bold")
    assert "詳細を読む" in _texts(stock_main["body"])
    stock_others = others[0]["contents"][0]
    assert _texts(stock_others["header"]) == ["株", "その他 2件"]
    assert _bubble_datas(stock_others) == ["detail:20260608:morning:株:1",
                                           "detail:20260608:morning:株:2"]


def test_every_item_appears_exactly_once():
    """全記事が主・注目・その他のどれか1か所だけに出る(省略注記が無い量のとき)。"""
    grouped = {"特大": [_big("特大", 0)], "AI": [_big("AI", 0), _big("AI", 1), _small(2, "AI")],
               "株": [_small(0), _small(1)], "話題": [_small(0, "話題")]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    datas = [d for s in specs[1:] for d in _bubble_datas(s["contents"])]
    assert sorted(datas) == sorted(f"detail:20260608:morning:{g}:{it.rank}"
                                   for g, items in grouped.items() for it in items)
    assert "件は省略" not in json.dumps(specs, ensure_ascii=False)


def test_six_genres_x25_fits_5_messages_without_omission():
    # 注目とその他が半々の日(実データに近い比率)。全件が注目の日は要約ぶん重く、
    # 5通に収まらない分が「省略」になりうる(test_heavy_7_genres_x30_* で上限と件数の勘定を確かめる)
    grouped = _bulk(["特大", "AI", "株", "テクノロジー", "暗号資産", "話題"], 25, summary_len=100)
    for items in grouped.values():
        for it in items[3::2]:
            it.score = 40
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                            schedule=_HEAVY_SCHEDULE, now=NOW,
                            x_usage={"used": 9870, "remaining": 3_040_677},
                            line_quota={"limit": 200, "used": 45, "cost": 3})
    _check_limits_and_coverage(grouped, specs)
    main, others = _split_specs(specs)
    assert len(main) == 1 and others
    assert "件は省略" not in json.dumps(specs, ensure_ascii=False)
    datas = [d for car in main + others for d in _bubble_datas(car)]
    assert sorted(datas) == sorted(f"detail:20260608:morning:{g}:{it.rank}"
                                   for g, items in grouped.items() for it in items)


def test_others_overflow_note_in_last_others_carousel():
    """ほかのニュースが入りきらない量なら、最後(ほかのニュース)のカルーセル末尾に省略件数を出す。"""
    grouped = _bulk(["特大", "AI", "株", "テクノロジー"], 150, summary_len=400)
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == lc.MAX_MESSAGES
    main, others = _split_specs(specs)
    assert main and others
    m = re.fullmatch(r"ほか (\d+) 件は省略", _texts(others[-1]["contents"][-1])[0])
    assert m
    shown = {d for s in specs[1:] for d in _bubble_datas(s["contents"])}
    assert len(shown) + int(m.group(1)) == sum(len(v) for v in grouped.values())


# ---- 注目ニュース/その他の見出し(score で2段・ページ単位のバブル) ----

def _omitted(specs) -> int:
    blob = json.dumps(_carousels(specs), ensure_ascii=False)
    return sum(int(n) for n in re.findall(r"ほか (\d+) 件は省略", blob))


def _check_accounting(grouped, specs) -> list[str]:
    """カルーセルに出た記事(重複なし) + 省略件数 = 全件。出た記事の postback data を返す。"""
    datas = [d for car in _carousels(specs) for d in _bubble_datas(car)]
    assert len(datas) == len(set(datas))
    assert set(datas) <= {f"detail:20260608:morning:{g}:{it.rank}"
                          for g, items in grouped.items() for it in items}
    assert len(datas) + _omitted(specs) == sum(len(v) for v in grouped.values())
    return datas


def _heads(spec) -> list[list[str]]:
    return [_texts(b["header"]) for b in spec["contents"]["contents"] if "header" in b]


def test_tier_split_by_score_boundary_49_50():
    """主以外は score 50 以上が注目ニュース、49 以下がその他の見出し。"""
    grouped = {"AI": [_big(), _small(1, "AI", score=50), _small(2, "AI", score=49)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert [s["alt"] for s in specs[1:]] == ["主なニュース（AI）", "注目ニュース（AI）",
                                             "その他の見出し（AI）"]
    assert _bubble_datas(specs[2]["contents"]) == ["detail:20260608:morning:AI:1"]
    assert _bubble_datas(specs[3]["contents"]) == ["detail:20260608:morning:AI:2"]
    assert _heads(specs[2]) == [["AI", "注目 1件"]]
    assert _heads(specs[3]) == [["AI", "その他 1件"]]
    assert _texts(specs[0]["contents"])[-1] == GUIDE_ALL
    assert lc.NOTABLE_MIN_SCORE == 50


def test_notable_pages_of_5_with_page_headers():
    def notable_spec(n):
        items = [_big("AI")] + [_small(i, "AI", score=80) for i in range(1, n + 1)]
        specs = lc.digest_specs({"AI": items}, slot="morning", digest_date=D, now=NOW)
        assert len(specs) == 3 and specs[2]["alt"] == "注目ニュース（AI）"
        return specs[2]

    spec = notable_spec(12)
    assert _heads(spec) == [["AI", "注目 1/3"], ["AI", "注目 2/3"], ["AI", "注目 3/3"]]
    pages = [_bubble_datas(b) for b in spec["contents"]["contents"]]
    assert [len(p) for p in pages] == [5, 5, 2]
    assert [d for p in pages for d in p] == [f"detail:20260608:morning:AI:{i}" for i in range(1, 13)]
    assert _heads(notable_spec(4)) == [["AI", "注目 4件"]]
    assert _heads(notable_spec(5)) == [["AI", "注目 5件"]]
    assert _heads(notable_spec(6)) == [["AI", "注目 1/2"], ["AI", "注目 2/2"]]
    # 新しいページの先頭は区切り線で始めない
    assert all(b["body"]["contents"][0]["type"] != "separator"
               for b in notable_spec(12)["contents"]["contents"])


def test_brief_pages_of_8_title_and_meta_only():
    def brief_spec(n):
        items = [_big("株")] + [_small(i, "株", score=30) for i in range(1, n + 1)]
        specs = lc.digest_specs({"株": items}, slot="morning", digest_date=D, now=NOW)
        assert len(specs) == 3 and specs[2]["alt"] == "その他の見出し（株）"
        return specs[2]

    spec = brief_spec(17)
    assert _heads(spec) == [["株", "その他 1/3"], ["株", "その他 2/3"], ["株", "その他 3/3"]]
    assert [len(_bubble_datas(b)) for b in spec["contents"]["contents"]] == [8, 8, 1]
    assert _heads(brief_spec(6)) == [["株", "その他 6件"]]
    # 1行は 見出し + 出典・時刻 だけ(要約はタップ先の詳細で読む)
    rows = [c for c in spec["contents"]["contents"][0]["body"]["contents"] if c.get("action")]
    assert all(_texts(r) == [f"小さな話題{i}", "@v"] for i, r in enumerate(rows, 1))
    assert "小要約" not in _texts(spec)
    title, meta = rows[0]["contents"]
    assert (title["size"], title["color"], "weight" in title) == ("sm", lc.TEXT_COLOR, False)
    assert meta["size"] == "xxs" and not meta.get("wrap")  # 出典・時刻は1行


def test_tiers_follow_genre_order_and_never_mix_genres_in_a_bubble():
    order = ["AI", "株", "暗号資産", "話題"]
    grouped = {g: [_big(g)] + [_small(i, g, score=70 if i % 2 else 30) for i in range(1, 15)]
               for g in order}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert [s["alt"].split("（")[0] for s in specs[1:]] == ["主なニュース", "注目ニュース",
                                                          "その他の見出し"]
    labels = [lc._genre_label(g) for g in order]
    assert specs[2]["alt"] == "注目ニュース（" + "・".join(labels) + "）"
    for spec, word in ((specs[2], "注目"), (specs[3], "その他")):
        heads = _heads(spec)
        # ジャンル順 → ページ順(注目は7件=2ページ、その他は7件=1ページ)
        if word == "注目":
            assert heads == [[lb, f"注目 {k}/2"] for lb in labels for k in (1, 2)]
        else:
            assert heads == [[lb, "その他 7件"] for lb in labels]
        for b in spec["contents"]["contents"]:
            genre = order[labels.index(_texts(b["header"])[0])]
            assert all(d.split(":")[3] == genre for d in _bubble_datas(b))
    assert _check_accounting(grouped, specs) and _omitted(specs) == 0


def _heavy7(kind: str) -> dict[str, list[NewsItem]]:
    """7ジャンル×30件(先頭3件 big)・要約100字。kind で主以外の score を決める。"""
    genres = ["特大", "AI", "株", "暗号資産", "テクノロジー", "話題", "ビジネス"]
    grouped = _bulk(genres, 30, summary_len=100)
    for items in grouped.values():
        for k, it in enumerate(items[3:]):
            it.score = {"notable": 70, "brief": 30, "mixed": 70 if k % 2 else 30}[kind]
    return grouped


def test_heavy_7_genres_x30_within_limits_for_any_tier_mix():
    """注目が多い日・その他が多い日・半々の日のどれでも、5通・12枚・48000B・28000B を超えず、
    全記事がどこかに出るか省略件数に数えられる。段の並びは 主 → 注目 → その他。"""
    for kind in ("notable", "brief", "mixed"):
        grouped = _heavy7(kind)
        specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                                schedule=_HEAVY_SCHEDULE, now=NOW,
                                x_usage={"used": 9870, "remaining": 3_040_677},
                                line_quota={"limit": 200, "used": 45, "cost": 3})
        _check_limits_and_coverage(grouped, specs)
        _check_accounting(grouped, specs)
        alts = [s["alt"] for s in specs[1:]]
        assert alts[0].startswith("主なニュース（")
        if kind == "notable":
            assert all(a.startswith("注目ニュース（") for a in alts[1:])
        if kind == "brief":
            assert all(a.startswith("その他の見出し（") for a in alts[1:])
            assert _omitted(specs) == 0  # 見出しだけなら 189件でも収まる
        # 注目が多い日・半々の日は要約ぶん重く、5通に入りきらない分は省略件数に数える
        # (上の _check_accounting で「出た件数 + 省略件数 = 全件」を確認済み)


def _big_main(genres: list[str], n_big: int) -> dict[str, list[NewsItem]]:
    """主なニュースが重い(長い要約の big が多い)データ。"""
    out = {}
    for g in genres:
        out[g] = [_big(g, r, score=95, title=f"{g}大{r}" + "あ" * 30) for r in range(n_big)]
        for it in out[g]:
            it.summary = "大" * 400
    return out


def test_main_needs_two_messages_tiers_get_one_each():
    """主なニュースが2通要る日は、注目・その他に1通ずつ(合計5)。入らない分は各段の末尾で省略件数を出す。"""
    genres = ["AI", "株", "暗号資産", "テクノロジー", "話題", "ビジネス", "健康"]
    grouped = _big_main(genres, 4)  # 1ジャンル約9KB × 7 = 1通(48000B)に入らず2通
    for g in genres:
        grouped[g] += [_small(r, g, score=70) for r in range(4, 34)]
        grouped[g] += [_small(r, g, score=30) for r in range(34, 64)]
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == lc.MAX_MESSAGES
    assert [s["alt"].split("（")[0] for s in specs[1:]] == ["主なニュース", "主なニュース",
                                                          "注目ニュース", "その他の見出し"]
    for spec in specs[3:]:
        assert re.fullmatch(r"ほか \d+ 件は省略", _texts(spec["contents"]["contents"][-1])[0])
    # 主なニュースは省略しない(主 → 注目 → その他の優先度)
    main_datas = {d for s in specs[1:3] for d in _bubble_datas(s["contents"])}
    assert main_datas == {f"detail:20260608:morning:{g}:{r}" for g in genres for r in range(4)}
    _check_accounting(grouped, specs)


def test_overflow_continuation_packs_notable_then_brief_in_one_message():
    """注目もその他も1通に収まらない日は、各段の1通目の後に「続き」を1通。続きは段を混ぜて
    注目 → その他の並び順で詰める(注目の続きを優先して1通使い切らない)。"""
    genres = ["AI", "株", "暗号資産", "テクノロジー", "話題"]
    grouped = {}
    for g in genres:
        grouped[g] = [_big(g, 0)]
        grouped[g] += [_small(r, g, score=70) for r in range(1, 11)]     # 注目10件=2ページ
        grouped[g] += [_small(r, g, score=30) for r in range(11, 29)]    # その他18件=3ページ
        for it in grouped[g][1:]:
            it.summary = "要" * 100
            it.title = f"{g}の話題{it.rank:02d}" + "あ" * 30
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == lc.MAX_MESSAGES
    kinds = [s["alt"].split("（")[0] for s in specs[1:]]
    assert kinds == ["主なニュース", "注目ニュース", "その他の見出し", "注目ニュース・その他の見出し"]
    notes = [h[1] for h in _heads(specs[4])]
    # 続きの1通: 注目の続き → その他の続き(段の境目は1回だけ)
    first_brief = next(i for i, n in enumerate(notes) if n.startswith("その他"))
    assert notes[:first_brief] and all(n.startswith("注目") for n in notes[:first_brief])
    assert all(n.startswith("その他") for n in notes[first_brief:])
    # 注目の1通目 + 続きの注目部分を並べると、注目の全記事がジャンル順 → rank 順に1回ずつ並ぶ
    cont = specs[4]["contents"]["contents"][:first_brief]
    notable_seq = _bubble_datas(specs[2]["contents"]) + [d for b in cont for d in _bubble_datas(b)]
    assert notable_seq == [f"detail:20260608:morning:{g}:{r}" for g in genres for r in range(1, 11)]
    assert _omitted(specs) == 0
    _check_accounting(grouped, specs)


# ---- 詳細 ----

def test_detail_spec_shows_sources_without_post_body():
    item = _big()
    item.detail = "これは長い詳細解説の本文です。"
    item.source_tweets = [{"text": "ツイート本文は載せない", "author": "u",
                           "url": "https://x.com/u/status/1", "views": 9000}]
    text = lc.detail_spec(item)["text"]
    assert text.startswith("【AI】大きな出来事")
    assert "これは長い詳細解説の本文です。" in text   # detail は出す
    assert "ツイート本文は載せない" not in text       # 出典の本文は出さない
    assert "\n出典\n・@u https://x.com/u/status/1" in text
    assert "元ポスト" not in text


def test_detail_spec_media_names_max3_and_tags():
    item = _big()
    item.genres = ["AI", "テクノロジー"]
    item.source_tweets = [
        {"author": "日経", "media": "日経", "kind": "news", "url": f"https://n/{i}"} for i in range(5)
    ]
    text = lc.detail_spec(item)["text"]
    assert text.startswith("【AI/テクノロジー】大きな出来事")
    assert text.count("・日経 https://n/") == 3


def test_detail_spec_contains_title_and_source():
    spec = lc.detail_spec(_big())
    assert "大きな出来事" in spec["text"]
    assert "https://x.com/u/status/1" in spec["text"]


def test_spec_to_sdk_message_text_and_flex():
    """line-bot-sdk v3 への変換が壊れていないか(API名の検証)。"""
    text = lc.text_spec("こんにちは", [{"label": "AI", "data": "genre:AI"}])
    msg = lc._spec_to_message(text)
    assert msg.text == "こんにちは"
    assert msg.quick_reply is not None

    specs = lc.digest_specs({"AI": [_big()]}, greeting=False, slot="morning", digest_date=D, now=NOW)
    fmsg = lc._spec_to_message(specs[0])
    assert fmsg.alt_text == "朝のニュース｜大きな出来事"
    cmsg = lc._spec_to_message(specs[1])
    assert cmsg.alt_text == "主なニュース（AI）"


# ---- 不正な元記事 URL・上限超過で push 全体が落ちないこと ----

_BASE = "https://example.com/"  # 20字


def test_safe_uri_accepts_only_plain_http_urls():
    ok_1000 = _BASE + "a" * (lc.URI_MAX_CHARS - len(_BASE))
    assert lc._safe_uri("https://example.com/a?b=1#c") == "https://example.com/a?b=1#c"
    assert lc._safe_uri("http://example.com/") == "http://example.com/"
    assert lc._safe_uri(ok_1000) == ok_1000                      # ちょうど1000字は可
    for bad in [None, "", ok_1000 + "a", "javascript:alert(1)", "line://nv/chat", "tel:0123",
                "ftp://example.com/a", "//example.com/a", "http://", "https:///path",
                "https://example.com/a b", "https://example.com/a\tb", "https://example.com/\n",
                "https://example.com/　", "https://example.com/\x00", "https://example.com/​",
                "http://[::1/"]:
        assert lc._safe_uri(bad) is None, bad


def test_safe_uri_encodes_chars_line_rejects():
    """日本語・| [] を含む URL は LINE が push ごと拒否するのでエンコードして通す(validate API で確認済み)。"""
    assert (lc._safe_uri("https://example.jp/ニュース?q=株")
            == "https://example.jp/%E3%83%8B%E3%83%A5%E3%83%BC%E3%82%B9?q=%E6%A0%AA")
    assert lc._safe_uri("https://example.com/a?x=1|2&y=[3]") == "https://example.com/a?x=1%7C2&y=%5B3%5D"
    assert lc._safe_uri("https://example.com/%E3%83%8B") == "https://example.com/%E3%83%8B"  # 二重にしない
    assert lc._safe_uri("https://example.com/100%off") is None   # 壊れた % は直せない
    assert lc._safe_uri("https://日本語.jp/a") is None             # ASCII でないホスト名
    long_ja = _BASE + "あ" * 200                                   # エンコード後に1000字を超える
    assert len(long_ja) <= lc.URI_MAX_CHARS and lc._safe_uri(long_ja) is None


def test_safe_uri_keeps_host_and_checks_authority():
    """ホスト部はエンコードしない。IPv6 リテラル(LINE が拒否)・ホスト欠落・不正ポート・userinfo は弾く。"""
    assert lc._safe_uri("https://example.com:8443/ニ") == "https://example.com:8443/%E3%83%8B"
    assert lc._safe_uri("https://example.com/a#節") == "https://example.com/a#%E7%AF%80"
    for bad in ["https://:443/a", "https://example.com:bad/a", "https://example.com:99999/",
                "https://user:pw@example.com/", "https://exa_mple.com/", "http://[zz]/a",
                "http://[2606:4700::1111]/a",
                "https://K.com/article",     # K(ケルビン記号): hostname の小文字化で k.com に化ける
                "https://example.Kom:8443/"]:
        assert lc._safe_uri(bad) is None, bad


def test_bad_source_urls_drop_only_the_link():
    """1023字・35000字・javascript:・空白入りの URL でも、uri は全部 http(s) かつ1000字以下になり、
    記事本体は出て「元記事」リンクだけが省かれる。先頭が不正でも後ろに正しい URL があればそれを使う。"""
    url_sets = [
        [_BASE + "a" * (1023 - len(_BASE))],
        [_BASE + "b" * (35000 - len(_BASE))],
        ["javascript:alert(document.cookie)"],
        ["https://example.com/a b"],
        [" https://example.com/lead-space"],
        [_BASE + "c" * 2000, "https://example.com/fallback"],
        [],
    ]
    items = []
    for r, urls in enumerate(url_sets):
        it = _big(rank=r, score=90 - r, title=f"記事{r}")
        it.source_urls = urls
        it.source_tweets = [{"media": "媒体", "kind": "news", "url": u} for u in urls]
        items.append(it)
    grouped = {"AI": items}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)

    uris = [a["uri"] for a in _actions(specs) if a["type"] == "uri"]
    assert uris == ["https://example.com/fallback"]
    texts = _texts(_carousels(specs))
    assert all(f"記事{r}" in texts for r in range(len(url_sets)))  # 記事本体は全部出る
    assert texts.count("元記事") == 1


def test_guard_replaces_only_oversized_bubble():
    small = lc._note_bubble("ふつうのバブル")
    huge = lc._note_bubble("あ" * 20000)                         # 60000B 超
    out = lc._guard_flex(lc._carousel([small, huge]))
    assert out["contents"][0] == small
    assert _texts(out["contents"][1]) == [lc.TOO_LARGE_NOTE]
    assert _bytes(out) <= lc.CAROUSEL_MAX_BYTES
    assert lc._guard_flex(small) == small
    assert _texts(lc._guard_flex(huge)) == [lc.TOO_LARGE_NOTE]


def test_untrimmable_summary_does_not_break_push():
    """切り詰めきれない異常出力(要点5本の見出しが各5万字)でも、その1通だけ注記にして全メッセージを
    上限内に収める(1通でも超えると LINE は push 全体を 400 で拒否するため)。"""
    items = [_big(rank=r, score=90 - r, title=chr(0x3042 + r) * 50000) for r in range(5)]
    grouped = {"AI": items}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert _texts(specs[0]["contents"]) == [lc.TOO_LARGE_NOTE]
    datas = {a["data"] for a in _actions(_carousels(specs)) if a["type"] == "postback"}
    assert datas == {f"detail:20260608:morning:AI:{r}" for r in range(5)}  # 記事カードは届く


def test_schedule_block_keeps_important_events_over_many_early_ones():
    """早い時刻の重要度3が12件あっても、翌03:00 の重要度5(FOMC)は表示に残る。"""
    early = [_ev(f"2026-06-08T{9 + k:02d}:00:00+09:00", f"{9 + k:02d}:00", f"決算{k:02d}")
             for k in range(12)]
    fomc = _ev("2026-06-09T03:00:00+09:00", "翌03:00", "FOMC 政策金利発表", importance=5)
    names = [n[0] for _t, n in _schedule_lines(lc.digest_specs(
        {"AI": [_big()]}, slot="morning", digest_date=D, schedule=early + [fomc], now=NOW)[0])]
    assert len(names) == 12 and names[-1] == "FOMC 政策金利発表" and "決算11" not in names


def test_schedule_block_keeps_approx_time_for_grace_period():
    """「昼ごろ」の予定は近似時刻(at)を過ぎても3時間は出す。確定時刻の予定は過ぎたら出さない。"""
    sched = [_ev("2026-06-08T06:00:00+09:00", "昼ごろ", "日銀 結果発表"),
             _ev("2026-06-08T06:00:00+09:00", "06:00", "過去の指標")]
    names = [n[0] for _t, n in _schedule_lines(lc.digest_specs(
        {"AI": [_big()]}, slot="morning", digest_date=D, schedule=sched, now=NOW)[0])]
    assert names == ["日銀 結果発表"]


# --- 残り使用量の節(要点バブル末尾。X と LINE の2行) ---

X_LABEL = "X（ニュース取得）  "
LINE_LABEL = "LINE（配信）  "


def _usage_node(x_usage):
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, now=NOW, x_usage=x_usage)
    nodes = [n for n in lc._text_nodes(specs[0]["contents"]) if n["text"].startswith(X_LABEL)]
    return specs, nodes


def test_x_usage_line_normal():
    specs, nodes = _usage_node({"used": 9870, "remaining": 3_040_677})
    assert [n["text"] for n in nodes] == [
        X_LABEL + "残り 3,040,677クレジット（約$30.41）・あと約308日／今回 9,870"]
    assert nodes[0]["size"] == "xs" and nodes[0]["color"] == lc.SUB_COLOR
    assert all(s["type"] == "flex" for s in specs)


def test_x_usage_line_warns_under_7_days():
    _specs, nodes = _usage_node({"used": 10_000, "remaining": 69_999})  # 6.99 日分 → あと約6日
    assert [n["text"] for n in nodes] == [
        X_LABEL + "要チャージ: 残り 69,999クレジット（約$0.70）・あと約6日／今回 10,000"]
    assert nodes[0]["color"] == lc.UP_COLOR
    _specs, nodes = _usage_node({"used": 10_000, "remaining": 70_000})  # ちょうど7日分は警告しない
    assert "要チャージ" not in nodes[0]["text"] and nodes[0]["color"] == lc.SUB_COLOR


def test_x_usage_line_shown_as_failed_when_none():
    """データが無くても行は出す(どこにあるか迷わせない)。"""
    for bad in (None, {}, {"used": "x", "remaining": 1}):
        _specs, nodes = _usage_node(bad)
        assert [n["text"] for n in nodes] == [X_LABEL + "取得できませんでした"]
        assert nodes[0]["color"] == lc.SUB_COLOR


def test_x_usage_line_used_zero_omits_days():
    _specs, nodes = _usage_node({"used": 0, "remaining": 3_040_677})
    assert [n["text"] for n in nodes] == [X_LABEL + "残り 3,040,677クレジット（約$30.41）／今回 0"]
    assert nodes[0]["color"] == lc.SUB_COLOR


def test_x_usage_line_no_empty_text_nodes():
    specs, _ = _usage_node({"used": 9870, "remaining": 3_040_677})
    assert all(t for t in _texts(specs[0]["contents"]))


def _quota_specs(line_quota, x_usage=None):
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, now=NOW,
                            x_usage=x_usage, line_quota=line_quota)
    nodes = [n for n in lc._text_nodes(specs[0]["contents"]) if n["text"].startswith(LINE_LABEL)]
    return specs, nodes


def test_line_quota_line_normal():
    _specs, nodes = _quota_specs({"limit": 200, "used": 45, "cost": 3})
    assert [n["text"] for n in nodes] == [LINE_LABEL + "今月 残り 152/200通・あと約50回／今回 3通"]
    assert nodes[0]["size"] == "xs" and nodes[0]["color"] == lc.SUB_COLOR


def test_line_quota_line_shown_as_failed_when_none():
    for bad in (None, {"limit": 200, "used": 1}, {"limit": 200, "used": 1, "cost": 0}):
        _specs, nodes = _quota_specs(bad)
        assert [n["text"] for n in nodes] == [LINE_LABEL + "取得できませんでした"]
        assert nodes[0]["color"] == lc.SUB_COLOR


def test_line_quota_line_warns_under_7_runs():
    # 残り 20 / cost 3 → あと約6回(7回未満)
    _specs, nodes = _quota_specs({"limit": 200, "used": 177, "cost": 3})
    assert [n["text"] for n in nodes] == [LINE_LABEL + "要注意: 今月 残り 20/200通・あと約6回／今回 3通"]
    assert nodes[0]["color"] == lc.UP_COLOR
    # 残り 21 / cost 3 → ちょうど7回は警告しない
    _specs, nodes = _quota_specs({"limit": 200, "used": 176, "cost": 3})
    assert "要注意" not in nodes[0]["text"] and nodes[0]["color"] == lc.SUB_COLOR


def test_line_quota_line_remaining_never_negative():
    _specs, nodes = _quota_specs({"limit": 200, "used": 199, "cost": 3})
    assert [n["text"] for n in nodes] == [LINE_LABEL + "要注意: 今月 残り 0/200通・あと約0回／今回 3通"]


def test_usage_section_heading_then_x_then_line_even_without_data():
    """区切り線 → 見出し「残り使用量」(市況と同じ書式) → X → LINE の順。データが None でも出る。"""
    for x_usage, line_quota in ((None, None),
                                ({"used": 9870, "remaining": 3_040_677},
                                 {"limit": 200, "used": 45, "cost": 3})):
        specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, now=NOW,
                                market=[_NIKKEI], x_usage=x_usage, line_quota=line_quota)
        body = specs[0]["contents"]["body"]["contents"]
        i = next(k for k, c in enumerate(body) if c.get("text") == "残り使用量")
        assert body[i - 1]["type"] == "separator"
        head = body[i]
        market_head = next(c for c in body if c.get("text") == "市況（前日終値）")
        assert {k: v for k, v in head.items() if k != "text"} == \
            {k: v for k, v in market_head.items() if k != "text"}
        assert body[i + 1]["text"].startswith(X_LABEL)
        assert body[i + 2]["text"].startswith(LINE_LABEL)
        assert body[i + 3]["text"] == GUIDE_MAIN  # 末尾は構造が分かる案内


# --- LineMessenger.fetch_quota (SDK を差し替え。実 API は叩かない) ---

class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeApi:
    def __init__(self, qtype="limited", value=200, usage=45, members=3, fail=None):
        self.qtype, self.value, self.usage, self.members, self.fail = qtype, value, usage, members, fail
        self.calls: list[tuple] = []
        self.timeouts: list = []

    def _go(self, name, *a):
        self.calls.append((name, *a))
        if self.fail == name:
            raise RuntimeError("boom")

    def get_message_quota(self, **kw):
        self.timeouts.append(kw.get("_request_timeout"))
        self._go("quota")
        return _Obj(type=_Obj(value=self.qtype), value=self.value)

    def get_message_quota_consumption(self, **kw):
        self.timeouts.append(kw.get("_request_timeout"))
        self._go("consumption")
        return _Obj(total_usage=self.usage)

    def get_group_member_count(self, gid, **kw):
        self.timeouts.append(kw.get("_request_timeout"))
        self._go("group", gid)
        return _Obj(count=self.members)

    def get_room_member_count(self, rid, **kw):
        self.timeouts.append(kw.get("_request_timeout"))
        self._go("room", rid)
        return _Obj(count=self.members)


def _messenger_with(monkeypatch, api):
    m = lc.LineMessenger("dummy-token")
    monkeypatch.setattr(m, "_api", lambda: api)
    return m


def test_fetch_quota_group_cost_is_member_count(monkeypatch):
    api = _FakeApi(members=3)
    assert _messenger_with(monkeypatch, api).fetch_quota("Cabc") == {"limit": 200, "used": 45, "cost": 3}
    assert ("group", "Cabc") in api.calls


def test_fetch_quota_room_cost_is_member_count(monkeypatch):
    api = _FakeApi(members=5)
    assert _messenger_with(monkeypatch, api).fetch_quota("Rabc")["cost"] == 5
    assert ("room", "Rabc") in api.calls


def test_fetch_quota_user_cost_is_one(monkeypatch):
    api = _FakeApi(members=9)
    assert _messenger_with(monkeypatch, api).fetch_quota("U123") == {"limit": 200, "used": 45, "cost": 1}
    assert not any(c[0] in ("group", "room") for c in api.calls)


def test_fetch_quota_unlimited_returns_none(monkeypatch):
    assert _messenger_with(monkeypatch, _FakeApi(qtype="none")).fetch_quota("U123") is None


def test_fetch_quota_returns_none_on_any_failure(monkeypatch):
    for name in ("quota", "consumption", "group"):
        assert _messenger_with(monkeypatch, _FakeApi(fail=name)).fetch_quota("Cabc") is None

    def broken():
        raise RuntimeError("no network")
    m = lc.LineMessenger("dummy-token")
    monkeypatch.setattr(m, "_api", broken)
    assert m.fetch_quota("U123") is None


def test_fetch_quota_passes_timeout_to_every_call(monkeypatch):
    # 応答停止で配信(push)まで止めないよう、取得系の全呼び出しに timeout を渡す
    for to, n in (("Cabc", 3), ("Rabc", 3), ("U123", 2)):
        api = _FakeApi()
        _messenger_with(monkeypatch, api).fetch_quota(to)
        assert api.timeouts == [lc._QUOTA_TIMEOUT] * n
    assert lc._QUOTA_TIMEOUT == (5, 10)
