"""日本の決算(注目決算・決算サプライズ)を Mac で取ってクラウドに渡す経路のテスト。

schedule.fetch_jp / fetch(jp=...)、d1sync の状態ファイル、pipeline の jp_schedule.json 読み込み、
scripts/push_jp_schedule.py。ネットワーク・wrangler は呼ばない。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from xnewsbot import schedule as sc

ROOT = Path(__file__).parents[1]
J = sc.JST
NOW = datetime(2026, 10, 1, 7, 15, tzinfo=J)   # 木曜。前営業日は 9/30
D = NOW.date()


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _notable() -> list[dict]:
    return [
        sc._jp_earnings_item(D, {"code": "7545", "name": "西松屋チェーン", "hm": (15, 0), "cap": 1200},
                             label="前回15:00", notable=True, session="after", star=True),
        sc._jp_earnings_item(D, {"code": "3549", "name": "クスリのアオキ'HD", "cap": 3000},
                             notable=True, session="unknown"),
        sc._item(sc._jst(D, 10, 30), "ir", "JP", "平和堂（8276）説明会", 3, day=D, notable=True,
                 session="intraday"),
    ]


def _surprises() -> list[dict]:
    return [sc._surprise("JP", "ナガイレーベン（7447）", -6.5, "PTS", "上期経常を下方修正")]


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    # 他の取得元はすべて失敗(空)にして、日本の決算だけを比べる
    monkeypatch.setattr(sc, "_get", lambda url: None)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: None)


def _live(monkeypatch, calls: list[str]):
    def notable(d):
        calls.append("notable")
        return _notable()

    def surprise(d, p):
        calls.append("surprise")
        return _surprises()

    monkeypatch.setattr(sc, "_jp_notable", notable)
    monkeypatch.setattr(sc, "_jp_surprise_job", surprise)


def _forbid_live(monkeypatch):
    def boom(*a):
        raise AssertionError("live で取ってはいけない")
    monkeypatch.setattr(sc, "_jp_notable", boom)
    monkeypatch.setattr(sc, "_jp_surprise_job", boom)


# --- schedule.fetch(jp=...) ---

def test_fetch_uses_same_day_jp_without_live(monkeypatch, capsys):
    _forbid_live(monkeypatch)
    jp = {"date": D.isoformat(), "fetched_at": "2026-10-01T07:00:00+09:00",
          "notable": sc._jp_dump(_notable()), "surprises": sc._jp_dump(_surprises())}
    got = sc.fetch(NOW, jp=jp)
    assert [x["name"] for x in got["schedule"]] == [
        "平和堂（8276）説明会", "西松屋チェーン（7545）決算", "クスリのアオキ'HD（3549）決算"]
    assert [x["name"] for x in got["surprises"]] == ["ナガイレーベン（7447）"]
    assert "Mac で取得した分を使用 (2026-10-01T07:00:00+09:00)" in capsys.readouterr().err


def test_fetch_other_day_or_none_items_go_live(monkeypatch):
    calls: list[str] = []
    _live(monkeypatch, calls)
    old = {"date": "2026-09-30", "notable": [], "surprises": []}
    sc.fetch(NOW, jp=old)
    assert sorted(calls) == ["notable", "surprise"]      # 日付違いは両方 live

    calls.clear()
    sc.fetch(NOW, jp={"date": D.isoformat(), "notable": None, "surprises": []})
    assert calls == ["notable"]                          # None の方だけ live

    calls.clear()
    sc.fetch(NOW)
    assert sorted(calls) == ["notable", "surprise"]      # jp 無しは今まで通り


def test_fetch_jp_json_roundtrip_matches_live(monkeypatch):
    calls: list[str] = []
    _live(monkeypatch, calls)
    live = sc.fetch(NOW)
    jp = json.loads(json.dumps(sc.fetch_jp(NOW), ensure_ascii=False))
    assert jp["date"] == D.isoformat() and jp["fetched_at"]
    _forbid_live(monkeypatch)
    assert sc.fetch(NOW, jp=jp) == live
    assert live["schedule"] and live["surprises"]


def test_fetch_jp_failure_is_none(monkeypatch, capsys):
    def boom(*a):
        raise sc._FetchError("403")
    monkeypatch.setattr(sc, "_jp_notable", boom)
    monkeypatch.setattr(sc, "_jp_surprise_job", lambda d, p: [])
    got = sc.fetch_jp(NOW)
    assert got["notable"] is None and got["surprises"] == []
    assert "予定: 株探・IRBANK(日本の注目決算) 取得失敗のため省略 (_FetchError: 403)" in capsys.readouterr().err


# --- d1sync ---

def test_d1sync_state_files_has_jp_schedule(tmp_path):
    d1 = _load("d1sync")
    assert d1.state_files(tmp_path / "x.db")["jp_schedule"] == tmp_path.resolve() / "jp_schedule.json"


# --- pipeline collect ---

def _collect(monkeypatch, tmp_path, jp_text: str | None) -> list:
    pl = _load("pipeline")
    db = tmp_path / "xnewsbot.db"
    if jp_text is not None:
        (tmp_path / "jp_schedule.json").write_text(jp_text, encoding="utf-8")
    seen: list = []

    def fake_fetch(now=None, jp=None):
        seen.append(jp)
        return {"schedule": [], "results": [], "surprises": []}

    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace(default_tz="Asia/Tokyo", db_path=str(db)))
    monkeypatch.setattr(pl.xclient, "load_keys", lambda settings: ["k"])
    monkeypatch.setattr(pl.xclient, "collect", lambda g, settings=None, keys=None: [])
    monkeypatch.setattr(pl, "_newsfeed_candidates", lambda g, settings: [
        {"source": "news", "text": "見出し", "url": "https://n/1"}])
    monkeypatch.setattr(pl.articles, "enrich_bodies", lambda cands: None)
    monkeypatch.setattr(pl.market, "fetch_market", lambda: [])
    monkeypatch.setattr(pl.schedule, "fetch", fake_fetch)
    monkeypatch.setattr(pl, "_recent_titles", lambda genres, day: {g: [] for g in genres})
    out = tmp_path / "raw.json"
    pl.cmd_collect(SimpleNamespace(user=None, due=False, genres="株", slot="morning", out=str(out)))
    assert json.loads(out.read_text(encoding="utf-8"))["genres"]["株"][0]["text"] == "見出し"
    return seen


def test_pipeline_passes_jp_schedule(monkeypatch, tmp_path):
    jp = {"date": "2026-10-01", "fetched_at": "x", "notable": [], "surprises": None}
    assert _collect(monkeypatch, tmp_path, json.dumps(jp)) == [jp]


def test_pipeline_missing_or_broken_jp_schedule(monkeypatch, tmp_path, capsys):
    assert _collect(monkeypatch, tmp_path, None) == [None]
    assert _collect(monkeypatch, tmp_path, "{壊れた") == [None]
    assert "jp_schedule.json を読めず live で取得" in capsys.readouterr().err


# --- scripts/push_jp_schedule.py ---

def test_push_both_none_does_not_call_wrangler(monkeypatch):
    pj = _load("push_jp_schedule")
    monkeypatch.setattr(pj.schedule, "fetch_jp",
                        lambda: {"date": "2026-10-01", "fetched_at": "x", "notable": None, "surprises": None})
    monkeypatch.setattr(pj.d1sync.subprocess, "run", lambda *a, **k: pytest.fail("wrangler を呼んだ"))
    assert pj.main() == 1


def test_push_writes_escaped_sql_via_wrangler(monkeypatch):
    pj = _load("push_jp_schedule")
    data = {"date": "2026-10-01", "fetched_at": "2026-10-01T07:00:00+09:00",
            "notable": sc._jp_dump(_notable()), "surprises": None}
    monkeypatch.setattr(pj.schedule, "fetch_jp", lambda: data)
    got: dict = {}

    def run(cmd, **kw):
        got["cmd"], got["cwd"] = cmd, kw.get("cwd")
        got["sql"] = Path(cmd[cmd.index("--file") + 1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(pj.d1sync.subprocess, "run", run)
    assert pj.main() == 0
    cmd = got["cmd"]
    assert cmd[1:4] == ["d1", "execute", pj.d1sync.DB_NAME] and "--remote" in cmd and "--file" in cmd
    assert got["cwd"] == ROOT / "cloud" / "worker"
    assert "クスリのアオキ''HD" in got["sql"]            # ' は '' に
    # 実際に SQLite で流して、kv に元の JSON がそのまま入ることを確かめる
    con = sqlite3.connect(":memory:")
    con.execute(pj.d1sync.KV_DDL)
    con.executescript(got["sql"])
    key, value, updated = con.execute("SELECT key, value, updated_at FROM kv").fetchone()
    assert key == "jp_schedule" and json.loads(value) == data
    assert len(updated) == 20 and updated.endswith("Z")
