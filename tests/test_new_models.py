"""新モデル公開の取得元(trends.openrouter_models / hf_models)と、AI ジャンルの genres.toml の単体テスト。
ネットワークは使わない(小さな JSON 断片を差し込む)。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from xnewsbot import articles, genres, trends


@pytest.fixture(autouse=True)
def _isolate_hf(tmp_path, monkeypatch):
    """HF 収集が本番の seen_models.json を触らず、README を取りに外へ出ないようにする。"""
    monkeypatch.setattr(trends, "_seen_path", lambda: str(tmp_path / "seen_models.json"))
    monkeypatch.setattr(trends, "_get_text", lambda url, timeout, max_bytes: "")


def _now() -> float:
    return time.time()


def _or_model(mid: str, hours_ago: float, **kw) -> dict:
    return {"id": mid, "name": kw.get("name", mid), "created": int(_now() - hours_ago * 3600),
            "description": kw.get("description", "説明 <b>です</b>"),
            "context_length": kw.get("ctx", 131072),
            "architecture": {"output_modalities": kw.get("out", ["text"])}}


def _hf(mid: str, hours_ago: float, **kw) -> dict:
    created = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    row = {"id": mid, "createdAt": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
           "likes": kw.get("likes", 0), "downloads": kw.get("downloads", 0)}
    if "tag" in kw:
        row["pipeline_tag"] = kw["tag"]
    if "score" in kw:
        row["trendingScore"] = kw["score"]
    return row


# --- OpenRouter ---

def test_openrouter_window_and_derived_variants():
    data = {"data": [
        _or_model("acme/new-1", 3, out=["text", "image"]),
        _or_model("acme/old", 100),                                   # 時間窓の外
        _or_model("acme/new-1:free", 3),                              # 本体が一覧にある派生 → 落とす
        _or_model("acme/old:batch", 2),                               # 本体(old)は古いが一覧にある派生 → 落とす
        _or_model("solo/only-free:free", 1, name="Solo: Only (free)"),  # 本体が無い無料版だけの新モデル → 残す
        _or_model("zed/newer", 0.5, out=["video"], ctx=0),
        {"id": "no/created", "name": "x"},                            # created 欠落は無視
    ]}
    rows = trends.parse_openrouter(data, 48)
    assert [r["id"] for r in rows] == ["zed/newer", "solo/only-free", "acme/new-1"]   # 新しい順
    assert rows[1]["name"] == "Solo: Only"
    assert rows[2]["output"] == ["text", "image"]
    assert rows[2]["description"] == "説明 です"                       # HTML は除去


def test_openrouter_models_candidates(monkeypatch):
    data = {"data": [_or_model("acme/new-1", 3, name="Acme: New 1", out=["text", "image"]),
                     _or_model("acme/old", 400)]}
    urls = []
    monkeypatch.setattr(trends, "_get_json", lambda url, timeout: urls.append((url, timeout)) or data)
    out = trends.openrouter_models(24)          # 収集窓が24hでも最低48h遡る
    assert urls == [(trends.OPENROUTER_URL, trends.OPENROUTER_TIMEOUT)]
    assert len(out) == 1
    c = out[0]
    assert c["text"] == "Acme: New 1" and c["url"] == "https://openrouter.ai/acme/new-1"
    assert c["media"] == "acme" and c["source"] == "news"
    assert "出力: text・image" in c["summary"] and "131,072" in c["summary"]
    assert c["trend"] == {"source": "openrouter_models", "id": "acme/new-1",
                          "output": ["text", "image"], "context_length": 131072}


def test_openrouter_window_floor_and_override(monkeypatch):
    data = {"data": [_or_model("a/b", 60)]}
    monkeypatch.setattr(trends, "_get_json", lambda url, timeout: data)
    assert trends.openrouter_models(24) == []          # 60h前 > 48h
    assert len(trends.openrouter_models(72)) == 1      # 収集窓が広ければそれに従う


def test_openrouter_failure_is_empty_via_safe(monkeypatch):
    def boom(url, timeout):
        raise TimeoutError("slow")
    monkeypatch.setattr(trends, "_get_json", boom)
    assert trends.SOURCES["openrouter_models"](24) == []


# --- Hugging Face ---

@pytest.mark.parametrize("mid,derived", [
    ("unsloth/Qwen3-8B", True), ("bartowski/x-7B", True), ("mlx-community/x", True),
    ("lmstudio-community/x", True), ("TheBloke/x", True),
    ("Qwen/Qwen3-8B-GGUF", True), ("acme/model-AWQ", True), ("acme/model-GPTQ-Int4", True),
    ("acme/model-MLX-4bit", True), ("acme/model-8bit", True), ("acme/model_fp8", True),
    ("acme/style-lora", True),
    ("Qwen/Qwen3-8B", False), ("acme/Kimi-K3", False), ("acme/bitnet-2B", False),
])
def test_is_derived_hf(mid, derived):
    assert trends._is_derived_hf(mid) is derived


def test_parse_hf_models_window_derived_and_score():
    rows = [_hf("Qwen/new", 5, likes=7, downloads=100, tag="text-generation"),
            _hf("Qwen/old", 200),
            _hf("Qwen/new-GGUF", 1),
            _hf("unsloth/new", 1),
            {"id": "bad/date", "createdAt": "???"}]
    got = trends.parse_hf_models(rows, 48)
    assert [r["id"] for r in got] == ["Qwen/new"]
    assert got[0]["pipeline_tag"] == "text-generation" and got[0]["likes"] == 7
    # 急上昇: 72h・スコア下限
    trend = [_hf("a/hot", 60, score=50), _hf("a/cold", 60, score=19), _hf("a/stale", 100, score=900),
             _hf("a/hot-AWQ", 10, score=900)]
    assert [r["id"] for r in trends.parse_hf_models(trend, 72, 20)] == ["a/hot"]
    assert trends.parse_hf_models({"error": "x"}, 48) == []          # 想定外の形は空


def _hf_server(monkeypatch, per_org: dict[str, list], trending: list, fail: set[str] = frozenset()):
    calls = []

    def fake(url, timeout):
        calls.append((url, timeout))
        if "sort=trendingScore" in url:
            return trending
        org = url.split("author=")[1].split("&")[0]
        if org in fail:
            raise ConnectionError("x")
        return per_org.get(org, [])
    monkeypatch.setattr(trends, "_get_json", fake)
    return calls


def test_hf_models_merges_orgs_and_trending_with_dedup(monkeypatch):
    calls = _hf_server(
        monkeypatch,
        per_org={"Qwen": [_hf("Qwen/Qwen4", 10, likes=5, tag="text-generation"), _hf("Qwen/Qwen4-GGUF", 10),
                          _hf("Qwen/ancient", 500)],
                 "google": [_hf("google/gem", 30, likes=9)]},
        trending=[_hf("Qwen/Qwen4", 10, likes=5, score=80, tag="text-generation"),     # 組織ウォッチと重複
                  _hf("indie/wow", 70, likes=40, downloads=999, score=60, tag="image-to-video"),
                  _hf("indie/low", 70, score=5),                                      # スコア不足
                  _hf("unsloth/x", 5, score=500)])                                    # 再配布
    out = trends.hf_models(24)
    ids = [c["trend"]["id"] for c in out]
    assert sorted(ids) == ["Qwen/Qwen4", "google/gem", "indie/wow"]       # 重複は1件
    assert ids == ["indie/wow", "google/gem", "Qwen/Qwen4"]               # いいね数の多い順
    q = next(c for c in out if c["trend"]["id"] == "Qwen/Qwen4")
    assert q["trend"]["trending"] == 80                                   # 急上昇の値がある方を残す
    assert q["url"] == "https://huggingface.co/Qwen/Qwen4" and q["media"] == "Hugging Face / Qwen"
    w = next(c for c in out if c["trend"]["id"] == "indie/wow")
    assert "image-to-video" in w["summary"] and "999" in w["summary"] and "40" in w["summary"]
    # 全組織+急上昇を1回ずつ・上限時間つきで引く
    assert len(calls) == 1 + len(trends.HF_ORGS) and all(t == trends.HF_TIMEOUT for _, t in calls)
    assert "expand[]=trendingScore" in next(u for u, _ in calls if "sort=trendingScore" in u)


def test_hf_models_survives_partial_failure_and_raises_when_all_fail(monkeypatch):
    _hf_server(monkeypatch, {"Qwen": [_hf("Qwen/ok", 3)]}, [], fail={"google", "openai"})
    assert [c["trend"]["id"] for c in trends.hf_models(24)] == ["Qwen/ok"]

    def boom(url, timeout):
        raise TimeoutError("down")
    monkeypatch.setattr(trends, "_get_json", boom)
    with pytest.raises(ConnectionError):
        trends.hf_models(24)
    assert trends.SOURCES["hf_models"](24) == []            # _safe 経由なら空で配信続行


def test_hf_orgs_cover_requested_list():
    assert len(trends.HF_ORGS) == len(set(trends.HF_ORGS)) == 43
    assert {"Qwen", "SakanaAI", "CohereLabs", "llm-jp", "black-forest-labs"} <= set(trends.HF_ORGS)


# --- genres.toml(AI ジャンル) ---

def test_ai_genre_new_model_sources_normalized():
    ai = genres.GENRES["AI"]
    assert genres.trend_sources("AI") == ["openrouter_models", "hf_models"]
    assert all(n in trends.SOURCES for n in genres.trend_sources("AI"))
    assert genres.news_max("AI") == 100
    xq = genres.x_queries("AI")
    assert len(xq) == 2
    for q in xq:
        assert q["max"] == 20 and q["min_faves"] is None            # いいね下限なし
        assert len(q["query"]) <= 450
        assert "-filter:replies" in q["query"] and "-filter:retweets" in q["query"]
    assert "from:MicrosoftAI" in xq[0]["query"] and "from:ArtificialAnlys" in xq[1]["query"]
    handles = [h for q in xq for h in __import__("re").findall(r"from:(\w+)", q["query"])]
    assert len(handles) == len({h.lower() for h in handles}) == 29
    # 既存の公式アカウントには足していない(大手に枠を奪われるため別クエリ。X のハンドルは大小文字を区別しない)
    assert not {h.lower() for h in handles} & {a.lower() for a in ai["accounts"]}
    urls = [f["url"] for f in genres.feeds("AI")]
    assert len(urls) == len(set(urls)) == 29
    flt = {f["name"] for f in genres.feeds("AI") if f["filter"]}
    assert {"Microsoft Research", "Microsoft Foundry", "AWS ML", "NVIDIA Dev", "Pandaily"} <= flt
    assert "Mistral" not in flt and "量子位" not in flt
    assert "r/LocalLLaMA" in flt          # 総合の人気投稿なので AI の語を含むものだけ


def test_merge_news_pinned_sources_go_first_in_full():
    from tests.test_sources import pl
    pinned = [{"text": f"model {i}", "url": f"https://openrouter.ai/m{i}"} for i in range(5)]
    other = [{"text": f"news {i}", "url": f"https://ex.com/{i}"} for i in range(5)]
    out = pl.merge_news([pinned, other], limit=6, pinned=1)
    assert [c["text"] for c in out] == [f"model {i}" for i in range(5)] + ["news 0"]


def test_mark_watched_makes_listed_accounts_official(monkeypatch):
    from tests.test_sources import pl
    monkeypatch.setattr(pl, "x_queries", lambda g: [{"query": "(from:MistralAI OR from:elevenlabs) -filter:replies"}])
    tw = [{"author": {"userName": "mistralai"}}, {"author": {"userName": "someone"}}]
    pl._mark_watched("AI", tw)
    assert tw[0].get("_official") is True and tw[0].get("_watched") is True
    assert not tw[1].get("_official") and not tw[1].get("_watched")


def test_mark_watched_ignores_negated_from_and_keeps_accounts_official(monkeypatch):
    from tests.test_sources import pl
    monkeypatch.setattr(pl, "x_queries", lambda g: [
        {"query": "(from:Good OR from:alsogood) -from:spam -filter:replies"}])
    tw = [{"author": {"userName": "spam"}}, {"author": {"userName": "ALSOGOOD"}},
          {"author": {"userName": "good"}, "_official": True}]    # accounts 由来で既に公式
    pl._mark_watched("AI", tw)
    assert not tw[0].get("_official") and not tw[0].get("_watched")   # -from: は監視しない
    assert tw[1]["_official"] and tw[1]["_watched"]                   # 大小文字は無視
    assert tw[2]["_official"] and "_watched" not in tw[2]             # 監視枠(上限あり)に落とさない


def test_internal_flags_do_not_leak_into_raw():
    from tests.test_sources import pl
    out = pl._trim({"text": "x", "author": {"userName": "u"}, "_official": True, "_watched": True})
    assert out["official"] is True and not any(k.startswith("_") for k in out)


def test_cap_x_budget_split_official_watched_general():
    from tests.test_sources import pl
    off = [{"id": f"o{i}", "_official": True, "viewCount": 1} for i in range(3)]
    watched = [{"id": f"w{i}", "_official": True, "_watched": True, "viewCount": i} for i in range(20)]
    general = [{"id": f"g{i}", "viewCount": i} for i in range(100)]
    out = pl._cap_x(general + watched + off)
    ids = [t["id"] for t in out]
    # 公式3 + 監視は再生数の上位 X_WATCHED_MAX(15) + 一般は max(30, 50-3-15=32)=32 、順は 公式→監視→一般
    assert ids[:3] == ["o0", "o1", "o2"]
    assert ids[3:18] == [f"w{i}" for i in range(19, 4, -1)]
    assert ids[18:] == [f"g{i}" for i in range(99, 67, -1)] and len(ids) == 50


def test_cap_x_guarantees_min_general_when_official_fills_budget():
    from tests.test_sources import pl
    off = [{"id": f"o{i}", "_official": True, "viewCount": 1} for i in range(40)]
    watched = [{"id": f"w{i}", "_official": True, "_watched": True, "viewCount": i} for i in range(15)]
    general = [{"id": f"g{i}", "viewCount": i} for i in range(100)]
    out = pl._cap_x(off + watched + general)
    assert len(out) == 40 + 15 + pl.X_MIN_GENERAL
    assert sum(1 for t in out if t["id"].startswith("g")) == 30


def test_merge_news_pinned_total_is_capped():
    from tests.test_sources import pl
    a = [{"text": f"or {i}", "url": f"https://openrouter.ai/m{i}"} for i in range(40)]
    b = [{"text": f"hf {i}", "url": f"https://huggingface.co/m{i}"} for i in range(40)]
    other = [{"text": f"news {i}", "url": f"https://ex.com/{i}"} for i in range(40)]
    out = pl.merge_news([a, b, other], limit=100, pinned=2)
    texts = [c["text"] for c in out]
    assert sum(t.startswith(("or ", "hf ")) for t in texts) == pl.PINNED_MAX == 30
    assert sum(t.startswith("hf ") for t in texts) == 15            # 先頭の一覧が枠を使い切らない
    assert sum(t.startswith("news ") for t in texts) == 40          # 残りの枠は通常のラウンドロビン
    assert len(texts) == 70


def test_reddit_is_not_a_body_target():
    for host in ("www.reddit.com", "old.reddit.com", "reddit.com"):
        assert not articles.is_target({"source": "news", "url": f"https://{host}/r/LocalLLaMA/comments/x"})
    assert articles.is_target({"source": "news", "url": "https://notreddit.com/a"})


# --- HF の「初めて見た時刻」判定(seen_models.json) ---

def _seen_file(tmp_path):
    return tmp_path / "seen_models.json"


def _iso_ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hf_org(monkeypatch, rows: list[dict]):
    """Qwen 組織の一覧だけに rows を返す(他の組織・急上昇は空)。"""
    _hf_server(monkeypatch, per_org={"Qwen": rows}, trending=[])


def test_seen_first_run_uses_created_at_and_writes_state(monkeypatch, tmp_path):
    _hf_org(monkeypatch, [_hf("Qwen/fresh", 5, downloads=1), _hf("Qwen/old-viral", 100, downloads=50)])
    out = trends.hf_models(24)
    assert [c["trend"]["id"] for c in out] == ["Qwen/fresh"]        # 初回は createdAt が窓内のものだけ
    saved = json.loads(_seen_file(tmp_path).read_text())
    assert set(saved) == {"Qwen/fresh", "Qwen/old-viral"}            # 古いものも記録(次回に新規扱いしない)
    assert [p.name for p in tmp_path.iterdir()] == ["seen_models.json"]   # 一時ファイルが残らない


def test_seen_second_run_treats_newly_visible_old_repo_as_new(monkeypatch, tmp_path):
    _hf_org(monkeypatch, [_hf("Qwen/old-viral", 100, downloads=50)])
    assert trends.hf_models(24) == []                                # 初回: createdAt 基準で古い
    # 2回目: 非公開で作っておいたモデルが公開されて一覧に初登場(createdAt は10日前のまま)
    _hf_org(monkeypatch, [_hf("Qwen/old-viral", 100, downloads=50), _hf("Qwen/secret", 240, downloads=3, likes=2)])
    out = trends.hf_models(24)
    assert [c["trend"]["id"] for c in out] == ["Qwen/secret"]        # old-viral は初回記録のまま新規でない
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    s = out[0]["summary"]
    assert f"初出 {today}" in s and "リポジトリ作成" in s and "公開)" not in s
    # 3回目: 初出から窓(48h)内なら、記録済みでも引き続き候補(窓切れまでは毎日出る = 配信間隔24hの取りこぼし防止)
    assert [c["trend"]["id"] for c in trends.hf_models(24)] == ["Qwen/secret"]


def test_seen_window_expiry_org_and_trending(monkeypatch, tmp_path):
    _seen_file(tmp_path).write_text(json.dumps({
        "Qwen/org-in": _iso_ago(hours=40), "Qwen/org-out": _iso_ago(hours=60),
        "a/trend-in": _iso_ago(hours=70), "a/trend-out": _iso_ago(hours=80)}))
    _hf_server(monkeypatch, per_org={"Qwen": [_hf("Qwen/org-in", 200, downloads=1), _hf("Qwen/org-out", 200, downloads=1)]},
               trending=[_hf("a/trend-in", 200, downloads=1, score=50), _hf("a/trend-out", 200, downloads=1, score=50)])
    ids = sorted(c["trend"]["id"] for c in trends.hf_models(24))
    assert ids == ["Qwen/org-in", "a/trend-in"]       # createdAt は古くても first_seen 基準で窓を判定


def test_seen_prunes_records_older_than_keep_days(monkeypatch, tmp_path):
    _seen_file(tmp_path).write_text(json.dumps({
        "x/ancient": _iso_ago(days=trends.HF_SEEN_KEEP_D + 1), "x/kept": _iso_ago(days=trends.HF_SEEN_KEEP_D - 1)}))
    _hf_org(monkeypatch, [_hf("Qwen/new", 1, downloads=1)])
    trends.hf_models(24)
    assert set(json.loads(_seen_file(tmp_path).read_text())) == {"x/kept", "Qwen/new"}


def test_seen_loose_created_at_limit_and_padding_exclusion(monkeypatch, tmp_path):
    _seen_file(tmp_path).write_text("{}")                             # 状態あり(2回目以降)= 初見は今を初出とする
    _hf_org(monkeypatch, [
        _hf("Qwen/ancient", 24 * (trends.HF_SEEN_MAX_AGE_D + 1), downloads=9),   # 作成が緩い上限より古い
        _hf("Qwen/no-dl-old", 24 * (trends.HF_NO_DL_AGE_D + 1), downloads=0),    # 水増し: DL0のまま日が経った
        _hf("Qwen/no-dl-new", 24 * (trends.HF_NO_DL_AGE_D - 1), downloads=0),    # 作成直後はDL0でも出す
        _hf("Qwen/dl-old", 24 * (trends.HF_NO_DL_AGE_D + 1), downloads=1)])
    assert sorted(c["trend"]["id"] for c in trends.hf_models(24)) == ["Qwen/dl-old", "Qwen/no-dl-new"]


def test_seen_unreadable_state_is_reported_and_ignored(monkeypatch, tmp_path, capsys):
    _seen_file(tmp_path).write_text("{broken")
    _hf_org(monkeypatch, [_hf("Qwen/ok", 3, downloads=1)])
    assert [c["trend"]["id"] for c in trends.hf_models(24)] == ["Qwen/ok"]    # 収集は続く
    err = capsys.readouterr().err
    assert "seen_models.json を読めず" in err
    assert "Qwen/ok" in json.loads(_seen_file(tmp_path).read_text())          # 壊れた状態は作り直される


def test_seen_save_failure_does_not_stop_collection(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(trends, "_seen_path", lambda: str(tmp_path / "no-such-dir" / "seen_models.json"))
    _hf_org(monkeypatch, [_hf("Qwen/ok", 3, downloads=1)])
    assert [c["trend"]["id"] for c in trends.hf_models(24)] == ["Qwen/ok"]
    assert "保存できず" in capsys.readouterr().err


# --- HF の説明文(README) ---

README = """---
license: apache-2.0
tags:
- text-generation
---
<div align="center"><img src="logo.png"></div>

# Qwen4-8B  [![Chat](https://img.shields.io/badge/Chat-blue)](https://chat.example.com)

<!-- 内部メモ -->
## Introduction
Qwen4 is a **new** generation of [language models](https://example.com/lm) with `reasoning`.
![chart](chart.png)

| a | b |
|---|---|
> Note: see https://example.com for details.
```python
print("code")
```
"""


def test_clean_readme_strips_markup():
    got = trends.clean_readme(README)
    assert got == ("Qwen4-8B Introduction Qwen4 is a new generation of language models with reasoning. "
                   "a b Note: see for details.")
    for bad in ("license", "---", "<", "http", "#", "**", "`", "print(", "内部メモ", "![", "]("):
        assert bad not in got
    assert len(trends.clean_readme("あ" * 1000)) == trends.HF_README_CHARS
    assert trends.clean_readme("---\nonly: front\n") == "" and trends.clean_readme(None) == ""


def test_hf_models_adds_readme_excerpt_as_body(monkeypatch):
    _hf_org(monkeypatch, [_hf("Qwen/with-doc", 3, downloads=2, likes=5), _hf("Qwen/no-doc", 2, downloads=1)])
    urls = []

    def fake(url, timeout, max_bytes):
        urls.append(url)
        if "no-doc" in url:
            raise ConnectionError("404")
        return README
    monkeypatch.setattr(trends, "_get_text", fake)
    out = {c["trend"]["id"]: c for c in trends.hf_models(24)}
    assert out["Qwen/with-doc"]["body"].startswith("Qwen4-8B Introduction")
    assert out["Qwen/no-doc"]["body"] == ""                        # 失敗は説明なしで続行
    assert sorted(urls) == ["https://huggingface.co/Qwen/no-doc/raw/main/README.md",
                            "https://huggingface.co/Qwen/with-doc/raw/main/README.md"]


def test_readme_fetch_gives_up_after_budget(monkeypatch):
    _hf_org(monkeypatch, [_hf("Qwen/slow", 3, downloads=1)])
    monkeypatch.setattr(trends, "HF_README_BUDGET_S", 0.2)
    monkeypatch.setattr(trends, "_get_text", lambda url, timeout, max_bytes: time.sleep(1.5) or README)
    t0 = time.monotonic()
    out = trends.hf_models(24)
    assert time.monotonic() - t0 < 1.0                             # 遅い取得を待たない
    assert [c["body"] for c in out] == [""]


def test_fetch_all_uses_daemon_threads_and_keeps_order(monkeypatch):
    import threading
    kinds = []

    def fn(x):
        kinds.append(threading.current_thread().daemon)
        if x == 2:
            raise ValueError("bad")
        return x * 10
    res = trends._fetch_all(fn, [1, 2, 3], workers=2, budget_s=5)
    assert res[0] == ("ok", 10) and res[2] == ("ok", 30)
    assert res[1][0] == "err" and isinstance(res[1][1], ValueError)
    assert kinds and all(kinds)                                    # 終了時に join されない daemon
    assert trends._fetch_all(fn, [], workers=2, budget_s=1) == []
