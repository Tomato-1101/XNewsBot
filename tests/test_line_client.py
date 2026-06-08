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


def test_digest_specs_structure():
    grouped = {"AI": [_big()], "株": [_small(0), _small(1)]}
    specs = lc.digest_specs(grouped, greeting=True)
    types = [s["type"] for s in specs]
    assert types[0] == "text"   # 挨拶
    assert "flex" in types       # 大/小
    # 小ニュースのバブルに詳細 postback がある
    flex = [s for s in specs if s["type"] == "flex"]
    small_flex = flex[-1]["contents"]["contents"][0]
    btn = small_flex["footer"]["contents"][0]["action"]
    assert btn["type"] == "postback" and btn["data"].startswith("detail:")


def test_digest_specs_empty():
    specs = lc.digest_specs({"AI": []}, greeting=True)
    assert len(specs) == 1 and specs[0]["type"] == "text"


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

    flex_spec = lc.digest_specs({"AI": [_big()]}, greeting=False)[0]
    assert flex_spec["type"] == "flex"
    fmsg = lc._spec_to_message(flex_spec)
    assert fmsg.alt_text == "大ニュース"
