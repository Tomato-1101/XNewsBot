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


def _point_titles(bubble: dict) -> list[str]:
    """要点カードの要点行(数字+ジャンル+見出し)の見出しだけを順に取り出す。"""
    out = []
    for c in bubble["body"]["contents"]:
        if c.get("type") == "box" and c.get("action", {}).get("type") == "postback":
            out.append(c["contents"][1]["contents"][1]["text"])
    return out


def _bytes(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _carousels(specs: list[dict]) -> list[dict]:
    """2通目以降のジャンルのカルーセル(1通目の要点・マーケットは含めない)。"""
    return [s["contents"] for s in specs[1:] if s["type"] == "flex"
            and s["contents"]["type"] == "carousel"]


def test_genre_select_marks_selected():
    spec = lc.genre_select_spec(["AI"])
    labels = [q["label"] for q in spec["quick_reply"]]
    assert any(l.startswith("✓") and "AI" in l for l in labels)
    assert any(l.startswith("＋") for l in labels)


# ---- 1通目(要点とマーケットの2枚) ----

def _summary(specs: list[dict]) -> dict:
    """1通目の要点カード(マーケットが無い日は1通目がこのバブルだけ)。"""
    c = specs[0]["contents"]
    return c if c["type"] == "bubble" else c["contents"][0]


def _market(specs: list[dict]) -> dict | None:
    """1通目のマーケットカード(無ければ None)。"""
    c = specs[0]["contents"]
    return c["contents"][1] if c["type"] == "carousel" else None


def _toc(specs: list[dict]) -> list[list[str]]:
    """目次の行を [●, ジャンル名, 件数, 行き先] に。"""
    body = _summary(specs)["body"]["contents"]
    start = next((i for i, c in enumerate(body) if c.get("text") == "目次"), None)
    if start is None:  # 要点カードが注記に差し替わった異常時
        return []
    rows = []
    for c in body[start + 1:]:
        if c.get("type") != "box":
            break
        rows.append(_texts(c))
    return rows


def _cards(specs: list[dict]) -> list[tuple[int, dict]]:
    """2通目以降のカードを (メッセージ番号(1始まり), バブル) で順に。"""
    return [(no, b) for no, s in enumerate(specs[1:], 2) for b in s["contents"]["contents"]]


def _head(bubble: dict) -> list[str]:
    return _texts(bubble["header"]) if "header" in bubble else []


def test_digest_specs_summary_then_genre_cards():
    # _small は score 40(< 50)なので、株の1件目は大きいニュースに上がり、2件目は「その他の見出し」
    grouped = {"話題": [], "AI": [_big()], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert [s["type"] for s in specs] == ["flex", "flex", "flex"]  # 要点 + 1ジャンル1通
    summary = specs[0]["contents"]
    assert summary["type"] == "bubble" and summary["size"] == "giga"  # マーケットが無い日は1枚
    texts = _texts(summary)
    assert texts[:3] == ["6月8日(月) 朝のニュース", "計3件", "目次"]
    assert _toc(specs) == [["●", "AI", "1件", "→ 2通目"], ["●", "株", "2件", "→ 3通目"]]  # 0件は出さない
    assert "今日の要点" in texts and lc.MARKET_HINT not in texts

    ai, st = specs[1]["contents"], specs[2]["contents"]
    assert ai["type"] == st["type"] == "carousel"
    assert [_head(b) for b in ai["contents"] + st["contents"]] == [["AI", "1件"], ["株", "2件"]]
    assert ai["contents"][0]["header"]["backgroundColor"] == "#4F46E5"
    assert st["contents"][0]["header"]["backgroundColor"] == "#0F766E"
    assert specs[1]["alt"] == "AI 1件｜大きな出来事"
    assert specs[2]["alt"] == "株 2件｜小さな話題0"
    stock = _texts(st["contents"][0]["body"])
    assert stock.index("小さな話題0") < stock.index("その他の見出し") < stock.index("小さな話題1")


def test_heading_same_without_greeting_and_evening_word():
    grouped = {"AI": [_big()]}
    a = lc.digest_specs(grouped, greeting=True, slot="evening", digest_date=D, now=NOW)
    b = lc.digest_specs(grouped, greeting=False, slot="evening", digest_date=D, now=NOW)
    assert _texts(a[0]["contents"])[0] == _texts(b[0]["contents"])[0] == "6月8日(月) 夜のニュース"
    assert a[0]["alt"].startswith("夜のニュース｜大きな出来事")


def test_points_order_big_by_score_then_small():
    """常時ジャンルが無い(2026-10-02 に特大を廃止)ので、big を score 降順 → small を score 降順。"""
    assert lc.ALWAYS_KEYS == []
    grouped = {
        "AI": [_big("AI", 0, score=70, title="AI大"), _small(1, "AI", score=90)],
        "株": [_big("株", 0, score=85, title="株大"), _small(1, "株", score=60)],
        "テクノロジー": [_big("テクノロジー", 0, score=70, title="テック大"),
                       _small(1, "テクノロジー", score=60)],
        "話題": [_big("話題", 0, score=10, title="話題大")],
    }
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    # big を score 降順(同点はジャンル順) → small を score 降順、最大5本
    assert _point_titles(_summary(specs)) == ["株大", "AI大", "テック大", "話題大", "小さな話題1"]
    rows = [c for c in _summary(specs)["body"]["contents"]
            if c.get("type") == "box" and c.get("action")]
    assert rows[0]["contents"][0] == {"type": "text", "text": "1", "size": "sm", "weight": "bold",
                                      "color": "#0F766E", "flex": 0}
    assert rows[4]["contents"][1]["contents"][0]["text"] == "AI"  # small は AI(90)が株・テック(60)より上


def test_points_always_genre_first_when_configured(monkeypatch):
    """常時ジャンル(selectable=false)を将来また置いたときは、その big を score に関係なく先頭に。"""
    monkeypatch.setattr(lc, "ALWAYS_KEYS", ["話題"])
    grouped = {"AI": [_big("AI", 0, score=70, title="AI大")],
               "話題": [_big("話題", 0, score=10, title="話題大")]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert _point_titles(_summary(specs)) == ["話題大", "AI大"]


def test_alt_text_first_point_and_count():
    grouped = {"AI": [_big(title="見出しA")], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert specs[0]["alt"] == "朝のニュース｜見出しA ほか2件"
    long = {"AI": [_big(title="長" * 1000)]}
    specs = lc.digest_specs(long, slot="morning", digest_date=D, now=NOW)
    assert all(len(s["alt"]) <= 400 for s in specs)


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
    assert specs[0]["contents"]["type"] == "carousel" and len(specs[0]["contents"]["contents"]) == 2
    body = _market(specs)["body"]["contents"]
    assert _texts(body)[:2] == ["マーケット", "市況（前日終値）"]
    rows = [c for c in body if c.get("type") == "box" and not c.get("action")]
    got = [[(t["text"], t["color"]) for t in r["contents"]] for r in rows]
    assert got == [
        [("日経平均", "#555555"), ("45,210", "#222222"), ("+1.21%", "#C62828")],
        [("S&P500", "#555555"), ("6,512", "#222222"), ("-0.53%", "#1565C0")],
        [("ドル円", "#555555"), ("149.50円", "#222222"), ("+0.00%", "#888888")],  # -0.00 にしない
        [("米10年債", "#555555"), ("4.25%", "#222222"), ("+0.03pt", "#C62828")],
    ]
    blob = json.dumps(specs, ensure_ascii=False)
    assert "▲" not in blob and "▼" not in blob  # ▲▼ は決算サプライズだけ
    # 要点カードには市況を出さず、右のカードへの案内だけ
    assert "市況（前日終値）" not in _texts(_summary(specs))
    assert lc.MARKET_HINT in _texts(_summary(specs))


def test_no_market_card_when_empty():
    for market, schedule in (([], None), (None, []), ([], [_ev("2026-06-08T07:00:00+09:00", "07:00", "過去")])):
        specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, market=market,
                                schedule=schedule, now=NOW)
        assert specs[0]["contents"]["type"] == "bubble"
        texts = _texts(specs[0]["contents"])
        assert "マーケット" not in texts and lc.MARKET_HINT not in texts and ADVICE_NOTE not in texts


CRYPTO_NOTE = "仮想通貨は直近値・24時間比"
ADVICE_NOTE = "評価は一般的な傾向で、投資助言ではありません"
_NIKKEI = {"key": "nikkei", "label": "日経平均", "close": 45210.35, "change": 540.1,
           "change_pct": 1.21, "asof": "2026-06-05", "kind": "index"}


def _section(bubble: dict | None, heading: str) -> list[dict]:
    """カードで見出し heading の後ろ、次の区切り線までの要素(カードが無ければ空)。"""
    if bubble is None:
        return []
    body = bubble["body"]["contents"]
    start = next((i for i, c in enumerate(body) if c.get("text") == heading), None)
    if start is None:
        return []
    out = []
    for c in body[start + 1:]:
        if c["type"] == "separator" or c.get("text") == ADVICE_NOTE:
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
    sec = _section(_market(specs), "市況（前日終値）")
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


def _schedule_lines(specs: list[dict], heading: str = "今日の予定") -> list[tuple[str, list[str]]]:
    """マーケットカードの予定(または注目決算)の各行を (時刻, [名前, 予想・前回]) に。"""
    return [(c["contents"][0]["text"], _texts(c["contents"][1]))
            for c in _section(_market(specs), heading) if c["type"] == "box"]


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
    assert _schedule_lines(specs) == [
        ("08:00", ["ちょうど今", "前回 1.0%"]),
        ("09:00", ["UTC表記"]),
        ("21:30", ["米 雇用統計", "予想 3.0%｜前回 2.9%"]),
        ("翌03:00", ["FOMC"]),
        ("未定", ["時刻未定A"]),          # at が無い予定は最後、元の順のまま
        ("寄り前", ["時刻未定B", "予想 10円"]),
    ]
    # 見出しは市況と同じ体裁で、前に区切り線
    body = _market(specs)["body"]["contents"]
    i = next(k for k, c in enumerate(body) if c.get("text") == "今日の予定")
    assert body[i - 1]["type"] == "separator"
    assert (body[i]["size"], body[i]["weight"]) == ("sm", "bold")


def test_schedule_block_max_12_and_none_when_empty():
    timed = [_ev(f"2026-06-08T{9 + k:02d}:00:00+09:00", f"{9 + k:02d}:00", f"予定{k:02d}")
             for k in range(13)]
    untimed = [_ev(None, "未定", f"未定{k}") for k in range(3)]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D,
                            schedule=untimed + timed, now=NOW)
    lines = _schedule_lines(specs)
    assert [names[0] for _t, names in lines] == [f"予定{k:02d}" for k in range(12)]
    # 0件・全部過ぎた予定ならブロックごと出さない
    for sched in ([], None, [_ev("2026-06-08T07:00:00+09:00", "07:00", "過去")]):
        s = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, schedule=sched, now=NOW)
        assert "今日の予定" not in _texts(s[0]["contents"])


def test_advice_note_at_end_of_market_card_only_when_shown():
    future = [_ev("2026-06-08T21:30:00+09:00", "21:30", "米 雇用統計")]
    past = [_ev("2026-06-08T07:00:00+09:00", "07:00", "過去")]
    cases = [([_NIKKEI], None, True), ([], future, True), ([_NIKKEI], future, True),
             ([], None, False), (None, past, False)]
    for market, schedule, shown in cases:
        specs = lc.digest_specs({"AI": [_big()], "株": [_small(0)]}, slot="morning", digest_date=D,
                                market=market, schedule=schedule, now=NOW)
        assert (ADVICE_NOTE in _texts(specs[0]["contents"])) is shown, (market, schedule)
        if shown:  # マーケットカードの末尾に極小で
            last = _market(specs)["body"]["contents"][-1]
            assert (last["text"], last["size"]) == (ADVICE_NOTE, "xxs")
            assert ADVICE_NOTE not in _texts(_summary(specs))


# ---- 注目決算・決算サプライズ ----

def _earn(country: str, name: str, label: str, at=None, **kw) -> dict:
    ev = {"at": at, "time_label": label, "kind": "earnings", "country": country, "name": name,
          "forecast": "", "previous": "", "result": "", "importance": 3, "notable": True}
    ev.update(kw)
    return ev


def _surp(country: str, name: str, pct, label: str, headline: str = "") -> dict:
    return {"kind": "surprise", "country": country, "name": name, "move_pct": pct,
            "move_label": label, "headline": headline, "at": None, "time_label": "",
            "forecast": "", "previous": "", "result": "", "importance": 3}


def test_earnings_section_by_country_without_trailing_word():
    schedule = [
        _ev("2026-06-08T21:30:00+09:00", "21:30", "米 雇用統計"),
        _earn("US", "Apple（AAPL）決算", "引け後"),
        _earn("JP", "トヨタ自動車（7203）決算", "15:00", at="2026-06-08T15:00:00+09:00"),
        _earn("JP", "過ぎた会社（1111）決算", "07:00", at="2026-06-08T07:00:00+09:00"),  # 過ぎたら出さない
        _earn("JP", "ソニーグループ（6758）決算", "未定"),
    ]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, schedule=schedule, now=NOW)
    # 決算は今日の予定には出さない
    assert [n[0] for _t, n in _schedule_lines(specs)] == ["米 雇用統計"]
    sec = _section(_market(specs), "注目決算")
    assert [c.get("text") if c["type"] == "text" else _texts(c) for c in sec] == [
        "日本", ["15:00", "トヨタ自動車（7203）"], ["未定", "ソニーグループ（6758）"],
        "米国", ["引け後", "Apple（AAPL）"],
    ]
    texts = _texts(_market(specs))
    assert texts.index("今日の予定") < texts.index("注目決算") < texts.index(ADVICE_NOTE)


def test_earnings_fallback_note_and_overflow_count():
    schedule = [_earn("JP", f"代用{k}（{1000 + k}）決算", "未定", fallback=True) for k in range(10)]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, schedule=schedule, now=NOW)
    sec = _section(_market(specs), "注目決算（時価総額上位で代用）")
    rows = [c for c in sec if c["type"] == "box"]
    assert len(rows) == lc.EARNINGS_MAX_PER_COUNTRY
    assert sec[-1]["text"] == f"ほか {10 - lc.EARNINGS_MAX_PER_COUNTRY} 社"  # 黙って消さない
    # 代用が無ければ見出しは「注目決算」だけ
    plain = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D,
                            schedule=[_earn("JP", "A社（1）決算", "15:00")], now=NOW)
    assert "注目決算" in _texts(_market(plain))
    assert "注目決算（時価総額上位で代用）" not in _texts(_market(plain))


def test_surprise_section_up_green_down_red_jp_first():
    schedule = [
        _surp("US", "Nike（NKE）", -8.6, "時間外"),
        _surp("JP", "グラファイトデザイン（7847）", 14.8, "PTS", headline="今期経常を上方修正"),
        _surp("JP", "下げ会社（2222）", -5.04, "PTS"),
        _surp("US", "壊れた行", None, "当日"),             # 騰落率が無い行は出さない
    ]
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, schedule=schedule, now=NOW)
    sec = _section(_market(specs), "決算サプライズ")
    assert [c.get("text") if c["type"] == "text" else _texts(c) for c in sec] == [
        "日本",
        ["▲ +14.8%", "グラファイトデザイン（7847）", "PTS", "今期経常を上方修正"],
        ["▼ -5.0%", "下げ会社（2222）", "PTS"],
        "米国",
        ["▼ -8.6%", "Nike（NKE）", "時間外"],
    ]
    rows = [c for c in sec if c["type"] == "box"]
    assert rows[0]["contents"][0]["contents"][0]["color"] == lc.SURPRISE_UP_COLOR
    assert rows[1]["contents"][0]["contents"][0]["color"] == lc.SURPRISE_DOWN_COLOR
    assert rows[0]["contents"][1]["size"] == "xxs"  # 見出しは小さく2行目
    # サプライズは今日の予定にも注目決算にも出さない
    assert _section(_market(specs), "今日の予定") == [] and _section(_market(specs), "注目決算") == []


def test_earnings_and_surprise_hidden_when_empty():
    specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, market=[_NIKKEI],
                            schedule=[_ev("2026-06-08T21:30:00+09:00", "21:30", "米 雇用統計"),
                                      _surp("JP", "壊れた行", "x", "PTS")], now=NOW)
    texts = _texts(_market(specs))
    assert "今日の予定" in texts
    assert not any(t.startswith("注目決算") for t in texts) and "決算サプライズ" not in texts
    # 決算・サプライズだけでもマーケットカードは出る
    only = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D,
                           schedule=[_surp("US", "Nike（NKE）", 3.2, "当日")], now=NOW)
    assert _texts(_market(only))[:2] == ["マーケット", "決算サプライズ"]


def test_new_genre_colors_use_genre_label():
    grouped = {"暗号資産": [_big("暗号資産")], "話題": [_big("話題")]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    heads = [b["header"] for _no, b in _cards(specs)]
    assert [h["backgroundColor"] for h in heads] == ["#B45309", "#A21CAF"]
    assert [_texts(h)[0] for h in heads] == [lc._genre_label("暗号資産"), lc._genre_label("話題")]


def test_removed_genre_past_items_render_with_other_color():
    """廃止したジャンル(特大)の過去記事でも落ちない: ラベルは DB の名前のまま・色は OTHER_COLOR・
    詳細タップのキーもそのジャンル名。"""
    assert "特大" not in lc.GENRES
    grouped = {"特大": [_big("特大", 0, title="過去の特大")], "AI": [_big("AI", 0)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert _toc(specs)[0] == ["●", "特大", "1件", "→ 2通目"]
    card = specs[1]["contents"]["contents"][0]
    assert _head(card) == ["特大", "1件"] and card["header"]["backgroundColor"] == lc.OTHER_COLOR
    assert "detail:20260608:morning:特大:0" in _bubble_datas(card)
    assert lc.detail_spec(grouped["特大"][0])["text"].startswith("【特大】過去の特大")


def test_digest_specs_empty():
    specs = lc.digest_specs({"AI": []}, greeting=True)
    assert len(specs) == 1 and specs[0]["type"] == "text"


# ---- ジャンルごとのカード ----

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
    assert len(specs) == 2
    bubble = specs[1]["contents"]["contents"][0]
    assert _head(bubble) == ["株", "3件"]
    body = bubble["body"]["contents"]
    assert "@v・2日前" in _texts(body)
    smalls = [c for c in body if c.get("type") == "box" and c.get("action")]
    assert [c["action"]["data"] for c in smalls] == ["detail:20260608:morning:株:1",
                                                    "detail:20260608:morning:株:2"]
    # 大きいニュース → 小見出し「その他の見出し」 → 見出しの行
    heading = next(i for i, c in enumerate(body) if c.get("text") == "その他の見出し")
    assert body[heading - 1]["type"] == "separator" and body[heading]["size"] == "xs"
    assert body.index(smalls[0]) == heading + 1
    # small 1件だけのジャンルはそれが大きいニュースに上がる(小見出しは出ない)
    only_small = lc.digest_specs({"株": [_small(0)]}, slot="morning", digest_date=D, now=NOW)
    assert len(only_small) == 2
    texts = _texts(only_small[1]["contents"])
    assert "小さな話題0" in texts and "詳細を読む" in texts and "その他の見出し" not in texts


def test_notable_row_shows_summary_truncated_and_keeps_tap():
    """注目(score >= 50)は 見出し(太字) → 要約(100字超は「…」) → 出典・時刻 の順。要約が空なら出さない。"""
    s_short, s_long, s_exact, s_empty = (_small(i, score=60) for i in (1, 2, 3, 4))
    s_short.summary = "短い要約"
    s_long.summary = "あ" * 100 + "いう"
    s_exact.summary = "え" * 100
    s_empty.summary = ""
    specs = lc.digest_specs({"株": [_big("株", 0), s_short, s_long, s_exact, s_empty]},
                            slot="morning", digest_date=D, now=NOW)
    body = specs[1]["contents"]["contents"][0]["body"]["contents"]
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
    assert "注目" in _texts(body) and "その他の見出し" not in _texts(body)


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
    first = specs[0]["contents"]
    if first["type"] == "carousel":
        assert len(first["contents"]) == 2 and _bytes(first) <= lc.CAROUSEL_MAX_BYTES
        assert all(_bytes(b) <= lc.BUBBLE_MAX_BYTES for b in first["contents"])
    else:
        assert _bytes(first) <= lc.BUBBLE_MAX_BYTES
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
    _check_genres_contiguous(grouped, specs)
    _check_toc_matches(grouped, specs)


def _check_genres_contiguous(grouped, specs) -> None:
    """ジャンルのカードは横に連続し(別ジャンルが割り込まない)、grouped の順に並ぶ。
    1枚に2ジャンルが混ざらず、ヘッダー右の「k/n」が 1 から連番。"""
    labels = []
    for _no, b in _cards(specs):
        if "header" not in b:   # 省略注記
            continue
        label = _head(b)[0]
        if not labels or labels[-1][0] != label:
            labels.append((label, []))
        labels[-1][1].append(_head(b)[1])
        genre = next(g for g in grouped if lc._genre_label(g) == label)
        assert all(d.split(":")[3] == genre for d in _bubble_datas(b) if d.count(":") == 4)
    order = [lc._genre_label(g) for g, items in grouped.items() if items]
    assert [lb for lb, _ in labels] == order[:len(labels)]
    dropped_cards = "件は省略" in json.dumps(specs, ensure_ascii=False)
    for label, notes in labels:
        shown = len(notes)
        total = len(grouped[next(g for g in grouped if lc._genre_label(g) == label)])
        if notes == [f"{total}件"]:
            continue
        n = int(notes[0].rsplit("/", 1)[1])
        assert n == shown or (dropped_cards and n > shown)
        assert notes == [f"{total}件 {k}/{n}" for k in range(1, shown + 1)]


def _check_toc_matches(grouped, specs) -> None:
    """目次の「→ k通目」が、そのジャンルのカードが実際に載ったメッセージ番号と一致する。"""
    where: dict[str, list[int]] = {}
    for no, b in _cards(specs):
        if "header" in b:
            where.setdefault(_head(b)[0], []).append(no)
    for _dot, label, count, dest in _toc(specs):
        nos = where.get(label, [])
        expect = lc._msg_range(nos)
        assert dest.startswith(expect), (label, dest, nos)
        genre = next(g for g in grouped if lc._genre_label(g) == label)
        assert count == f"{len(grouped[genre])}件"


def test_bulk_4_genres_x20_fits_and_keeps_every_item():
    grouped = _bulk(["AI", "株", "テクノロジー", "話題"], 20)
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
    """重い日の実例: AI35(big5+small30)・株25・暗号資産20・テクノロジー25・話題28(旧特大の big 3件を含む)。
    見出し40字前後・big の要約150字・small の要約100字・出典つき(big 3件・small 2件)。
    small の先頭6件は注目(score >= 50)、残りはその他の見出し。"""
    plan = [("AI", 5, 30), ("株", 3, 22), ("暗号資産", 3, 17), ("テクノロジー", 3, 22), ("話題", 6, 22)]
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
                genre=g, importance="big" if big else "small", rank=r,
                score=90 - r if r < n_big + 6 else 30,
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
    for k in range(12)] + [
    _earn("JP" if k < 8 else "US", f"注目企業{k:02d}ホールディングス（{7000 + k}）決算", "引け後")
    for k in range(16)] + [
    _surp("JP" if k < 5 else "US", f"サプライズ企業{k}（{8000 + k}）", 10.0 - 3 * k, "PTS",
          headline="通期の営業利益を上方修正、市場予想を上回る" if k < 5 else "")
    for k in range(10)]


def test_heavy_day_within_limits_and_market_card_fits():
    """重い日(約140件・市況8行・予定12行・決算16社・サプライズ10社)でも各上限内に収まり、
    出た記事 + 省略件数 = 全件。マーケットカードは切り詰めずに全部の節が載る。"""
    grouped = _heavy()
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                            schedule=_HEAVY_SCHEDULE, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    _check_accounting(grouped, specs)
    # 大きいニュースと注目は省略しない(削るのはその他の見出しの末尾から)
    shown = set(d for car in _carousels(specs) for d in _bubble_datas(car))
    assert all(f"detail:20260608:morning:{g}:{it.rank}" in shown
               for g, items in grouped.items() for it in items if (it.score or 0) >= 50)
    assert len(_schedule_lines(specs)) == 12
    assert len([c for c in _section(_market(specs), "市況（前日終値）") if c["type"] == "box"]) == 8
    assert len([c for c in _section(_market(specs), "注目決算") if c["type"] == "box"]) == 16
    assert len([c for c in _section(_market(specs), "決算サプライズ") if c["type"] == "box"]) == 10
    assert lc.TOO_LARGE_NOTE not in _texts(specs[0]["contents"])


def test_each_genre_gets_own_message_when_slots_allow():
    """原則1ジャンル=1通(縦に並ぶのでジャンル単位で追え、件数の違うジャンル同士でバブルの高さが
    揃えられて空白が出ることもない)。4ジャンルなら2〜5通目に1つずつ。"""
    grouped = {"AI": [_big("AI", 0)],
               "株": [_big("株", r, title=f"株{r}") for r in range(12)],
               "テクノロジー": [_big("テクノロジー", 0)], "話題": [_small(0, "話題")]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == 5
    assert [{_head(b)[0] for b in s["contents"]["contents"]} for s in specs[1:]] == [
        {"AI"}, {"株"}, {"テクノロジー"}, {"話題"}]
    assert [row[3] for row in _toc(specs)] == ["→ 2通目", "→ 3通目", "→ 4通目", "→ 5通目"]
    assert specs[2]["alt"] == "株 12件｜株0"


def test_adjacent_small_genres_share_a_message_only_when_over_slots():
    """6ジャンルで通数(4)が足りない日だけ、隣り合うジャンルを1通にまとめる。まとめるのはカードの合計が
    一番少ない組(同数なら後ろの組から)。目次の通番もそれに合わせる。"""
    genres = ["AI", "株", "暗号資産", "テクノロジー", "話題", "ビジネス"]
    grouped = _bulk(genres, 8)
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == 5
    assert [[_head(b)[0] for b in s["contents"]["contents"]] for s in specs[1:]] == [
        ["AI"], ["株"], ["仮想通貨", "テクノロジー"], ["話題", "ビジネス"]]
    assert [row[3] for row in _toc(specs)] == ["→ 2通目", "→ 3通目", "→ 4通目", "→ 4通目",
                                               "→ 5通目", "→ 5通目"]
    assert specs[4]["alt"] == "話題 8件・ビジネス 8件｜話題の見出し00" + "あ" * 30


def test_large_genre_spans_messages_with_continuous_numbering():
    # 1ジャンル40件の big(要約400字)は 1通(48000B)に入らないので通をまたぎ、番号は通をまたいで連番
    grouped = _bulk(["AI"], 40, summary_len=400)
    for it in grouped["AI"]:
        it.importance = "big"
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    cards = _cards(specs)
    n = len(cards)
    assert [_head(b) for _no, b in cards] == [["AI", f"40件 {k}/{n}"] for k in range(1, n + 1)]
    nos = sorted({no for no, _b in cards})
    assert len(nos) >= 2 and nos == list(range(2, nos[-1] + 1))
    assert _toc(specs) == [["●", "AI", "40件", f"→ 2〜{nos[-1]}通目"]]
    # 分割後のバブル先頭は区切り線で始めない
    assert all(b["body"]["contents"][0]["type"] != "separator" for _no, b in cards)
    datas = [d for _no, b in cards for d in _bubble_datas(b)]
    assert datas == [f"detail:20260608:morning:AI:{r}" for r in range(40)]


def test_overflow_beyond_5_messages_shows_omitted_count():
    # 基本的に起きない量(全部 big で削れる見出しが無い)。入りきらない分は後ろのカードごと落とし、
    # 黙って消さず「ほか N 件は省略」を最後に出す
    grouped = _bulk(["AI", "株", "テクノロジー", "話題"], 150, summary_len=400)
    for items in grouped.values():
        for it in items:
            it.importance = "big"
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == lc.MAX_MESSAGES
    last_bubble = _carousels(specs)[-1]["contents"][-1]
    m = re.fullmatch(r"ほか (\d+) 件は省略", _texts(last_bubble)[0])
    assert m
    _check_accounting(grouped, specs)
    toc = _toc(specs)
    assert toc[-1][3] == "→ 省略"     # 全部落ちたジャンルも目次に出す
    assert re.fullmatch(r"→ 5通目（\d+件省略）", toc[0][3]) is None  # AI は 2〜3通目
    assert toc[0][3].startswith("→ 2")


def test_overflow_trims_brief_from_the_end_and_counts_it():
    """5通に入りきらない日は「その他の見出し」の末尾から削る(大きいニュース・注目は残す)。
    削るのは残りの多いジャンルから1件ずつ。削った件数は最後の通の末尾と目次に出す。"""
    genres = ["AI", "株", "暗号資産", "テクノロジー", "話題"]
    grouped = {}
    for g in genres:
        grouped[g] = [_big(g, 0)]
        grouped[g] += [_small(r, g, score=70) for r in range(1, 11)]
        grouped[g] += [_small(r, g, score=30) for r in range(11, 11 + (100 if g == "AI" else 80))]
        for it in grouped[g][1:]:
            it.summary = "要" * 100
            it.title = f"{g}の話題{it.rank:02d}" + "あ" * 30
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    assert len(specs) == lc.MAX_MESSAGES
    datas = _check_accounting(grouped, specs)
    omitted = _omitted(specs)
    assert omitted > 0
    assert re.fullmatch(r"ほか \d+ 件は省略", _texts(_carousels(specs)[-1]["contents"][-1])[0])
    shown = set(datas)
    for g in genres:
        ranks = [it.rank for it in grouped[g] if f"detail:20260608:morning:{g}:{it.rank}" in shown]
        assert ranks[:11] == list(range(11))                      # big と注目は全部
        assert ranks == list(range(len(ranks)))                   # 削ったのは末尾だけ
    # 目次の省略件数の合計 = 末尾の「ほか N 件」
    toc_dropped = [int(m.group(1)) for row in _toc(specs) for m in [re.search(r"（(\d+)件省略）", row[3])] if m]
    assert sum(toc_dropped) == omitted
    # 見出しが一番多い AI から削る
    left = {g: sum(1 for d in shown if d.split(":")[3] == g) for g in genres}
    assert max(left.values()) - min(left.values()) <= 1


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


def _bubble_datas(bubble) -> list[str]:
    return [a["data"] for a in _actions(bubble) if a["type"] == "postback"]


def test_genre_cards_ordered_big_then_notable_then_brief():
    """ジャンルの中は 大きいニュース → 注目 → その他の見出し が途切れず続き、段の変わり目に小見出し。"""
    grouped = {g: [_big(g, 0), _big(g, 1)] + [_small(i, g, score=70 if i % 2 else 30) for i in range(2, 16)]
               for g in ["AI", "株", "暗号資産", "話題"]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    _check_limits_and_coverage(grouped, specs)
    for g, items in grouped.items():
        cards = [b for _no, b in _cards(specs) if _head(b) and _head(b)[0] == lc._genre_label(g)]
        seq = [int(d.rsplit(":", 1)[1]) for b in cards for d in _bubble_datas(b)]
        notable = [i for i in range(2, 16) if i % 2]
        brief = [i for i in range(2, 16) if not i % 2]
        assert seq == [0, 1] + notable + brief
        heads = [t for b in cards for t in _texts(b["body"]) if t in ("注目", "その他の見出し")]
        # 小見出しは段の始まりと、ページの先頭(続きのページ)にだけ出る
        assert heads[0] == "注目" and "その他の見出し" in heads
        assert heads == sorted(heads, key=lambda t: t != "注目")
    assert _omitted(specs) == 0


def test_genre_without_big_promotes_top_item():
    """big が0件のジャンルは rank 最上位の1件を大きいニュースの見た目で先頭に出し、下の段からは除く。"""
    small_genre = [_small(2, "株"), _small(0, "株"), _small(1, "株")]  # 並びは rank 順でなくてよい
    grouped = {"AI": [_big()], "株": small_genre}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    stock = specs[2]["contents"]["contents"][0]
    assert _head(stock) == ["株", "3件"]
    assert _bubble_datas(stock) == ["detail:20260608:morning:株:0", "detail:20260608:morning:株:1",
                                    "detail:20260608:morning:株:2"]
    title = next(n for n in lc._text_nodes(stock["body"]) if n["text"] == "小さな話題0")
    assert (title["size"], title.get("weight")) == ("md", "bold")
    assert "詳細を読む" in _texts(stock["body"])


def test_every_item_appears_exactly_once():
    """全記事がジャンルのカードのどこか1か所だけに出る(省略注記が無い量のとき)。"""
    grouped = {"AI": [_big("AI", 0), _big("AI", 1), _small(2, "AI")],
               "株": [_small(0), _small(1)], "話題": [_big("話題", 0), _small(1, "話題")]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    datas = [d for s in specs[1:] for d in _bubble_datas(s["contents"])]
    assert sorted(datas) == sorted(f"detail:20260608:morning:{g}:{it.rank}"
                                   for g, items in grouped.items() for it in items)
    assert "件は省略" not in json.dumps(specs, ensure_ascii=False)


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


def test_tier_split_by_score_boundary_49_50():
    """主以外は score 50 以上が注目、49 以下がその他の見出し。"""
    grouped = {"AI": [_big(), _small(1, "AI", score=50), _small(2, "AI", score=49)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert len(specs) == 2
    texts = _texts(specs[1]["contents"]["contents"][0]["body"])
    assert texts.index("注目") < texts.index("小さな話題1") < texts.index("その他の見出し") \
        < texts.index("小さな話題2")
    assert lc.NOTABLE_MIN_SCORE == 50


def test_page_weight_and_position_header():
    """1枚は重さ20まで(大きいニュース3・注目1.5・見出し1)。枚数は最小のまま各カードの重さを均す
    (カルーセルのバブルは一番高いものに高さが揃うので、最後だけ短いと白い空白になる)。
    ヘッダー右は「件数 k/n」(1枚なら件数だけ)。続きのページの先頭は区切り線でなく段の小見出しから。"""
    def cards(items):
        specs = lc.digest_specs({"AI": items}, slot="morning", digest_date=D, now=NOW)
        _check_limits_and_coverage({"AI": items}, specs)
        return [b for _no, b in _cards(specs)]

    assert lc.PAGE_WEIGHT_MAX == 20
    # 3 + 1.5×12 = 21 → 2枚。均すと 10.5 ずつ(大1+注目5 / 注目7)。均さないと 3+1.5×11=19.5 と 1.5
    notable = cards([_big("AI")] + [_small(i, "AI", score=80) for i in range(1, 13)])
    assert [_head(b) for b in notable] == [["AI", "13件 1/2"], ["AI", "13件 2/2"]]
    assert [len(_bubble_datas(b)) for b in notable] == [6, 7]
    assert notable[1]["body"]["contents"][0].get("text") == "注目"

    # 3 + 1×17 = 20 → ちょうど1枚(ヘッダーは件数だけ)
    one = cards([_big("AI")] + [_small(i, "AI", score=30) for i in range(1, 18)])
    assert [_head(b) for b in one] == [["AI", "18件"]]

    # 3 + 1×30 = 33 → 2枚を 17 と 16 に均す
    brief = cards([_big("AI")] + [_small(i, "AI", score=30) for i in range(1, 31)])
    assert [len(_bubble_datas(b)) for b in brief] == [15, 16]
    assert brief[1]["body"]["contents"][0].get("text") == "その他の見出し"
    rows = [c for c in brief[1]["body"]["contents"] if c.get("action")]
    assert all(_texts(r) == [f"小さな話題{i}", "@v"] for i, r in enumerate(rows, 15))
    assert "小要約" not in _texts(brief)
    title, meta = rows[0]["contents"]
    assert (title["size"], title["color"], "weight" in title) == ("sm", lc.TEXT_COLOR, False)
    assert meta["size"] == "xxs" and not meta.get("wrap")  # 出典・時刻は1行


def _heavy7(kind: str) -> dict[str, list[NewsItem]]:
    """7ジャンル×30件(先頭3件 big)・要約100字。kind で主以外の score を決める。
    「特大」(廃止)・「ビジネス」(未登録)も混ぜて、genres.toml に無いジャンルでも落ちないことを見る。"""
    genres = ["AI", "株", "暗号資産", "テクノロジー", "話題", "特大", "ビジネス"]
    grouped = _bulk(genres, 30, summary_len=100)
    for items in grouped.values():
        for k, it in enumerate(items[3:]):
            it.score = {"notable": 70, "brief": 30, "mixed": 70 if k % 2 else 30}[kind]
    return grouped


def test_heavy_7_genres_x30_within_limits_for_any_tier_mix():
    """注目が多い日・その他が多い日・半々の日のどれでも、5通・12枚・48000B・28000B を超えず、
    全記事がどこかに出るか省略件数に数えられる。ジャンルのカードは連続し、目次は実際の通番と一致。"""
    for kind in ("notable", "brief", "mixed"):
        grouped = _heavy7(kind)
        specs = lc.digest_specs(grouped, slot="morning", digest_date=D, market=_HEAVY_MARKET,
                                schedule=_HEAVY_SCHEDULE, now=NOW,
                                x_usage={"used": 9870, "remaining": 3_040_677},
                                line_quota={"limit": 200, "used": 45, "cost": 3})
        _check_limits_and_coverage(grouped, specs)
        _check_accounting(grouped, specs)
        if kind == "brief":
            assert _omitted(specs) == 0  # 見出しだけなら 189件でも収まる


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
    assert cmsg.alt_text == "AI 1件｜大きな出来事"


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
        {"AI": [_big()]}, slot="morning", digest_date=D, schedule=early + [fomc], now=NOW))]
    assert len(names) == 12 and names[-1] == "FOMC 政策金利発表" and "決算11" not in names


def test_schedule_block_keeps_approx_time_for_grace_period():
    """「昼ごろ」の予定は近似時刻(at)を過ぎても3時間は出す。確定時刻の予定は過ぎたら出さない。"""
    sched = [_ev("2026-06-08T06:00:00+09:00", "昼ごろ", "日銀 結果発表"),
             _ev("2026-06-08T06:00:00+09:00", "06:00", "過去の指標")]
    names = [n[0] for _t, n in _schedule_lines(lc.digest_specs(
        {"AI": [_big()]}, slot="morning", digest_date=D, schedule=sched, now=NOW))]
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
    """区切り線 → 見出し「残り使用量」(今日の要点と同じ書式) → X → LINE の順で要点カードの末尾。
    データが None でも出る。"""
    for x_usage, line_quota in ((None, None),
                                ({"used": 9870, "remaining": 3_040_677},
                                 {"limit": 200, "used": 45, "cost": 3})):
        specs = lc.digest_specs({"AI": [_big()]}, slot="morning", digest_date=D, now=NOW,
                                market=[_NIKKEI], x_usage=x_usage, line_quota=line_quota)
        body = _summary(specs)["body"]["contents"]
        i = next(k for k, c in enumerate(body) if c.get("text") == "残り使用量")
        assert body[i - 1]["type"] == "separator"
        head = body[i]
        points_head = next(c for c in body if c.get("text") == "今日の要点")
        assert {k: v for k, v in head.items() if k != "text"} == \
            {k: v for k, v in points_head.items() if k != "text"}
        assert body[i + 1]["text"].startswith(X_LABEL)
        assert body[i + 2]["text"].startswith(LINE_LABEL)
        assert len(body) == i + 3  # 要点カードの末尾


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
