"""配信ジャンルの定義とXの検索キーワード群。

定義は config/genres.toml に外出し(ユーザーが編集する一次ファイル)。
本モジュールは起動時に1度読み込み、辞書として公開する。
collect 時は 日本語(keywords, lang:ja)・英語(keywords_en, lang:en)・公式(accounts, from:)の
最大3クエリになり、exclude 語・exclude_accounts は除外する。feeds は直取り RSS(pipeline collect)。
x_queries はキーワードを使わない生クエリ(話題ジャンルの「いいねが極端に多い日本語の投稿」用)、
trend_sources は急上昇ワード・はてブ等の話題の取得元(xnewsbot.trends)、news_max はニュース候補の上限。
watch=true のジャンルは上の収集を使わず、管理画面で登録した監視アカウントの全投稿を集める(xnewsbot.watch)。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

GENRES_FILE = Path(__file__).resolve().parent.parent / "config" / "genres.toml"


def _feed(genre: str, f: dict) -> dict:
    """feeds の1要素を {url, name, filter} に正規化する(url 必須。name 省略時は url)。"""
    url = str(f.get("url") or "").strip() if isinstance(f, dict) else ""
    if not url:
        raise ValueError(f"genres.toml: {genre} の feeds に url の無い要素があります: {f}")
    return {"url": url, "name": str(f.get("name") or url), "filter": bool(f.get("filter", False))}


def _xq(genre: str, q: dict) -> dict:
    """x_queries の1要素を {query, max, min_faves} に正規化する(query 必須)。"""
    query = str(q.get("query") or "").strip() if isinstance(q, dict) else ""
    if not query:
        raise ValueError(f"genres.toml: {genre} の x_queries に query の無い要素があります: {q}")
    mf = q.get("min_faves")
    return {"query": query, "max": int(q.get("max", 40)), "min_faves": int(mf) if mf is not None else None}


def _load() -> dict[str, dict]:
    with GENRES_FILE.open("rb") as f:
        data = tomllib.load(f)
    out: dict[str, dict] = {}
    for g in data.get("genre", []):
        key = g.get("key")
        if not key:
            raise ValueError(f"genres.toml: key の無い [[genre]] があります: {g}")
        if key in out:
            raise ValueError(f"genres.toml: key が重複しています: {key}")
        out[key] = {
            "label": g.get("label", key),
            "keywords": list(g.get("keywords", [])),
            "exclude": list(g.get("exclude", [])),
            "min_faves": g.get("min_faves"),  # None なら設定の既定値を使う
            "min_faves_en": g.get("min_faves_en"),  # None なら max(min_faves, 英語の既定下限)
            "keywords_en": list(g.get("keywords_en", [])),
            "accounts": [str(a).lstrip("@") for a in g.get("accounts", [])],
            "exclude_accounts": [str(a).lstrip("@") for a in g.get("exclude_accounts", [])],
            "feeds": [_feed(key, f) for f in g.get("feeds", [])],
            "x_queries": [_xq(key, q) for q in g.get("x_queries", [])],
            "trend_sources": [str(t) for t in g.get("trend_sources", [])],
            "news_max": g.get("news_max"),  # None なら pipeline の既定(NEWS_PER_GENRE)
            "selectable": bool(g.get("selectable", True)),  # false=常時ジャンル(全員に常時配信)
            "watch": bool(g.get("watch", False)),  # true=監視アカウントの全投稿を集めるジャンル
            # 廃止(2026-10-01): 英語は keywords_en の別クエリで集める。互換のため読み込みだけ残す。
            "lang": str(g.get("lang", "ja")).strip().lower(),
            "note": g.get("note", ""),
        }
    if not out:
        raise ValueError(f"genres.toml にジャンルが1つもありません: {GENRES_FILE}")
    return out


# 表示順は genres.toml の記載順(dict は挿入順を保持)
GENRES: dict[str, dict] = _load()
GENRE_KEYS: list[str] = list(GENRES.keys())
# オンボーディングで選べるジャンル(selectable=true)
SELECTABLE_KEYS: list[str] = [k for k in GENRE_KEYS if GENRES[k]["selectable"]]
# 全員に常時配信する常時ジャンル(selectable=false。2026-10-02 に特大を廃止して現在は空)
ALWAYS_KEYS: list[str] = [k for k in GENRE_KEYS if not GENRES[k]["selectable"]]
# 監視アカウントの全投稿を集めるジャンル(watch=true)
WATCH_KEYS: list[str] = [k for k in GENRE_KEYS if GENRES[k]["watch"]]


def is_valid_genre(name: str) -> bool:
    return name in GENRES


def is_always(genre: str) -> bool:
    return genre in ALWAYS_KEYS


def is_watch(genre: str) -> bool:
    return genre in WATCH_KEYS


def display_genres(enabled: list[str]) -> list[str]:
    """配信に出すジャンルを表示順で返す: 常時ジャンル + 購読ジャンル(重複排除)。"""
    chosen = set(enabled) | set(ALWAYS_KEYS)
    return [g for g in GENRE_KEYS if g in chosen]


def keywords(genre: str) -> list[str]:
    return GENRES[genre]["keywords"]


def excludes(genre: str) -> list[str]:
    return GENRES[genre].get("exclude", [])


def min_faves(genre: str) -> int | None:
    """ジャンル別の最低いいね数(未指定なら None → 設定の既定値を使う)。"""
    v = GENRES[genre].get("min_faves")
    return int(v) if v is not None else None


def lang(genre: str) -> str:
    """収集の言語フィルタ。"ja"=日本語のみ。"any"/"" なら言語指定なし(英語等も拾う)。"""
    return GENRES[genre].get("lang", "ja")


def keywords_en(genre: str) -> list[str]:
    """英語の X 検索キーワード(空なら英語クエリは投げない)。"""
    return GENRES[genre].get("keywords_en", [])


def accounts(genre: str) -> list[str]:
    """公式・一次情報の X ハンドル(@ なし)。"""
    return GENRES[genre].get("accounts", [])


def exclude_accounts(genre: str) -> list[str]:
    """候補から外す使い回し・煽り系アカウント(@ なし)。"""
    return GENRES[genre].get("exclude_accounts", [])


def feeds(genre: str) -> list[dict]:
    """直取り RSS の [{url, name, filter}]。"""
    return GENRES[genre].get("feeds", [])


def min_faves_en(genre: str) -> int | None:
    """英語クエリの最低いいね数(未指定なら None → 呼び出し側の既定)。"""
    v = GENRES[genre].get("min_faves_en")
    return int(v) if v is not None else None


def x_queries(genre: str) -> list[dict]:
    """キーワードを使わない X の生クエリ [{query, max, min_faves}](無ければ空)。"""
    return GENRES[genre].get("x_queries", [])


def trend_sources(genre: str) -> list[str]:
    """話題の取得元の名前(xnewsbot.trends.SOURCES のキー)。"""
    return GENRES[genre].get("trend_sources", [])


def news_max(genre: str) -> int | None:
    """ニュース候補の上限(未指定なら None → pipeline の既定)。"""
    v = GENRES[genre].get("news_max")
    return int(v) if v is not None else None
