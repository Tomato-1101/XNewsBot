"""pipeline の split(キュレーションの並列分割)と merge(組ごとの curated の結合)。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

# scripts/pipeline.py を import(パッケージ外なのでパス指定でロード)
_PL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("pipeline", _PL_PATH)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


def _raw(genres: dict) -> dict:
    return {"date": "2026-10-02", "tz": "Asia/Tokyo", "slot": "morning",
            "market": [{"key": "N225", "close": 1}],
            "schedule": [{"time_label": "21:30", "name": "米 雇用統計"}],
            "indicator_results": [{"name": "米 ADP", "result": "9万人"}],
            "recent_titles": {g: [f"{g}の前日見出し"] for g in genres},
            "genres": genres}


def _write_raw(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "xnews_morning_raw.json"
    p.write_text(pl._dump_raw(raw), encoding="utf-8")
    return p


def test_split_raw_groups_and_unknown_goes_to_smaller():
    genres = {"特大": [{"text": "x" * 10}], "AI": [{"text": "a" * 5000}],
              "暗号資産": [{"text": "s"}], "話題": [], "新ジャンル": [{"text": "n"}]}
    parts = pl.split_raw(_raw(genres))
    assert [list(p["genres"]) for p in parts] == [["特大", "AI"], ["暗号資産", "話題", "新ジャンル"]]
    # recent_titles はその組のジャンルだけ。共通キーはそのまま
    assert list(parts[1]["recent_titles"]) == ["暗号資産", "話題", "新ジャンル"]
    for p in parts:
        for k in ("date", "tz", "slot", "market", "schedule", "indicator_results"):
            assert p[k] == _raw(genres)[k]


def test_split_raw_skips_empty_group():
    parts = pl.split_raw(_raw({"暗号資産": [{"text": "s"}], "話題": []}))
    assert len(parts) == 1 and list(parts[0]["genres"]) == ["暗号資産", "話題"]


def test_cmd_split_small_raw_is_not_split(tmp_path, capsys):
    p = _write_raw(tmp_path, _raw({"AI": [{"text": "a"}], "株": [{"text": "s"}]}))
    pl.cmd_split(SimpleNamespace(raw=str(p)))
    assert capsys.readouterr().out.splitlines() == [str(p)]
    assert sorted(x.name for x in tmp_path.iterdir()) == [p.name]


def test_cmd_split_large_raw_keeps_candidates_and_i(tmp_path, capsys):
    big = "本" * 2000  # 1候補 約6KB
    genres = {g: [{"text": f"{g}{j}", "body": big} for j in range(10)]
              for g in ("特大", "AI", "株", "暗号資産", "テクノロジー", "話題")}
    raw = _raw(genres)
    p = _write_raw(tmp_path, raw)
    assert p.stat().st_size >= pl.SPLIT_MIN_BYTES
    pl.cmd_split(SimpleNamespace(raw=str(p)))
    paths = capsys.readouterr().out.splitlines()
    assert paths == [str(tmp_path / "xnews_morning_raw.p0.json"),
                     str(tmp_path / "xnews_morning_raw.p1.json")]
    whole = json.loads(p.read_text(encoding="utf-8"))
    p0, p1 = (json.loads(Path(x).read_text(encoding="utf-8")) for x in paths)
    assert list(p0["genres"]) == ["特大", "AI", "株"]
    assert list(p1["genres"]) == ["暗号資産", "テクノロジー", "話題"]
    for part in (p0, p1):
        for g, cands in part["genres"].items():
            assert cands == whole["genres"][g]  # 候補も `i` も元の raw と同じ
            assert [c["i"] for c in cands] == list(range(10))
        assert list(part["recent_titles"]) == list(part["genres"])
        assert part["market"] == raw["market"] and part["schedule"] == raw["schedule"]


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_cmd_merge_combines_genres(tmp_path):
    a = _write(tmp_path, "a.json", json.dumps({"genres": {"AI": [{"title": "x"}]}}))
    b = _write(tmp_path, "b.json", json.dumps({"genres": {"株": [], "話題": [{"title": "y"}]}}))
    out = tmp_path / "cur.json"
    pl.cmd_merge(SimpleNamespace(out=str(out), parts=[a, b]))
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "genres": {"AI": [{"title": "x"}], "株": [], "話題": [{"title": "y"}]}}


@pytest.mark.parametrize("second", [
    None,                                   # 無い
    "",                                     # 空
    "{not json",                            # JSON でない
    json.dumps({"genres": ["AI"]}),         # genres が dict でない
    json.dumps({"genres": {"AI": []}}),     # ジャンルが重複
    json.dumps({"genres": {"株": None}}),   # ジャンルの値が配列でない
    json.dumps({"genres": {"株": {"title": "y"}}}),  # 配列でなく dict
    json.dumps({"genres": {"株": ["y"]}}),  # 要素が dict でない
])
def test_cmd_merge_fails(tmp_path, second):
    a = _write(tmp_path, "a.json", json.dumps({"genres": {"AI": []}}))
    b = str(tmp_path / "b.json") if second is None else _write(tmp_path, "b.json", second)
    out = tmp_path / "cur.json"
    with pytest.raises(SystemExit) as e:
        pl.cmd_merge(SimpleNamespace(out=str(out), parts=[a, b]))
    assert e.value.code not in (0, None)
    assert not out.exists()


# --- ingest: 1ジャンルでも壊れていたら DB に一切書かない ---

def _ingest_env(monkeypatch, tmp_path, session, curated: dict):
    import contextlib
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"date": "2026-10-02", "slot": "morning", "market": [],
                               "genres": {"AI": [{"text": "a"}], "株": [{"text": "b"}]}}),
                   encoding="utf-8")
    cur = tmp_path / "cur.json"
    cur.write_text(json.dumps(curated), encoding="utf-8")

    @contextlib.contextmanager
    def fake_session():
        try:
            yield session
        finally:
            session.rollback()  # 実際の Session も閉じるときに未確定分を捨てる

    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "get_session", fake_session)
    return SimpleNamespace(raw=str(raw), curated=str(cur), date=None, slot=None)


def _titles(session) -> dict:
    from datetime import date
    from xnewsbot import digest
    out = {}
    for g in ("AI", "株"):
        d = digest.get_genre_digest(session, g, date(2026, 10, 2), "morning")
        out[g] = [it.title for it in digest.items_of_digest(session, d.id)] if d else None
    return out


def _seed(session):
    from datetime import date
    from xnewsbot import digest
    from xnewsbot.curator import CuratedItem
    for g in ("AI", "株"):
        digest.ingest_curated(session, g, date(2026, 10, 2), "morning",
                              [CuratedItem(title=f"既存{g}", summary="s", detail="d",
                                           importance="small", score=1, source_idxs=[0])],
                              [{"text": "t"}])


def test_ingest_broken_genre_changes_nothing(monkeypatch, tmp_path, session):
    _seed(session)
    args = _ingest_env(monkeypatch, tmp_path, session,
                       {"genres": {"AI": [{"title": "新AI", "source_idxs": [0]}], "株": None}})
    with pytest.raises(ValueError):
        pl.cmd_ingest(args)
    assert _titles(session) == {"AI": ["既存AI"], "株": ["既存株"]}


def test_ingest_failure_while_writing_rolls_back_all(monkeypatch, tmp_path, session):
    _seed(session)
    args = _ingest_env(monkeypatch, tmp_path, session,
                       {"genres": {"AI": [{"title": "新AI", "source_idxs": [0]}],
                                   "株": [{"title": "新株", "source_idxs": [0]}]}})
    real = pl.digest._build_news_items

    def build(digest_id, genre, curated, tweets):
        if genre == "株":
            raise RuntimeError("書き込み中の失敗")
        return real(digest_id, genre, curated, tweets)
    monkeypatch.setattr(pl.digest, "_build_news_items", build)
    with pytest.raises(RuntimeError):
        pl.cmd_ingest(args)
    assert _titles(session) == {"AI": ["既存AI"], "株": ["既存株"]}


def test_ingest_ok_and_empty_genre_keeps_existing(monkeypatch, tmp_path, session):
    _seed(session)
    args = _ingest_env(monkeypatch, tmp_path, session,
                       {"genres": {"AI": [{"title": "新AI", "source_idxs": [0]}], "株": []}})
    pl.cmd_ingest(args)
    assert _titles(session) == {"AI": ["新AI"], "株": ["既存株"]}  # 空の株は既存を上書きしない
