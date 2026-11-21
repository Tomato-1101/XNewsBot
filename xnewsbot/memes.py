"""ミームのジャンル用: いま流行っているミームの一覧を作り、ミームごとに X で裏付けを取って候補にする(2026-11-21 追加)。

直近24時間のバズ投稿だけでは「ドパガキ」「ちいかわ」のように何日も続く流行りを拾えなかった(本人の指摘)。そこで
1. claude(WebSearch/WebFetch だけ許可)に、日本と海外でいま流行っているミームを調べさせて一覧(JSON)にする
   (ops/meme_search_prompt.md。流行語大賞・SHIBUYA109 lab.・Know Your Meme なども見る)。
2. ミームごとに x_terms で twitterapi.io を検索し(直近72時間・人気順)、投稿数・いいねの最大・上位の投稿を添える。
候補の `trend` に {"source": "meme_search", ...} を入れる。url は X の一番いいねの多い投稿(無ければ根拠の記事)。
失敗しても空を返すだけ(配信は止めない)。
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from . import explain, xclient

PROMPT_PATH = Path(__file__).resolve().parent.parent / "ops" / "meme_search_prompt.md"
TIMEOUT = 420          # 秒。ウェブ検索を何度もするので解説(300秒)より長め。収集全体の上限は600秒
MAX_MEMES = 15         # 1地域あたり
REGIONS = ("日本", "海外")
X_HOURS = 72           # 何日も続く流行りなので直近3日の投稿を見る
X_MAX = 20             # 1ミームあたりの取得数(人気順の上位)
X_SAMPLES = 3
SAMPLE_CHARS = 140


def _parse(out: str) -> list[dict]:
    """claude の出力から JSON 配列を取り出す(前後に文やコードブロックの印が付いても読む)。"""
    m = re.search(r"\[.*\]", out, re.S)
    if not m:
        return []
    data = json.loads(m.group(0))
    memes = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not str(d.get("name") or "").strip():
            continue
        memes.append({
            "name": str(d["name"]).strip(),
            "name_original": str(d.get("name_original") or d["name"]).strip(),
            "region": str(d.get("region") or "").strip(),
            "origin": str(d.get("origin") or "").strip(),
            "how": str(d.get("how") or "").strip(),
            "scale": str(d.get("scale") or "").strip(),
            "x_terms": [str(t).strip() for t in d.get("x_terms") or [] if len(str(t).strip()) >= 2][:4],
            "sources": [str(u) for u in d.get("sources") or [] if str(u).startswith("http")][:3],
        })
    return memes[:MAX_MEMES]


def _search_region(region: str, today: date) -> list[dict]:
    prompt = (PROMPT_PATH.read_text(encoding="utf-8")
              .replace("__TODAY__", today.isoformat()).replace("__REGION__", region))
    try:
        return _parse(explain.run_claude(prompt, timeout=TIMEOUT))
    except Exception as e:  # 片方が失敗しても、もう片方の地域だけで続ける
        print(f"  ミーム: {region}の調査に失敗 ({type(e).__name__}: {e})", file=sys.stderr)
        return []


def search_list(today: date | None = None) -> list[dict]:
    """日本と海外を別々の claude で並列に調べる(1回で両方を頼むと検索が浅く、それぞれ数件しか出なかった)。"""
    with ThreadPoolExecutor(max_workers=len(REGIONS)) as pool:
        lists = list(pool.map(lambda r: _search_region(r, today or date.today()), REGIONS))
    seen, out = set(), []
    for m in (m for lst in lists for m in lst):
        if m["name"] not in seen:
            seen.add(m["name"])
            out.append(m)
    return out


def x_evidence(meme: dict, keys: list[str]) -> dict:
    """x_terms で直近の人気投稿を引き、投稿数・いいねの最大・上位の投稿を返す。失敗は空の裏付け。"""
    if not meme["x_terms"]:
        return {"x_posts": 0}
    q = "(" + " OR ".join(xclient._term(t) for t in meme["x_terms"]) + ")"
    q += xclient._window_clause(X_HOURS) + " -filter:replies"
    try:
        tweets = xclient.fetch_with_retry(q, "Top", X_MAX, keys, retries=2)
    except xclient.XClientError as e:
        print(f"  ミーム: X 検索失敗 {meme['name']} ({type(e).__name__})", file=sys.stderr)
        return {"x_posts": 0}
    tweets = [t for t in tweets if not xclient._is_rt(t)]
    tweets.sort(key=lambda t: t.get("likeCount") or 0, reverse=True)
    return {
        "x_posts": len(tweets),
        "x_max_likes": (tweets[0].get("likeCount") or 0) if tweets else 0,
        "x_top_url": tweets[0].get("url", "") if tweets else "",
        "x_samples": [{"text": " ".join((t.get("text") or "").split())[:SAMPLE_CHARS],
                       "likes": t.get("likeCount") or 0, "url": t.get("url", "")}
                      for t in tweets[:X_SAMPLES]],
    }


def candidates(hours: float | None = None) -> list[dict]:
    memes = search_list()
    print(f"  ミーム: 流行っているミームの一覧 {len(memes)} 件", file=sys.stderr)
    if not memes:
        return []
    try:
        keys = xclient.load_keys()
    except Exception as e:  # 鍵が無くても一覧だけで続ける
        print(f"  ミーム: X の鍵を読めず裏付けなしで続行 ({type(e).__name__})", file=sys.stderr)
        keys = []
    if keys:
        with ThreadPoolExecutor(max_workers=4) as pool:
            evid = list(pool.map(lambda m: x_evidence(m, keys), memes))
    else:
        evid = [{"x_posts": 0} for _ in memes]
    out = []
    for m, ev in zip(memes, evid):
        url = ev.get("x_top_url") or (m["sources"][0] if m["sources"] else "")
        out.append({
            "text": f"{m['name']}（{m['region']}）: {m['origin']}",
            "viewCount": 0,
            "likeCount": ev.get("x_max_likes", 0),
            "url": url,
            "createdAt": "",
            "author": {"userName": "ミーム調査", "followers": 0},
            "source": "news",
            "summary": " ".join(x for x in (m["how"], m["scale"]) if x),
            "media": "X" if ev.get("x_top_url") else "ウェブ",
            "body": "",
            "trend": {"source": "meme_search", "name": m["name"], "name_original": m["name_original"],
                      "region": m["region"], "sources": m["sources"], **{k: v for k, v in ev.items()
                                                                       if k != "x_top_url"}},
        })
    return out
