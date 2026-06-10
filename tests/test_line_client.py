from __future__ import annotations

from xnewsbot import line_client as lc
from xnewsbot.models import NewsItem


def _big() -> NewsItem:
    return NewsItem(genre="AI", importance="big", rank=0, title="大きな出来事",
                    summary="重要な要約", source_urls=["https://x.com/u/status/1"],
                    source_tweets=[{"author": "u", "url": "https://x.com/u/status/1", "views": 9000}],
                    top_view_count=9000)


def _small(i: int) -> NewsItem:
    return NewsItem(genre="株", importance="small", rank=i, title=f"小さな話題{i}",
                    summary="小要約", source_urls=[f"https://x.com/u/status/{i}"],
                    source_tweets=[{"author": "v", "url": f"https://x.com/u/status/{i}", "views": 100}],
                    top_view_count=100)


def test_genre_select_marks_selected():
    spec = lc.genre_select_spec(["AI"])
    labels = [q["label"] for q in spec["quick_reply"]]
    assert any(l.startswith("✓") and "AI" in l for l in labels)
    assert any(l.startswith("＋") for l in labels)


def test_digest_specs_single_bubble_with_detail():
    # 通数節約のため挨拶+大+小は1枚の縦長バブル(1メッセージ)にまとまる
    grouped = {"AI": [_big()], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, greeting=True, slot="morning")
    assert len(specs) == 1 and specs[0]["type"] == "flex"
    body = specs[0]["contents"]["body"]["contents"]
    # 小ニュース行にタップ詳細 postback がある
    details = [c["action"]["data"] for c in body
               if c.get("action", {}).get("data", "").startswith("detail:")]
    assert details


def test_digest_specs_empty():
    specs = lc.digest_specs({"AI": []}, greeting=True)
    assert len(specs) == 1 and specs[0]["type"] == "text"


def test_big_block_has_detail_tap():
    # 大ニュースも一覧は見出し+要約までで、タップ(postback detail:)で長文詳細を開ける
    item = _big()
    item.id = 42
    grouped = {"AI": [item]}
    specs = lc.digest_specs(grouped, greeting=False)
    blob = __import__("json").dumps(specs, ensure_ascii=False)
    assert "detail:42" in blob


def test_detail_spec_keeps_link_drops_body():
    item = _big()
    item.detail = "これは長い詳細解説の本文です。"
    item.source_tweets = [{"text": "ツイート本文は載せない", "author": "u",
                           "url": "https://x.com/u/status/1", "views": 9000}]
    spec = lc.detail_spec(item)
    assert "大きな出来事" in spec["text"]
    assert "これは長い詳細解説の本文です。" in spec["text"]   # detail は出す
    assert "ツイート本文は載せない" not in spec["text"]       # 元ポスト本文は出さない
    assert "https://x.com/u/status/1" in spec["text"]        # リンクは残す


def test_detail_spec_contains_title_and_source():
    spec = lc.detail_spec(_big())
    assert "大きな出来事" in spec["text"]
    assert "https://x.com/u/status/1" in spec["text"]


def test_digest_specs_uses_stable_detail_key_with_date():
    """digest_date を渡すと postback は安定キー(日付:slot:genre:rank)になる(id 直指定でない)。"""
    import json
    from datetime import date
    item = _big()
    item.id = 7
    item.rank = 0
    specs = lc.digest_specs({"AI": [item]}, greeting=False, slot="morning",
                            digest_date=date(2026, 6, 8))
    blob = json.dumps(specs, ensure_ascii=False)
    assert "detail:20260608:morning:AI:0" in blob
    assert '"detail:7"' not in blob  # id 直指定にフォールバックしていない


def test_pack_bubbles_overflow_adds_notice():
    """5メッセージを超える量は黙って捨てず、最後のバブルに省略を明示する。"""
    comps = [{"type": "text", "text": "あ" * 2200, "wrap": True,
              "action": {"type": "postback", "data": f"detail:{i}"}} for i in range(7)]
    specs = lc._pack_bubbles(comps, alt_first="a", alt_rest="b")
    assert len(specs) == lc.MAX_MESSAGES  # 5 で頭打ち
    last_body = specs[-1]["contents"]["body"]["contents"]
    assert any("次回の配信" in c.get("text", "") for c in last_body)


def test_pack_bubbles_measures_utf8_bytes():
    """バブル分割はUTF-8バイト数で判定する(日本語は1文字3バイト)。件数は削らない。"""
    import json
    comp = {"type": "text", "text": "日本語のニュース本文。" * 45, "wrap": True}
    specs = lc._pack_bubbles([dict(comp) for _ in range(12)], alt_first="a", alt_rest="b")
    assert len(specs) > 1  # 1バブルに収まらない量であること(判定が効いている前提の確認)
    for s in specs:
        body = s["contents"]["body"]["contents"]
        assert len(json.dumps(body, ensure_ascii=False).encode("utf-8")) <= lc.BUBBLE_MAX_BYTES
    assert sum(len(s["contents"]["body"]["contents"]) for s in specs) == 12


def test_spec_to_sdk_message_text_and_flex():
    """line-bot-sdk v3 への変換が壊れていないか(API名の検証)。"""
    text = lc.text_spec("こんにちは", [{"label": "AI", "data": "genre:AI"}])
    msg = lc._spec_to_message(text)
    assert msg.text == "こんにちは"
    assert msg.quick_reply is not None

    flex_spec = lc.digest_specs({"AI": [_big()]}, greeting=False)[0]
    assert flex_spec["type"] == "flex"
    fmsg = lc._spec_to_message(flex_spec)
    assert fmsg.alt_text == "今日のニュース"
