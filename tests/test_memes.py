"""ミームの調査(xnewsbot/memes.py)。claude・X は呼ばない(差し替え)。"""

from xnewsbot import memes, trends, xclient

OUT = '''調べた結果です。
```json
[{"name": "ドパガキ", "region": "日本", "origin": "ドーパミン中毒の子どもを指す語", "how": "自虐に使う",
  "scale": "流行語として記事化", "x_terms": ["ドパガキ", "x"], "sources": ["https://a/1", "not-url"]},
 {"name": "Huss Huss Huss", "name_original": "Huss Huss Huss", "region": "海外", "x_terms": ["Huss Huss Huss"]},
 {"region": "日本"}]
```'''


def test_parse_reads_array_inside_text_and_cleans_fields():
    got = memes._parse(OUT)
    assert [m["name"] for m in got] == ["ドパガキ", "Huss Huss Huss"]       # name の無いものは捨てる
    assert got[0]["name_original"] == "ドパガキ"                            # 無ければ name
    assert got[0]["x_terms"] == ["ドパガキ"]                                # 1文字の語は捨てる
    assert got[0]["sources"] == ["https://a/1"]                             # URL でないものは捨てる


def test_parse_no_array_is_empty():
    assert memes._parse("見つかりませんでした") == []


def test_x_evidence_query_and_top_post(monkeypatch):
    seen = {}

    def fake(q, qtype, n, keys, retries=3):
        seen["q"], seen["qtype"] = q, qtype
        return [{"text": "RT @a: x", "likeCount": 999, "url": "https://x/rt"},
                {"text": "小さい", "likeCount": 10, "url": "https://x/1"},
                {"text": "大きい  投稿", "likeCount": 500, "url": "https://x/2"}]
    monkeypatch.setattr(xclient, "fetch_with_retry", fake)
    ev = memes.x_evidence({"name": "H", "x_terms": ["Huss Huss Huss", "#huss"]}, ["k"])
    assert seen["q"] == '("Huss Huss Huss" OR #huss) within_time:72h -filter:replies'
    assert seen["qtype"] == "Top"
    assert ev["x_posts"] == 2                                            # RT は数えない
    assert ev["x_max_likes"] == 500 and ev["x_top_url"] == "https://x/2"
    assert ev["x_samples"][0] == {"text": "大きい 投稿", "likes": 500, "url": "https://x/2"}


def test_x_evidence_failure_is_empty(monkeypatch):
    def boom(*a, **k):
        raise xclient.XClientError("timeout")
    monkeypatch.setattr(xclient, "fetch_with_retry", boom)
    assert memes.x_evidence({"name": "H", "x_terms": ["abc"]}, ["k"]) == {"x_posts": 0}


def test_candidates_use_top_post_url_or_source(monkeypatch):
    monkeypatch.setattr(memes, "search_list", lambda: memes._parse(OUT))
    monkeypatch.setattr(xclient, "load_keys", lambda: ["k"])
    monkeypatch.setattr(memes, "x_evidence", lambda m, keys: (
        {"x_posts": 3, "x_max_likes": 800, "x_top_url": "https://x/top", "x_samples": []}
        if m["name"] == "Huss Huss Huss" else {"x_posts": 0}))
    c = memes.candidates(24)
    assert [x["url"] for x in c] == ["https://a/1", "https://x/top"]
    assert c[0]["text"].startswith("ドパガキ（日本）") and c[0]["media"] == "ウェブ"
    assert c[1]["likeCount"] == 800 and c[1]["media"] == "X"
    assert c[1]["trend"]["source"] == "meme_search" and c[1]["trend"]["x_posts"] == 3
    assert "x_top_url" not in c[1]["trend"]


def test_registered_as_pinned_source_and_failure_is_empty(monkeypatch):
    assert "meme_search" in trends.SOURCES and "meme_search" in trends.PINNED

    def boom():
        raise RuntimeError("claude down")
    monkeypatch.setattr(memes, "search_list", boom)
    assert trends.SOURCES["meme_search"](24) == []


def test_search_list_runs_each_region_and_survives_one_failure(monkeypatch):
    prompts = []

    def fake(prompt, timeout):
        prompts.append(prompt)
        if "「海外」" in prompt:
            raise RuntimeError("timeout")
        return '[{"name": "ドパガキ", "region": "日本"}, {"name": "ドパガキ", "region": "日本"}]'
    monkeypatch.setattr(memes.explain, "run_claude", fake)
    got = memes.search_list()
    assert [m["name"] for m in got] == ["ドパガキ"]                  # 重複は1件・失敗した地域は空
    assert len(prompts) == 2 and all("__REGION__" not in p and "__TODAY__" not in p for p in prompts)
