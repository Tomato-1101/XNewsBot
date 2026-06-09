from __future__ import annotations

from xnewsbot.curator import (
    MAX_BIG_PER_GENRE,
    _extract_json_array,
    curation_instructions,
    format_tweets_for_curation,
    parse_curated,
)

from .conftest import make_tweets


def test_extract_json_array_with_code_fence():
    raw = '```json\n[{"title":"a","importance":"small","score":1,"source_idxs":[0]}]\n```'
    out = _extract_json_array(raw)
    assert out[0]["title"] == "a"


def test_parse_curated_orders_big_first_and_caps():
    # big を上限+2 件 → 上限まで big、残りは small に降格。大→小・score降順。
    data = [{"title": f"b{i}", "summary": "s", "importance": "big",
             "score": 100 - i, "source_idxs": [i]} for i in range(MAX_BIG_PER_GENRE + 2)]
    items = parse_curated(data)
    bigs = [i for i in items if i.importance == "big"]
    assert len(bigs) == MAX_BIG_PER_GENRE
    assert items[0].importance == "big"
    assert items[0].score >= items[1].score


def test_parse_curated_accepts_json_string():
    items = parse_curated('[{"title":"x","importance":"small","score":3,"source_idxs":[0]}]')
    assert items[0].title == "x" and items[0].importance == "small"


def test_parse_curated_skips_invalid_entries():
    data = [{"summary": "no title"}, {"title": "ok", "importance": "small", "score": 1}]
    items = parse_curated(data)
    assert len(items) == 1 and items[0].title == "ok"


def test_parse_curated_keeps_detail():
    items = parse_curated([{"title": "x", "summary": "短い", "detail": "長い詳細解説",
                            "importance": "small", "score": 5, "source_idxs": [0]}])
    assert items[0].detail == "長い詳細解説"


def test_curation_helpers_smoke():
    instr = curation_instructions("AI")
    assert "JSON" in instr and "detail" in instr  # detail 指示を含む
    s = format_tweets_for_curation(make_tweets(3))
    assert "[0]" in s and "[2]" in s
