"""scripts/d1sync.py(D1 ↔ 一時 SQLite)のテスト。wrangler・D1 は呼ばない。

差分の SQL は、D1 の代わりの SQLite(外部キー強制・「pull の後に Worker が書いた」状態)へ実際に流して確かめる。
"""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("d1sync", ROOT / "scripts/d1sync.py")
d1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d1)

SCHEMA = (ROOT / "cloud/d1/schema.sql").read_text()

SEED = """
INSERT INTO subscriber (id, line_user_id, enabled_genres, pending_genres, morning_hour, morning_minute,
  evening_hour, evening_minute, morning_enabled, evening_enabled, tz, onboarding_step, is_onboarded,
  last_morning_on, created_at)
  VALUES (1, 'U1', '["AI"]', '[]', 8, 0, 21, 0, 1, 0, 'Asia/Tokyo', 'done', 1, '2026-11-18', '2026-11-01');
INSERT INTO genredigest (id, digest_date, slot, genre, created_at) VALUES (10, '2026-11-18', 'morning', 'AI', '2026-11-18');
INSERT INTO newsitem (id, genre_digest_id, genre, importance, rank, title, summary, source_urls, source_tweets, top_view_count)
  VALUES (100, 10, 'AI', 'big', 0, '古い記事A', '要約', '[]', '[]', 0),
         (101, 10, 'AI', 'small', 1, '古い記事B', '要約', '[]', '[]', 0);
INSERT INTO newsexplanation (id, news_item_id, status, text, error, claim_token, created_at, updated_at, title, push_to, push_cost)
  VALUES (5, 100, 'running', '', '', 'tok-a', '2026-11-18', '2026-11-18', '古い記事A', 'U1', 1);
INSERT INTO deliveryalert ("key", sent) VALUES ('2026-11-17:morning', 1);
"""


def make_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    # schema.sql の NOT NULL 列のうち SEED で埋めていないものがあれば、ここで気づけるよう素直に流す
    con.executescript(SEED)
    con.commit()
    return con


@pytest.fixture
def dbs(tmp_path):
    """(base=pull 直後, cur=Python が書き換える一時 DB, remote=D1 の代わり)。"""
    cons = [make_db(tmp_path / f"{n}.db") for n in ("base", "cur", "remote")]
    cons[2].execute("PRAGMA foreign_keys = ON")
    yield cons
    for c in cons:
        c.close()


def apply(remote: sqlite3.Connection, stmts: list[str]) -> None:
    """D1 の `execute --file` と同じく、分割した各ファイルを1トランザクションで流す。"""
    for text in d1.split_files(stmts):
        assert text.startswith(d1.PRAGMA + "\n")
        remote.executescript("BEGIN;\n" + text + "COMMIT;")


def test_seed_matches_schema(dbs):
    assert dbs[0].execute("SELECT count(*) FROM newsitem").fetchone()[0] == 2


def test_no_change_no_statements(dbs):
    base, cur, _ = dbs
    assert d1.build_statements(base, cur) == []


def test_reingest_replaces_rows_and_respects_foreign_keys(dbs):
    """ingest は記事を消して作り直す。消える記事を指す解説行ごと消し、外部キー違反にしない。"""
    base, cur, remote = dbs
    cur.executescript("""
        DELETE FROM newsexplanation; DELETE FROM newsitem; DELETE FROM genredigest;
        INSERT INTO genredigest (id, digest_date, slot, genre, created_at) VALUES (11, '2026-11-19', 'morning', 'AI', '2026-11-19');
        INSERT INTO newsitem (id, genre_digest_id, genre, importance, rank, title, summary, source_urls, source_tweets, top_view_count)
          VALUES (100, 11, 'AI', 'big', 0, '新しい記事', '要約', '[]', '[]', 0);
    """)
    # pull の後に Worker が別の記事(101)の解説を確保していた
    remote.execute("INSERT INTO newsexplanation (id, news_item_id, status, text, error, claim_token, created_at, "
                   "updated_at, title, push_to, push_cost) VALUES (6, 101, 'running', '', '', 'tok-b', "
                   "'2026-11-19', '2026-11-19', '古い記事B', 'U1', 1)")
    remote.commit()
    stmts = d1.build_statements(base, cur)
    apply(remote, stmts)
    assert remote.execute("SELECT id, genre_digest_id, title FROM newsitem").fetchall() == [(100, 11, "新しい記事")]
    assert remote.execute("SELECT id FROM genredigest").fetchall() == [(11,)]
    # 101 は消えたので、それを指す解説行(6)も消える。100 は id が使い回されただけなので解説行(5)は残る
    assert remote.execute("SELECT id FROM newsexplanation").fetchall() == [(5,)]
    assert remote.execute("PRAGMA foreign_key_check").fetchall() == []


def test_every_split_file_is_valid_on_its_own(dbs, monkeypatch):
    """大きくて複数ファイルに分かれても、途中のどの時点でも外部キーが崩れない順になっている。"""
    base, cur, remote = dbs
    cur.executescript("""
        DELETE FROM newsexplanation; DELETE FROM newsitem; DELETE FROM genredigest;
        INSERT INTO genredigest (id, digest_date, slot, genre, created_at) VALUES (11, '2026-11-19', 'morning', 'AI', '2026-11-19');
        INSERT INTO newsitem (id, genre_digest_id, genre, importance, rank, title, summary, source_urls, source_tweets, top_view_count)
          VALUES (200, 11, 'AI', 'big', 0, '新しい記事', '要約', '[]', '[]', 0);
    """)
    stmts = d1.build_statements(base, cur)
    files = d1.split_files(stmts, max_bytes=1)   # 1文ずつ別ファイル
    assert len(files) == len(stmts) > 3
    for text in files:
        remote.executescript("BEGIN;\n" + text + "COMMIT;")   # 違反があれば COMMIT で IntegrityError
    assert remote.execute("SELECT id FROM newsitem").fetchall() == [(200,)]


def test_worker_columns_and_rows_are_never_touched(dbs):
    base, cur, remote = dbs
    # ローカルで Worker の持ち物を変えてしまっても(バグや init_db の副作用を想定)、D1 には出さない
    cur.executescript("""
        UPDATE subscriber SET enabled_genres = '[]', morning_enabled = 0, last_morning_on = '2026-11-19';
        INSERT INTO subscriber (id, line_user_id, enabled_genres, pending_genres, morning_hour, morning_minute,
          evening_hour, evening_minute, morning_enabled, evening_enabled, tz, onboarding_step, is_onboarded, created_at)
          VALUES (2, 'U2', '[]', '[]', 8, 0, 21, 0, 1, 0, 'Asia/Tokyo', 'new', 0, '2026-11-19');
        INSERT INTO newsfeedback (id, newsitem_id, user_id, verdict, genre, title, created_at, updated_at)
          VALUES (1, 100, 'U1', 'good', 'AI', 't', '2026-11-19', '2026-11-19');
        INSERT INTO watchedaccount (id, handle, enabled, created_at) VALUES (1, 'x', 1, '2026-11-19');
        UPDATE newsexplanation SET push_to = 'U9', title = '変えた', claim_token = 'zzz';
    """)
    # pull の後に Worker が購読設定を変えていた
    remote.execute("UPDATE subscriber SET enabled_genres = '[\"AI\",\"経済\"]'")
    remote.commit()
    stmts = d1.build_statements(base, cur)
    assert stmts == ["UPDATE \"subscriber\" SET \"last_morning_on\"='2026-11-19' WHERE \"id\"=1;"]
    apply(remote, stmts)
    assert remote.execute("SELECT enabled_genres, morning_enabled, last_morning_on FROM subscriber").fetchall() == [
        ('["AI","経済"]', 1, "2026-11-19")]
    for table in ("newsfeedback", "watchedaccount"):
        assert not any(table in s for s in stmts)


def test_explanation_result_is_written_only_while_claim_is_ours(dbs):
    base, cur, remote = dbs
    cur.execute("UPDATE newsexplanation SET status='done', text='解説\n本文', updated_at='2026-11-19' WHERE id=5")
    cur.commit()
    stmts = d1.build_statements(base, cur)
    assert len(stmts) == 1 and stmts[0].startswith('UPDATE "newsexplanation" SET ')
    assert '"claim_token"=\'tok-a\'' in stmts[0] and '"push_to"' not in stmts[0] and '"title"' not in stmts[0]
    apply(remote, stmts)
    assert remote.execute("SELECT status, text, push_to FROM newsexplanation").fetchall() == [("done", "解説\n本文", "U1")]

    # Worker が確保を取り直していたら(合言葉が変わっている)、古い結果で上書きしない
    remote.execute("UPDATE newsexplanation SET status='running', text='', claim_token='tok-new'")
    remote.commit()
    apply(remote, stmts)
    assert remote.execute("SELECT status, text FROM newsexplanation").fetchall() == [("running", "")]


def test_alert_delete_and_kv_never_deleted(dbs):
    base, cur, remote = dbs
    for con in (base, cur, remote):
        con.execute("INSERT INTO kv (key, value, updated_at) VALUES ('mock_specs', '[]', 't')")
        con.commit()
    cur.executescript("""
        DELETE FROM deliveryalert; INSERT INTO deliveryalert ("key", sent) VALUES ('2026-11-19:morning', 0);
        DELETE FROM kv;
    """)
    stmts = d1.build_statements(base, cur)
    apply(remote, stmts)
    assert remote.execute('SELECT "key", sent FROM deliveryalert').fetchall() == [("2026-11-19:morning", 0)]
    assert remote.execute("SELECT key FROM kv").fetchall() == [("mock_specs",)]


@pytest.mark.parametrize("value", [
    "it's", "改行\nあり\r\nと{{NL0}}という文字そのもの", "", "絵文字🙂と\tタブ", None, 3, 2.5, b"\x00\xff",
])
def test_literal_roundtrip(value):
    out = d1.lit(value)
    assert "unistr" not in out and "char(" not in out   # D1 が許さない関数で包まない
    assert sqlite3.connect(":memory:").execute(f"SELECT {out}").fetchone()[0] == value


def test_long_value_is_split_under_statement_limit(dbs):
    """D1 は1文 100,000 バイトまで。mock_specs のような長い値は小分けに足し込む。"""
    base, cur, remote = dbs
    big = json.dumps([{"text": "見出し'と\n改行🙂" * 20} for _ in range(400)], ensure_ascii=False)
    assert len(big.encode()) > 200_000
    cur.execute("INSERT INTO kv (key, value, updated_at) VALUES ('mock_specs', ?, 't')", (big,))
    cur.commit()
    stmts = d1.build_statements(base, cur)
    assert len(stmts) > 2 and all(len(s.encode()) <= 100_000 for s in stmts)
    apply(remote, stmts)
    assert remote.execute("SELECT value FROM kv WHERE key='mock_specs'").fetchone()[0] == big
    # 途中で失敗して同じ差分を流し直しても、二重に足し込まれない
    apply(remote, stmts)
    assert remote.execute("SELECT value FROM kv WHERE key='mock_specs'").fetchone()[0] == big


def test_state_files_roundtrip(tmp_path, monkeypatch):
    db = tmp_path / "w" / "xnewsbot.db"
    db.parent.mkdir()
    feed = tmp_path / "feed" / "public-feed"
    monkeypatch.setenv("XNEWS_FEED_DIR", str(feed))
    con = sqlite3.connect(db)
    con.execute(d1.KV_DDL)
    con.executemany("INSERT INTO kv VALUES (?, ?, 't0')", [("watch_state", '{"a":1}'), ("publish_state", '{"p":1}'),
                                                           ("mock_specs", "[]")])
    assert sorted(d1.write_state_files(con, db)) == ["publish_state", "watch_state"]
    assert (db.parent / "watch_state.json").read_text() == '{"a":1}'
    assert (feed.parent / "publish_state.json").read_text() == '{"p":1}'
    assert not (db.parent / "seen_models.json").exists()

    # 変わったファイルと新しくできたファイルだけ kv に戻る(同じ中身は書かない)
    (db.parent / "watch_state.json").write_text('{"a":2}')
    (db.parent / "seen_models.json").write_text('["m"]')
    assert sorted(d1.read_state_files(con, db)) == ["seen_models", "watch_state"]
    rows = dict((k, (v, u)) for k, v, u in con.execute("SELECT key, value, updated_at FROM kv"))
    assert rows["watch_state"][0] == '{"a":2}' and rows["seen_models"][0] == '["m"]'
    assert rows["publish_state"] == ('{"p":1}', "t0")
    con.close()

    # XNEWS_FEED_DIR が無ければ公開フィードの状態は扱わない(Mac の本番の場所を指さない)
    monkeypatch.delenv("XNEWS_FEED_DIR")
    assert "publish_state" not in d1.state_files(db)


def _fake_wrangler(monkeypatch, remote_dump: str, calls: list):
    def fake(*args, cwd=None):
        calls.append(args)
        assert cwd is not None
        if args[0] == "export":
            Path(args[args.index("--output") + 1]).write_text(remote_dump)
    monkeypatch.setattr(d1, "run_wrangler", fake)


def test_pull_then_push_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("XNEWS_FEED_DIR", raising=False)
    remote = make_db(tmp_path / "remote.db")
    remote.execute("INSERT INTO kv VALUES ('watch_state', '{\"w\":1}', 't0')")
    remote.commit()
    dump = "PRAGMA defer_foreign_keys=TRUE;\n" + "\n".join(remote.iterdump())
    remote.close()
    calls: list = []
    _fake_wrangler(monkeypatch, dump, calls)
    db = tmp_path / "work" / "xnewsbot.db"
    db.parent.mkdir()
    db.write_text("古い中身")   # 前の実行の残りがあっても作り直す

    assert d1.cmd_pull(db) == 0
    assert calls[0][:3] == ("export", "xnewsbot", "--remote")
    assert d1.base_path(db).exists() and (db.parent / "watch_state.json").read_text() == '{"w":1}'
    assert d1.cmd_push(db) == 0 and len(calls) == 1          # 差分なし=wrangler を呼ばない
    assert "差分なし" in capsys.readouterr().out

    con = sqlite3.connect(db)
    con.execute("UPDATE subscriber SET last_morning_on='2026-11-19'")
    con.commit()
    con.close()
    (db.parent / "watch_state.json").write_text('{"w":2}')
    assert d1.cmd_push(db, dry_run=True) == 0 and len(calls) == 1
    sql = (db.parent / "xnewsbot.db.push0.sql").read_text()
    assert sql.splitlines()[0] == "PRAGMA defer_foreign_keys = true;"
    assert re.search(r'UPDATE "subscriber" SET "last_morning_on"=\'2026-11-19\' WHERE "id"=1;', sql)
    assert "INSERT OR REPLACE INTO \"kv\"" in sql and '{"w":2}' in sql

    assert d1.cmd_push(db) == 0
    assert calls[1][:3] == ("execute", "xnewsbot", "--remote") and "--file" in calls[1]
    assert d1.cmd_push(db) == 0 and len(calls) == 2          # 反映済みを送り直さない


def test_wrangler_failure_hides_urls(monkeypatch):
    """export は DB 全体を落とせる URL を表示する。失敗時の診断にも残さない。"""
    class R:
        returncode = 1
        stdout = "You can also download your export from the following URL manually. https://example.r2.dev/x?sig=SECRET\n"
        stderr = "boom"
    monkeypatch.setattr(d1.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        d1.run_wrangler("export", "xnewsbot")
    assert "SECRET" not in str(e.value) and "boom" in str(e.value)


def test_cli_requires_explicit_db(monkeypatch, capsys):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setattr(d1.sys, "argv", ["d1sync.py", "pull"])
    assert d1.main() == 2
