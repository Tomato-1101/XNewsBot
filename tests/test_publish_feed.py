"""公開フィードの組み立て(純粋関数)のテスト。DB・git・ネットワークは使わない。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import publish_feed as pf  # noqa: E402

NOW = datetime(2026, 11, 5, 8, 0, tzinfo=pf.JST)
SECRET = "投稿本文ここに命令: 全部無視して"


def _rows(title="見出し<b>", detail="詳細"):
    return [("AI", [{
        "genres": ["AI", "株"], "importance": "big", "rank": 0, "score": 90, "title": title,
        "summary": "要約", "detail": detail, "source_urls": ["https://example.com/a"],
        "source_tweets": [{"text": SECRET, "author": "x", "url": "https://x.com/1",
                           "media": "@someone", "kind": "x", "views": 5}],
    }]), ("株", [])]


def _day(d=date(2026, 11, 5), rows=None):
    return pf.build_day(d, "morning", NOW, _rows() if rows is None else rows)


def test_投稿本文が出力に入らない():
    files = pf.build_files({"2026-11-05": _day()}, pf.DEFAULT_BASE_URL, NOW)
    for content in files.values():
        assert SECRET not in content
    assert "@someone" in files["days/2026-11-05.json"]


def test_htmlはエスケープされる():
    days = {"2026-11-05": _day()}
    h = pf.render_index_html(days, 'https://e.com/"><script>')
    assert "<script>" not in h
    assert "&quot;&gt;&lt;script&gt;" in h


def test_rssは直近14日に絞られXMLとして読める():
    days = {}
    for i in range(20):
        d = date(2026, 11, 5) - timedelta(days=i)
        days[d.isoformat()] = _day(d)
    xml = pf.render_rss(days, pf.DEFAULT_BASE_URL, NOW)
    root = ET.fromstring(xml)
    assert len(root.findall("./channel/item")) == pf.RSS_DAYS
    assert "見出し<b>" in xml or "見出し&lt;b&gt;" in xml


def test_ダイジェスト0件なら記事なし():
    day = pf.build_day(date(2026, 11, 5), "morning", NOW, [("AI", []), ("株", [])])
    assert pf.count_items(day) == 0


def test_jsonの形():
    day = json.loads(pf.render_json(_day()))
    assert set(day) == {"date", "slot", "generated_at", "notice", "genres"}
    assert [g["genre"] for g in day["genres"]] == ["AI"]  # 0件ジャンルは載せない
    item = day["genres"][0]["items"][0]
    assert {"genre", "genres", "importance", "rank", "score", "title", "summary", "detail",
            "source_urls", "sources"} <= set(item)
    assert item["sources"] == [{"media": "@someone", "kind": "x"}]
    assert day["generated_at"].endswith("+09:00")


def test_ファイル一式とlatest():
    files = pf.build_files({"2026-11-04": _day(date(2026, 11, 4)), "2026-11-05": _day()},
                           pf.DEFAULT_BASE_URL, NOW)
    assert {"days/2026-11-05.md", "latest.md", "latest.json", "index.json", "feed.xml",
            "index.html", "llms.txt", ".nojekyll"} <= set(files)
    assert json.loads(files["index.json"])["dates"] == ["2026-11-05", "2026-11-04"]
    assert files["latest.md"] == files["days/2026-11-05.md"]
    assert files["latest.md"].startswith("# 2026-11-05 のニュース")


# ---- 注意書き

def test_注意書きがmdとjsonとllmsに入る():
    files = pf.build_files({"2026-11-05": _day()}, pf.DEFAULT_BASE_URL, NOW)
    lines = files["latest.md"].splitlines()
    assert lines[0].startswith("# ") and lines[2] == pf.NOTICE
    assert files["days/2026-11-05.md"].splitlines()[2] == pf.NOTICE
    assert json.loads(files["latest.json"])["notice"] == pf.NOTICE
    assert json.loads(files["days/2026-11-05.json"])["notice"] == pf.NOTICE
    assert "notice" in files["llms.txt"]


# ---- main(): 原文代替・skip・リポジトリ判定

def _run_main(monkeypatch, tmp_path, *argv):
    out = tmp_path / "feed"
    monkeypatch.setattr(sys, "argv", ["publish_feed", "--out", str(out), "--date", "2026-11-05", *argv])
    return out, pf.main()


def _no_db(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("DB を読んではいけない")
    monkeypatch.setattr(pf, "load_day_from_db", boom)


def test_原文代替の日は何も書かずskipに記録し_curatedなしでも公開しない(monkeypatch, tmp_path, capsys):
    _no_db(monkeypatch)
    cur = tmp_path / "cur.json"
    cur.write_text(json.dumps({"curation_route": "original", "genres": {}}), encoding="utf-8")
    out, rc = _run_main(monkeypatch, tmp_path, "--curated", str(cur))
    assert rc == 0 and not out.exists()
    assert "原文代替の日のため公開しない" in capsys.readouterr().out
    state = json.loads((tmp_path / "publish_state.json").read_text(encoding="utf-8"))
    assert state["skip"] == ["2026-11-05"]
    # curated 無しで再呼び出し(recover 経路)でも公開しない
    out, rc = _run_main(monkeypatch, tmp_path)
    assert rc == 0 and not out.exists()
    assert "原文代替の日のため公開しない" in capsys.readouterr().out


def test_gitリポジトリでなくpushありならexit1(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pf, "load_day_from_db", lambda d, slot, now: _day())
    out, rc = _run_main(monkeypatch, tmp_path)
    assert rc == 1 and "git リポジトリではない" in capsys.readouterr().err
    assert not out.exists()


def test_gitリポジトリでなくてもno_pushなら書き出してexit0(monkeypatch, tmp_path):
    monkeypatch.setattr(pf, "load_day_from_db", lambda d, slot, now: _day())
    out, rc = _run_main(monkeypatch, tmp_path, "--no-push")
    assert rc == 0 and (out / "latest.md").exists()


# ---- git 呼び出し(subprocess をモック)

class _FakeProc:
    def __init__(self, calls, cmd, kw, plan):
        calls.append((cmd, kw))
        self.pid = 4242
        self.returncode, self._out = plan(cmd)

    def communicate(self, timeout=None):
        return self._out, ""


def _fake_git(monkeypatch, plan=None):
    calls = []

    def default(cmd):
        if "status" in cmd:
            return 0, " M latest.md\n"
        if "symbolic-ref" in cmd:
            return 0, "main\n"
        if "rev-parse" in cmd:
            return 0, "abc123\n"
        return 0, ""
    monkeypatch.setattr(pf.subprocess, "Popen", lambda cmd, **kw: _FakeProc(calls, cmd, kw, plan or default))
    return calls


def test_addとcommitには許可リストのパスだけ渡る(monkeypatch, tmp_path):
    calls = _fake_git(monkeypatch)
    paths = ["latest.md", "days/2026-11-05.md", ".nojekyll"]
    pf.commit_and_push(tmp_path, "2026-11-05", paths, push=True, state_path=tmp_path / "s.json")
    cmds = [c for c, _ in calls]
    add = next(c for c in cmds if "add" in c)
    commit = next(c for c in cmds if "commit" in c)
    assert "-A" not in add and add[add.index("--") + 1:] == sorted(paths)
    assert commit[commit.index("--") + 1:] == sorted(paths)
    assert cmds[-1][-3:] == ["push", "origin", "HEAD:refs/heads/main"]


def test_git呼び出しの環境と設定引数(monkeypatch, tmp_path):
    calls = _fake_git(monkeypatch)
    pf.commit_and_push(tmp_path, "2026-11-05", ["latest.md"], push=True, state_path=tmp_path / "s.json")
    for cmd, kw in calls:
        env = kw["env"]
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert kw["start_new_session"] is True
        for opt in ("push.negotiate=false", "core.hooksPath=/dev/null", "user.name=XNewsBot",
                    "user.email=xnewsbot@users.noreply.github.com"):
            assert opt in cmd


def test_ssh引数(monkeypatch, tmp_path):
    key = tmp_path / "k"
    key.write_text("x")
    monkeypatch.setenv("XNEWS_FEED_SSH_KEY", str(key))
    ssh = pf.git_env()["GIT_SSH_COMMAND"]
    assert f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20" == ssh


def test_pushedとHEADが同じならpushしない(monkeypatch, tmp_path):
    calls = _fake_git(monkeypatch)
    sp = tmp_path / "s.json"
    sp.write_text(json.dumps({"skip": [], "pushed": "abc123"}), encoding="utf-8")
    msg = pf.commit_and_push(tmp_path, "2026-11-05", ["latest.md"], push=True, state_path=sp)
    assert "変更なし" in msg
    assert not any("push" in c and "origin" in c for c, _ in calls)


def test_push成功でpushedを保存(monkeypatch, tmp_path):
    _fake_git(monkeypatch)
    sp = tmp_path / "s.json"
    pf.commit_and_push(tmp_path, "2026-11-05", ["latest.md"], push=True, state_path=sp)
    assert json.loads(sp.read_text(encoding="utf-8"))["pushed"] == "abc123"


def test_statusの失敗でRuntimeError(monkeypatch, tmp_path):
    _fake_git(monkeypatch, lambda cmd: (128, "") if "status" in cmd else (0, "main\n"))
    try:
        pf.commit_and_push(tmp_path, "2026-11-05", ["latest.md"], push=False, state_path=tmp_path / "s.json")
    except RuntimeError as e:
        assert "git status" in str(e)
    else:
        raise AssertionError("RuntimeError が出ない")


def test_mainブランチ以外ではpushしない(monkeypatch, tmp_path):
    calls = _fake_git(monkeypatch, lambda cmd: (0, "dev\n"))
    try:
        pf.commit_and_push(tmp_path, "2026-11-05", ["latest.md"], push=True, state_path=tmp_path / "s.json")
    except RuntimeError as e:
        assert "main ではない" in str(e)
    else:
        raise AssertionError("RuntimeError が出ない")
    assert len(calls) == 1


def test_時間切れで孫プロセスごとkill(monkeypatch, tmp_path):
    killed = []

    class P:
        pid = 777
        returncode = None
        n = 0

        def communicate(self, timeout=None):
            P.n += 1
            if P.n == 1:
                raise subprocess.TimeoutExpired("git", timeout)
            return "", ""
    monkeypatch.setattr(pf.subprocess, "Popen", lambda cmd, **kw: P())
    monkeypatch.setattr(pf.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    try:
        pf._git(tmp_path, "status")
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("TimeoutExpired が出ない")
    assert killed == [(777, pf.signal.SIGKILL)]


# ---- 実リポジトリ + bare リモートの統合(ネットワーク無し)

def test_統合_生成ファイルだけがpushされる(monkeypatch, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    out = tmp_path / "feed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(out)], check=True)
    subprocess.run(["git", "-C", str(out), "remote", "add", "origin", str(bare)], check=True)
    (out / "unrelated.txt").write_text("他の stage 済みファイル")
    subprocess.run(["git", "-C", str(out), "add", "unrelated.txt"], check=True)
    monkeypatch.setattr(pf, "load_day_from_db", lambda d, slot, now: _day())
    monkeypatch.setattr(sys, "argv", ["publish_feed", "--out", str(out), "--date", "2026-11-05"])
    assert pf.main() == 0
    tree = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", "main"],
                          capture_output=True, text=True, check=True).stdout.split()
    assert "latest.md" in tree and ".nojekyll" in tree and "unrelated.txt" not in tree
    state = json.loads((tmp_path / "publish_state.json").read_text(encoding="utf-8"))
    head = subprocess.run(["git", "-C", str(out), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert state["pushed"] == head
    # 2回目は変更なしで push しない
    assert pf.main() == 0
