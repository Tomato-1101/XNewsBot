"""配信ジャンルの定義とXの検索キーワード群。

定義は config/genres.toml に外出し(ユーザーが編集する一次ファイル)。
本モジュールは起動時に1度読み込み、辞書として公開する。
collect 時に "(kw1 OR kw2 ...) lang:ja" の OR 検索になり、exclude 語は除外する。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

GENRES_FILE = Path(__file__).resolve().parent.parent / "config" / "genres.toml"


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
            "note": g.get("note", ""),
        }
    if not out:
        raise ValueError(f"genres.toml にジャンルが1つもありません: {GENRES_FILE}")
    return out


# 表示順は genres.toml の記載順(dict は挿入順を保持)
GENRES: dict[str, dict] = _load()
GENRE_KEYS: list[str] = list(GENRES.keys())


def is_valid_genre(name: str) -> bool:
    return name in GENRES


def keywords(genre: str) -> list[str]:
    return GENRES[genre]["keywords"]


def excludes(genre: str) -> list[str]:
    return GENRES[genre].get("exclude", [])


def min_faves(genre: str) -> int | None:
    """ジャンル別の最低いいね数(未指定なら None → 設定の既定値を使う)。"""
    v = GENRES[genre].get("min_faves")
    return int(v) if v is not None else None
