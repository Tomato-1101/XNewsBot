"""公開しない設定(XNEWS_PRIVATE_DIR)と、競合ジャンル用の項目の読み込み。"""
from __future__ import annotations

from datetime import date

import pytest

from xnewsbot import genres


def _private(tmp_path, monkeypatch, toml: str | None = None, context: str | None = None):
    if toml is not None:
        (tmp_path / "genres.toml").write_text(toml, encoding="utf-8")
    if context is not None:
        (tmp_path / "aixone.md").write_text(context, encoding="utf-8")
    monkeypatch.setenv(genres.PRIVATE_DIR_ENV, str(tmp_path))


def test_public_genre_fields():
    g = genres.GENRES["競合"]
    assert g["private"] and g["dedupe_days"] == 60 and g["weekly_recap"] == "mon"
    assert genres.PRIVATE_KEYS == ["競合"] and genres.is_private("競合") and not genres.is_private("AI")
    assert genres.empty_note("競合") == "新しい動きなし" and genres.empty_note("AI") == ""
    assert genres.dedupe_days("AI") is None and genres.dedupe_days("無いジャンル") is None
    assert genres.is_recap_day("競合", date(2026, 11, 23))        # 月曜
    assert not genres.is_recap_day("競合", date(2026, 11, 24))
    assert not genres.is_recap_day("AI", date(2026, 11, 23))


def test_no_private_dir_adds_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(genres.PRIVATE_DIR_ENV, raising=False)
    assert genres._load() == genres.GENRES
    assert genres._load()["競合"]["keywords"] == []
    # フォルダはあるが genres.toml が無い場合も何も足さない
    _private(tmp_path, monkeypatch)
    assert genres._load() == genres.GENRES
    # context_file が読めなければ1行出して空
    assert genres.context_text("競合") == ""
    assert capsys.readouterr().err.strip().startswith("競合: context_file を読めず参考情報なしで続行")
    assert genres.context_text("AI") == ""      # 指定の無いジャンルは何も出さない
    assert capsys.readouterr().err == ""


def test_overlay_appends_lists_and_overwrites_scalars(monkeypatch, tmp_path):
    base = genres._load()["AI"]
    _private(tmp_path, monkeypatch, """
[[genre]]
key = "競合"
keywords = ["会社A", "会社B"]
accounts = ["rival_a"]
min_faves = 5

[[genre]]
key = "AI"
keywords = ["足した語"]
""", context="事業の説明\n")
    loaded = genres._load()
    assert loaded["競合"]["keywords"] == ["会社A", "会社B"] and loaded["競合"]["accounts"] == ["rival_a"]
    assert loaded["競合"]["min_faves"] == 5
    assert loaded["競合"]["private"] and loaded["競合"]["dedupe_days"] == 60      # 公開側の値は残る
    assert loaded["AI"]["keywords"] == base["keywords"] + ["足した語"]           # リストは後ろに足す
    assert loaded["AI"]["min_faves"] == base["min_faves"]
    assert genres.context_text("競合") == "事業の説明\n"


def test_overlay_unknown_key_is_error(monkeypatch, tmp_path):
    _private(tmp_path, monkeypatch, '[[genre]]\nkey = "公開側に無い"\nkeywords = ["x"]\n')
    with pytest.raises(ValueError, match="公開側に無い"):
        genres._load()
