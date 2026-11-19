"""GitHub Actions で動かすための部品(render_mock / run_explain / deliver.sh の両 OS 対応)のテスト。

claude・LINE・D1 は呼ばない(生成はフェイク、送信は FakeMessenger、DB はインメモリか tmp)。
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import sqlite3
import subprocess
import time
from datetime import date
from pathlib import Path

import pytest

from xnewsbot import explain, mockdata
from xnewsbot import line_client as lc
from xnewsbot.models import GenreDigest, NewsExplanation, NewsItem

from .conftest import FakeMessenger

ROOT = Path(__file__).parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


render_mock = _load("render_mock")
run_explain = _load("run_explain")


# ---------------------------------------------------------------- render_mock

def test_render_mock_is_line_api_messages():
    msgs = render_mock.render()
    assert 2 <= len(msgs) <= lc.MAX_MESSAGES                 # 1回の reply に収まる
    assert msgs[0] == {"type": "text", "text": mockdata.WARNING}
    for m in msgs[1:]:
        # LINE の API にそのまま渡せる形(内部の spec の alt ではなく altText)
        assert m["type"] == "flex" and m["altText"] and "alt" not in m
        assert m["contents"]["type"] in ("bubble", "carousel")
    json.dumps(msgs)                                          # JSON にできる(SDK のオブジェクトが残っていない)


def test_render_mock_writes_only_kv(tmp_path):
    db = tmp_path / "x.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE newsitem (id INTEGER PRIMARY KEY, title TEXT)")   # 本物の表の代わり
    con.execute("CREATE TABLE kv (key VARCHAR NOT NULL PRIMARY KEY, value TEXT NOT NULL, updated_at VARCHAR NOT NULL)")
    con.execute("INSERT INTO kv VALUES ('watch_state', '{}', 't'), ('mock_specs', '古い', 't')")
    con.commit()
    msgs = render_mock.render()
    render_mock.save(db, msgs)
    assert con.execute("SELECT count(*) FROM newsitem").fetchone()[0] == 0   # モックの記事を本物の表に混ぜない
    rows = dict(con.execute("SELECT key, value FROM kv"))
    assert rows["watch_state"] == "{}" and json.loads(rows["mock_specs"]) == msgs
    con.close()


# ---------------------------------------------------------------- run_explain

GOOD = "【何が起きたか】\n" + "解説の本文。" * 60


def _factory(session):
    @contextlib.contextmanager
    def f():
        yield session
    return f


def _claimed(session, push_to="U1", status="running") -> NewsExplanation:
    gd = GenreDigest(digest_date=date(2026, 11, 19), slot="morning", genre="AI")
    session.add(gd)
    session.commit()
    it = NewsItem(genre_digest_id=gd.id, genre="AI", genres=["AI"], importance="big", rank=0,
                  title="AIの新モデル発表", summary="要約", detail="詳細", source_urls=[], source_tweets=[])
    session.add(it)
    session.commit()
    # Worker が確保した行(D1 から pull してきた状態)
    row = NewsExplanation(news_item_id=it.id, status=status, claim_token="tok", title=it.title,
                          push_to=push_to, push_cost=1 if push_to else 0)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def test_run_explain_saves_result_and_pushes(session, monkeypatch):
    row = _claimed(session)
    monkeypatch.setattr(explain, "generate", lambda payload: GOOD)
    m = FakeMessenger()
    assert run_explain.run(row.id, messenger=m, session_factory=_factory(session)) == 0
    session.refresh(row)
    assert row.status == "done" and row.text == GOOD and row.claim_token == "tok"
    assert m.pushes == [("U1", [lc.text_spec(explain.line_text("AIの新モデル発表", GOOD))])]


def test_run_explain_failure_is_recorded_and_notified(session, monkeypatch):
    row = _claimed(session)

    def boom(payload):
        raise explain.ExplainError("時間切れ")
    monkeypatch.setattr(explain, "generate", boom)
    m = FakeMessenger()
    # 失敗も結果として D1 に戻すので、終了コードは 0(後続の push を「失敗した実行」にしない)
    assert run_explain.run(row.id, messenger=m, session_factory=_factory(session)) == 0
    session.refresh(row)
    assert row.status == "failed" and row.error == "時間切れ"
    assert len(m.pushes) == 1 and "作れませんでした" in m.pushes[0][1][0]["text"]


def test_run_explain_without_destination_does_not_push(session, monkeypatch):
    row = _claimed(session, push_to="")
    monkeypatch.setattr(explain, "generate", lambda payload: GOOD)
    monkeypatch.setattr(lc, "LineMessenger", lambda token: pytest.fail("LINE を使わない"))
    assert run_explain.run(row.id, session_factory=_factory(session)) == 0
    session.refresh(row)
    assert row.status == "done"


def test_run_explain_skips_rows_that_are_not_running(session, monkeypatch):
    row = _claimed(session, status="done")
    monkeypatch.setattr(explain, "generate", lambda payload: pytest.fail("作り直さない"))
    m = FakeMessenger()
    assert run_explain.run(row.id, messenger=m, session_factory=_factory(session)) == 0
    assert m.pushes == []
    assert run_explain.run(row.id + 99, messenger=m, session_factory=_factory(session)) == 1


def test_claude_child_gets_ci_token_but_no_line_secrets(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "t")
    monkeypatch.setenv("LINE_CHANNEL_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "secret")
    env = explain.claude_env()
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    assert "LINE_CHANNEL_ACCESS_TOKEN" not in env and "CLOUDFLARE_API_TOKEN" not in env


# ---------------------------------------------------------------- ops/deliver.sh と cloud/actions

DELIVER = (ROOT / "ops/deliver.sh").read_text()


def test_deliver_paths_default_to_mac_and_can_be_overridden():
    for line in ('PROJ="${XNEWS_PROJ:-/Users/tomato/Project/XNewsBot}"',
                 'PY="${XNEWS_PY:-$PROJ/.venv/bin/python}"',
                 'CLAUDE="${XNEWS_CLAUDE_BIN:-/Users/tomato/.local/bin/claude}"',
                 'LOG="${XNEWS_LOG:-$HOME/Library/Logs/xnewsbot-deliver.log}"',
                 'LOCK_DIR="${XNEWS_LOCK_DIR:-/tmp/xnewsbot-deliver.lock}"'):
        assert line in DELIVER


def _function(name: str) -> str:
    start = DELIVER.index(f"{name}() {{")
    return DELIVER[start:DELIVER.index("\n}\n", start) + 3]


def test_hhmm_epoch_matches_local_time():
    """BSD(macOS)でも GNU(Linux)でも、今日の HHMM の epoch 秒が同じ値になる。"""
    out = subprocess.run(["bash", "-c", _function("hhmm_epoch") + 'hhmm_epoch "$1"', "_", "0800"],
                         capture_output=True, text=True, timeout=10)
    lt = time.localtime()
    want = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 8, 0, 0, 0, 0, -1)))
    assert out.returncode == 0 and int(out.stdout) == want


def test_lock_mtime_works_on_this_os(tmp_path):
    line = next(l for l in DELIVER.splitlines() if l.strip().startswith("mtime=$(stat "))
    out = subprocess.run(["bash", "-c", f'LOCK_DIR="$1"; {line.strip()}; echo "$mtime"', "_", str(tmp_path)],
                         capture_output=True, text=True, timeout=10)
    assert abs(int(out.stdout) - int(tmp_path.stat().st_mtime)) <= 1


def test_login_check_is_skipped_only_with_ci_token():
    assert 'if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] && ! (cd /tmp && "$CLAUDE" auth status' in DELIVER


def test_action_scripts_parse():
    scripts = [str(ROOT / "ops/deliver.sh"), *map(str, sorted((ROOT / "cloud/actions").glob("*.sh")))]
    assert len(scripts) >= 3
    for s in scripts:
        assert subprocess.run(["bash", "-n", s], capture_output=True).returncode == 0, s


@pytest.mark.parametrize("script,args", [("deliver.sh", ["bogus"]), ("deliver.sh", ["user"]),
                                         ("explain.sh", ["12;rm"]), ("explain.sh", [])])
def test_action_scripts_reject_bad_input_before_doing_anything(script, args, tmp_path):
    # 入力の検査は D1 や作業フォルダに触る前に行う(PATH を空にしても 2 で終わる=何も起動していない)
    r = subprocess.run(["/bin/bash", str(ROOT / "cloud/actions" / script), *args],
                       env={"PATH": "", "XNEWS_WORK": str(tmp_path / "w")}, capture_output=True, text=True, timeout=10)
    assert r.returncode == 2 and not (tmp_path / "w").exists()
