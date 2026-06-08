from __future__ import annotations

import json

from xnewsbot.curator import MAX_BIG_PER_GENRE, Curator, _extract_json_array

from .conftest import make_tweets


def test_extract_json_array_with_code_fence():
    raw = '```json\n[{"title":"a","importance":"small","score":1,"source_idxs":[0]}]\n```'
    out = _extract_json_array(raw)
    assert out[0]["title"] == "a"


def test_curate_orders_big_first_and_caps():
    # big を上限+2 件返すフェイク → 上限まで big、残りは small に降格
    def complete(system, user):
        items = [{"title": f"b{i}", "summary": "s", "importance": "big",
                  "score": 100 - i, "source_idxs": [i]} for i in range(MAX_BIG_PER_GENRE + 2)]
        return json.dumps(items, ensure_ascii=False)

    cur = Curator(complete=complete)
    items = cur.curate("AI", make_tweets(8))
    bigs = [i for i in items if i.importance == "big"]
    assert len(bigs) == MAX_BIG_PER_GENRE
    # 先頭は big、かつ score 降順
    assert items[0].importance == "big"
    assert items[0].score >= items[1].score


def test_curate_falls_back_on_bad_json():
    def complete(system, user):
        return "これはJSONではありません"

    cur = Curator(complete=complete)
    items = cur.curate("株", make_tweets(3))
    assert items  # 機械的フォールバックで空にならない
    assert items[0].importance == "big"


def test_curate_empty_tweets_returns_empty():
    cur = Curator(complete=lambda s, u: "[]")
    assert cur.curate("AI", []) == []
