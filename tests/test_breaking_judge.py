"""速報のLLM判定層(parse_verdicts / judge_breaking / 否認ストア)の単体テスト。

ネットワーク・実claude起動は使わない(subprocess はモック)。
monitor_breaking のロード方法は test_newsfeeds.py と同じ(importlib でスクリプトを読む)。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from xnewsbot import newsfeeds as nf

_MB_PATH = Path(__file__).resolve().parent.parent / "scripts" / "monitor_breaking.py"
_spec = importlib.util.spec_from_file_location("monitor_breaking_judge", _MB_PATH)
mb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mb)

NOW = datetime(2026, 7, 2, 5, 0, tzinfo=timezone.utc)


def _picked(*titles):
    """(genre, FeedItem, key) の列を作るヘルパ。"""
    out = []
    for t in titles:
        item = nf.FeedItem(title=t, url=f"http://x/{t}", source="s",
                           published=NOW - timedelta(minutes=5), origin="google_news")
        out.append(("株", item, mb.norm_key(t)))
    return out


def test_parse_verdicts_splits_accepted_and_rejected():
    picked = _picked("日銀が緊急利上げを決定", "個別株のレーティング増額")
    data = {"verdicts": [
        {"idx": 0, "major": True, "reason": "政策決定"},
        {"idx": 1, "major": False, "reason": "個別銘柄"},
    ]}
    accepted, rejected = mb.parse_verdicts(picked, data)
    assert [g_i_k_r[1].title for g_i_k_r in accepted] == ["日銀が緊急利上げを決定"]
    assert [g_i_k_r[1].title for g_i_k_r in rejected] == ["個別株のレーティング増額"]
    assert rejected[0][3] == "個別銘柄"  # reason が保持される


def test_parse_verdicts_missing_idx_falls_to_rejected():
    picked = _picked("A見出し", "B見出し")
    data = {"verdicts": [{"idx": 0, "major": True, "reason": "重大"}]}
    accepted, rejected = mb.parse_verdicts(picked, data)
    assert len(accepted) == 1 and len(rejected) == 1
    assert rejected[0][3] == "判定なし"  # verdict の無い idx は安全側で見送り


def test_parse_verdicts_fail_closed_on_broken_shapes():
    picked = _picked("A見出し")
    assert mb.parse_verdicts(picked, {}) is None                                  # verdicts 欠落
    assert mb.parse_verdicts(picked, {"verdicts": "x"}) is None                   # list でない
    assert mb.parse_verdicts(picked, {"verdicts": [{"idx": 5, "major": True}]}) is None   # idx 範囲外
    assert mb.parse_verdicts(picked, {"verdicts": [{"idx": 0, "major": "yes"}]}) is None  # major 非bool
    assert mb.parse_verdicts(picked, {"verdicts": [{"idx": True, "major": True}]}) is None  # bool idx


_VERDICT_JSON = ('{"verdicts":[{"idx":0,"major":true,"reason":"災害"},'
                 '{"idx":1,"major":false,"reason":"軽微"}]}')


def _rule_path(cmd, tool):
    """--allowedTools の `Tool(/<abs>)` から絶対パスを取り出す(`//abs` 形式 = 絶対パス指定)。"""
    rules = cmd[cmd.index("--allowedTools") + 1:]
    hits = [r for r in rules if r.startswith(f"{tool}(/") and r.endswith(")")]
    assert len(hits) == 1, rules
    return hits[0][len(tool) + 2:-1]


def _isolate_tmp(monkeypatch, tmp_path):
    """mkdtemp の置き場を pytest の tmp_path にして本番の /tmp を汚さない。"""
    monkeypatch.setattr(mb.tempfile, "tempdir", str(tmp_path))


def test_judge_breaking_fail_closed_when_no_verdict_file(monkeypatch, tmp_path):
    _isolate_tmp(monkeypatch, tmp_path)
    picked = _picked("A見出し")

    def fake_run(cmd, timeout=None, capture_output=None, cwd=None):  # claude が verdict を書かず終わる
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    assert mb.judge_breaking(picked, mb.get_settings()) is None


def test_judge_breaking_parses_verdict_written_by_claude(monkeypatch, tmp_path):
    _isolate_tmp(monkeypatch, tmp_path)
    picked = _picked("大地震が発生", "軽い話題")
    seen = {}

    def fake_run(cmd, timeout=None, capture_output=None, cwd=None):  # claude の代わりに verdict を書く
        seen["cmd"], seen["cwd"] = cmd, cwd
        Path(_rule_path(cmd, "Edit")).write_text(_VERDICT_JSON, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    accepted, rejected = mb.judge_breaking(picked, mb.get_settings())
    assert [x[1].title for x in accepted] == ["大地震が発生"]
    assert [x[1].title for x in rejected] == ["軽い話題"]

    cmd, cwd = seen["cmd"], seen["cwd"]
    # 権限を絞るフラグ(外部テキストのプロンプトインジェクション対策)が揃っていること
    for flag, value in [("--tools", "Read,Write"), ("--permission-mode", "dontAsk"),
                        ("--setting-sources", "")]:
        assert cmd[cmd.index(flag) + 1] == value
    for flag in ["--strict-mcp-config", "--no-session-persistence", "--safe-mode", "--restricted"]:
        assert flag in cmd
    # cwd は実行ごとの一時ディレクトリ(固定の /tmp パスではない)で、実行後に消えている
    assert cwd is not None and cwd.startswith(os.path.realpath(str(tmp_path)) + os.sep)
    assert Path(cwd).name.startswith("xnews_breaking_")
    assert not Path(cwd).exists()
    # 許可は cands の Read と verdict の Edit だけで、どちらも cwd 内の絶対パス
    rules = cmd[cmd.index("--allowedTools") + 1:]
    assert len(rules) == 2
    cands, verdict = _rule_path(cmd, "Read"), _rule_path(cmd, "Edit")
    for path in (cands, verdict):
        assert os.path.isabs(path) and os.path.dirname(path) == cwd
    assert cands != verdict
    prompt = cmd[cmd.index("-p") + 1]
    assert cands in prompt and verdict in prompt


def test_judge_breaking_fail_closed_on_nonzero_exit(monkeypatch, tmp_path):
    _isolate_tmp(monkeypatch, tmp_path)
    picked = _picked("大地震が発生", "軽い話題")

    def fake_run(cmd, timeout=None, capture_output=None, cwd=None):  # verdict は書けたが異常終了
        Path(_rule_path(cmd, "Edit")).write_text(_VERDICT_JSON, encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(mb.subprocess, "run", fake_run)
    assert mb.judge_breaking(picked, mb.get_settings()) is None
    assert list(tmp_path.iterdir()) == []  # 一時ディレクトリは失敗時も片付く


def test_seen_keys_includes_rejected(tmp_path):
    db = str(tmp_path / "t.db")
    con = mb._store(db)
    picked = _picked("送った速報", "見送った話題")
    g0, i0, k0 = picked[0]
    g1, i1, k1 = picked[1]
    mb._record(con, k0, i0, g0, NOW)
    mb._record_rejected(con, k1, i1, g1, "個別銘柄", NOW)
    seen = mb._seen_keys(con, NOW - timedelta(hours=48))
    assert k0 in seen and k1 in seen  # 既送も否認済みも再選定されない
    con.close()
