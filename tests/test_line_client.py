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

def test_digest_specs_summary_then_genre_carousel():
    grouped = {"特大": [], "AI": [_big()], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, slot="morning", digest_date=D, now=NOW)
    assert [s["type"] for s in specs] == ["flex", "flex"]
    summary = specs[0]["contents"]
    assert summary["type"] == "bubble" and summary["size"] == "giga"
    texts = _texts(summary)
    assert texts[0] == "6月8日(月) 朝のニュース"
    assert texts[1] == "AI 1・株 2（計3件）"  # 0件のジャンル(特大)は出さない
    assert "今日の要点" in texts
    assert "ジャンル別の記事は次のカードを横にスワイプ →" in texts

    car = specs[1]["contents"]
    assert car["type"] == "carousel"
    heads = [_texts(b["header"]) for b in car["contents"]]
    assert heads == [["AI", "1件"], ["株", "2件"]]  # grouped の順、1ジャンル1枚
    assert car["contents"][0]["header"]["backgroundColor"] == "#4F46E5"
    assert car["contents"][1]["header"]["backgroundColor"] == "#0F766E"
    assert specs[1]["alt"] == "ジャンル別ニュース（AI・株）"


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


def test_digest_specs_empty():
    specs = lc.digest_specs({"AI": []}, greeting=True)
    assert len(specs) == 1 and specs[0]["type"] == "text"


# ---- ジャンル別カルーセル ----

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
    body = specs[1]["contents"]["contents"][0]["body"]["contents"]
    texts = _texts(body)
    assert "ほかの見出し" in texts
    assert "@v・2日前" in texts
    smalls = [c for c in body if c.get("type") == "box" and c.get("action")]
    assert [c["action"]["data"] for c in smalls] == ["detail:20260608:morning:株:1",
                                                    "detail:20260608:morning:株:2"]
    # small だけのジャンルには「ほかの見出し」を出さない(大ニュースとの区切りが無いため)
    only_small = lc.digest_specs({"株": [_small(0)]}, slot="morning", digest_date=D, now=NOW)
    assert "ほかの見出し" not in _texts(only_small[1]["contents"])


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
    assert all(_texts(b["header"])[1] == "40件" for b in bubbles)  # 件数はジャンル全体
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
    assert cmsg.alt_text == "ジャンル別ニュース（AI）"


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
