#!/usr/bin/env python3
"""Cloudflare D1(正)と、Actions 上の一時 SQLite を行き来させる(cloud/DESIGN.md)。

    DB_PATH=/tmp/x/xnewsbot.db python scripts/d1sync.py pull   D1 を丸ごと一時 SQLite に落とす
    DB_PATH=/tmp/x/xnewsbot.db python scripts/d1sync.py push   pull 直後との差分だけ D1 に戻す

- pull は `wrangler d1 export` の SQL をそのまま流し込み、取得直後の写し(<DB_PATH>.base)を残す。
  kv の watch_state / seen_models / publish_state は、Python が読む場所へファイルとして書き出す。
- push が書くのは「Actions だけが書く」表・列だけ(FULL_TABLES / COLUMN_TABLES)。Worker が書く表・列は
  SQL に一切出さない。例外は外部キーの都合の1つだけ: 消える記事(newsitem)を指す newsexplanation の行は
  一緒に消す(D1 は外部キーを強制するので、残すと記事を消せず配信の取り込み直しが丸ごと失敗する)。
- DB_PATH(または --db)の明示が必須。既定の xnewsbot.db(Mac の本番)を誤って消さないため。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_NAME = os.environ.get("XNEWS_D1_NAME", "xnewsbot")
WRANGLER = os.environ.get("XNEWS_WRANGLER", "wrangler")
WRANGLER_TIMEOUT = 600

# 行ごと置き換える表(Actions だけが書く)。親 → 子の順(upsert はこの順、削除は逆順)
FULL_TABLES = ("genredigest", "newsitem", "marketsnapshot", "schedulesnapshot", "xusagesnapshot",
               "deliveryalert", "kv")
NO_DELETE = ("kv",)  # mock_specs 等を持つ。Python が行を消すことは無いので削除は出さない
# 列だけ書く表: {表: (Actions が書く列, 一致を確かめる列)}。行の作成・削除と他の列は Worker のもの
COLUMN_TABLES = {
    "subscriber": (("last_morning_on", "last_evening_on"), None),
    # claim_token が pull 時と同じ行にだけ書く(Worker が取り直した確保に、古い結果を上書きしない)
    "newsexplanation": (("status", "text", "error", "updated_at", "push_cost"), "claim_token"),
}
KV_DDL = ("CREATE TABLE IF NOT EXISTS kv (key VARCHAR NOT NULL PRIMARY KEY, value TEXT NOT NULL, "
          "updated_at VARCHAR NOT NULL)")

PRAGMA = "PRAGMA defer_foreign_keys = true;"
MAX_STMT_BYTES = 90_000    # D1 の上限は1文 100,000 バイト
CHUNK_CHARS = 15_000       # 長い文字列を足し込む1回分(4バイト文字ばかりでも 60KB)
MAX_FILE_BYTES = 5_000_000  # 1ファイル=1トランザクション。普段の差分(1日分)は 1MB に届かない
DELETE_BATCH = 500


# ---------------------------------------------------------------- 状態ファイル

def state_files(db: Path) -> dict[str, Path]:
    """kv のキー → Python が読むファイル。watch.state_path / trends の seen_models / publish_feed と同じ場所。"""
    d = db.resolve().parent
    files = {"watch_state": d / "watch_state.json", "seen_models": d / "seen_models.json",
             # Mac(scripts/push_jp_schedule.py)が書く日本の決算。クラウドは読むだけ(中身が同じなら push しない)
             "jp_schedule": d / "jp_schedule.json"}
    # 公開フィードは XNEWS_FEED_DIR を明示したときだけ扱う(未設定だと Mac の本番の publish_state.json を指すため)
    feed = os.environ.get("XNEWS_FEED_DIR")
    if feed:
        files["publish_state"] = Path(feed).expanduser().parent / "publish_state.json"
    return files


def _has_table(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def write_state_files(con: sqlite3.Connection, db: Path) -> list[str]:
    if not _has_table(con, "kv"):
        return []
    done = []
    for key, path in state_files(db).items():
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(row[0], encoding="utf-8")
        done.append(key)
    return done


def read_state_files(con: sqlite3.Connection, db: Path) -> list[str]:
    """状態ファイルを kv に戻す。中身が kv と同じなら触らない(並行する別ジョブの値を古い値で上書きしない)。"""
    con.execute(KV_DDL)
    done = []
    for key, path in state_files(db).items():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if row is not None and row[0] == text:
            continue
        con.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, text, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        done.append(key)
    con.commit()
    return done


# ---------------------------------------------------------------- 差分 SQL

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(v) -> str:
    """SQL リテラル。文字列は '…' の中にそのまま入れ、' だけ '' にする(改行もそのまま)。

    sqlite3 の .dump が使う unistr() は D1 が許さない("not authorized to use function")ので関数で包まない。"""
    if v is None:
        return "NULL"
    if isinstance(v, (bool, int)):
        return str(int(v))
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _where(pk_cols: list[str], pk: tuple) -> str:
    return " AND ".join(f"{_q(c)}={lit(v)}" for c, v in zip(pk_cols, pk))


def upsert_stmts(table: str, cols: list[str], pk_cols: list[str], row: tuple) -> list[str]:
    """INSERT OR REPLACE。1文の上限を超える行は、長い文字列の列を空で入れてから小分けに足し込む。"""
    def insert(values: list) -> str:
        return (f"INSERT OR REPLACE INTO {_q(table)} ({', '.join(_q(c) for c in cols)}) "
                f"VALUES ({', '.join(lit(v) for v in values)});")

    values = list(row)
    stmt = insert(values)
    if len(stmt.encode()) <= MAX_STMT_BYTES:
        return [stmt]
    pk = tuple(row[cols.index(c)] for c in pk_cols)
    tail = []
    # 長い順に外へ出し、INSERT が上限に収まったところでやめる
    for i in sorted((i for i, v in enumerate(row) if isinstance(v, str) and cols[i] not in pk_cols),
                    key=lambda i: -len(row[i])):
        text, values[i] = row[i], ""
        for at in range(0, len(text), CHUNK_CHARS):
            tail.append(f"UPDATE {_q(table)} SET {_q(cols[i])} = {_q(cols[i])} || "
                        f"{lit(text[at:at + CHUNK_CHARS])} WHERE {_where(pk_cols, pk)};")
        stmt = insert(values)
        if len(stmt.encode()) <= MAX_STMT_BYTES:
            break
    return [stmt, *tail]


def delete_stmts(table: str, pk_cols: list[str], pks: list[tuple], column: str | None = None) -> list[str]:
    """主キーが1列なら IN でまとめて消す。column を渡すと、その列が pks の値である行を消す。"""
    if not pks:
        return []
    if len(pk_cols) == 1:
        col = column or pk_cols[0]
        ids = [pk[0] for pk in pks]
        return [f"DELETE FROM {_q(table)} WHERE {_q(col)} IN "
                f"({', '.join(lit(i) for i in ids[at:at + DELETE_BATCH])});"
                for at in range(0, len(ids), DELETE_BATCH)]
    return [f"DELETE FROM {_q(table)} WHERE {_where(pk_cols, pk)};" for pk in pks]


def _columns(con: sqlite3.Connection, table: str) -> tuple[list[str], list[str]]:
    info = con.execute(f"PRAGMA table_info({_q(table)})").fetchall()
    cols = [r[1] for r in info]
    pk_cols = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    return cols, pk_cols


def _rows(con: sqlite3.Connection, table: str, cols: list[str], pk_cols: list[str]) -> dict[tuple, tuple]:
    if not _has_table(con, table):
        return {}
    idx = [cols.index(c) for c in pk_cols]
    out = {}
    for row in con.execute(f"SELECT {', '.join(_q(c) for c in cols)} FROM {_q(table)}"):
        out[tuple(row[i] for i in idx)] = tuple(row)
    return out


def build_statements(base: sqlite3.Connection, cur: sqlite3.Connection) -> list[str]:
    """pull 直後(base)と今(cur)を比べ、D1 に流す文を外部キーが途中でも崩れない順で返す(PRAGMA は含めない)。"""
    upserts: list[str] = []
    deletes: dict[str, list[str]] = {}
    for table in FULL_TABLES:
        if not _has_table(cur, table):
            continue
        cols, pk_cols = _columns(cur, table)
        if not pk_cols:
            continue
        # 列は今の表のものを使う。base に無い列(あり得ないが)があれば全行が「変わった」扱いになるだけ
        base_cols = cols if _has_table(base, table) and _columns(base, table)[0] == cols else None
        old = _rows(base, table, cols, pk_cols) if base_cols else {}
        new = _rows(cur, table, cols, pk_cols)
        for pk, row in new.items():
            if old.get(pk) != row:
                upserts += upsert_stmts(table, cols, pk_cols, row)
        if table in NO_DELETE:
            continue
        gone = [pk for pk in old if pk not in new]
        stmts = []
        if table == "newsitem" and gone:
            # 外部キー: 消える記事を指す解説行を先に消す(pull の後に Worker が作った行も D1 側で拾える)
            stmts += delete_stmts("newsexplanation", pk_cols, gone, column="news_item_id")
        deletes[table] = stmts + delete_stmts(table, pk_cols, gone)

    updates: list[str] = []
    for table, (write_cols, guard) in COLUMN_TABLES.items():
        if not (_has_table(cur, table) and _has_table(base, table)):
            continue
        cols, pk_cols = _columns(cur, table)
        if _columns(base, table)[0] != cols or not pk_cols:
            continue
        old = _rows(base, table, cols, pk_cols)
        for pk, row in _rows(cur, table, cols, pk_cols).items():
            before = old.get(pk)
            if before is None:
                continue  # 行を作るのは Worker だけ
            changed = [c for c in write_cols if c in cols and before[cols.index(c)] != row[cols.index(c)]]
            if not changed:
                continue
            where = _where(pk_cols, pk)
            if guard:
                where += f" AND {_q(guard)}={lit(before[cols.index(guard)])}"
            sets = ", ".join(f"{_q(c)}={lit(row[cols.index(c)])}" for c in changed)
            updates.append(f"UPDATE {_q(table)} SET {sets} WHERE {where};")

    out = upserts + updates
    for table in reversed(FULL_TABLES):  # 子 → 親の順で消す
        out += deletes.get(table, [])
    return out


def split_files(stmts: list[str], max_bytes: int = MAX_FILE_BYTES) -> list[str]:
    """文の列をファイルの中身に分ける。どのファイルも先頭に PRAGMA を置く(外部キーの遅延はトランザクションごと)。"""
    files, cur, size = [], [PRAGMA], 0
    for s in stmts:
        n = len(s.encode()) + 1
        if size and size + n > max_bytes:
            files.append("\n".join(cur) + "\n")
            cur, size = [PRAGMA], 0
        cur.append(s)
        size += n
    if size:
        files.append("\n".join(cur) + "\n")
    return files


# ---------------------------------------------------------------- wrangler

def run_wrangler(*args: str, cwd: Path | None = None) -> None:
    """wrangler d1 を実行する。出力は見せない(export は DB 全体を落とせる期限つき URL を表示するため)。

    cwd は作業用フォルダにする(wrangler が実行した場所に .wrangler/ を作るので、リポジトリを汚さない)。"""
    cmd = [WRANGLER, "d1", *args]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=WRANGLER_TIMEOUT, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"wrangler d1 {args[0]} を実行できません: {type(e).__name__}") from None
    if r.returncode:
        tail = re.sub(r"https?://\S+", "[URL]", ((r.stdout or "") + (r.stderr or "")).strip())[-1500:]
        raise RuntimeError(f"wrangler d1 {args[0]} が失敗(終了コード {r.returncode})\n{tail}")


def _backup(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    a, b = sqlite3.connect(src), sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def base_path(db: Path) -> Path:
    return db.with_name(db.name + ".base")


# ---------------------------------------------------------------- コマンド

def cmd_pull(db: Path) -> int:
    db.parent.mkdir(parents=True, exist_ok=True)
    dump = db.with_name(db.name + ".export.sql")
    dump.unlink(missing_ok=True)
    run_wrangler("export", DB_NAME, "--remote", "--output", str(dump), cwd=db.parent)
    for p in (db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
        p.unlink(missing_ok=True)
    con = sqlite3.connect(db)
    try:
        con.executescript(dump.read_text(encoding="utf-8"))
        con.commit()
        states = write_state_files(con, db)
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        counts = {t: con.execute(f"SELECT count(*) FROM {_q(t)}").fetchone()[0] for t in tables}
    finally:
        con.close()
    dump.unlink(missing_ok=True)
    _backup(db, base_path(db))
    print(f"d1sync pull: {len(tables)} 表 {sum(counts.values())} 行 "
          f"({', '.join(f'{t}={n}' for t, n in counts.items())}) 状態ファイル: {', '.join(states) or 'なし'}")
    return 0


def cmd_push(db: Path, dry_run: bool = False) -> int:
    base = base_path(db)
    if not db.exists() or not base.exists():
        print(f"d1sync push: {db} か {base.name} が無い(先に pull する)", file=sys.stderr)
        return 1
    cur_con = sqlite3.connect(db)
    base_con = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    try:
        states = read_state_files(cur_con, db)
        stmts = build_statements(base_con, cur_con)
    finally:
        cur_con.close()
        base_con.close()
    if not stmts:
        print("d1sync push: 差分なし")
        return 0
    files = split_files(stmts)
    paths = []
    for i, text in enumerate(files):
        p = db.with_name(f"{db.name}.push{i}.sql")
        p.write_text(text, encoding="utf-8")
        paths.append(p)
    summary = f"{len(stmts)} 文 / {len(files)} ファイル / {sum(len(t.encode()) for t in files)} バイト"
    if dry_run:
        print(f"d1sync push(dry-run): {summary} → {', '.join(str(p) for p in paths)}")
        return 0
    for p in paths:
        run_wrangler("execute", DB_NAME, "--remote", "--yes", "--file", str(p), cwd=db.parent)
        p.unlink(missing_ok=True)
    # 同じ実行の中でもう一度 push しても同じ文を送り直さないよう、写しを今の状態に進める
    _backup(db, base)
    print(f"d1sync push: {summary} を反映 状態ファイル: {', '.join(states) or '変更なし'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["pull", "push"])
    ap.add_argument("--db", default=os.environ.get("DB_PATH"), help="一時 SQLite の場所(既定は環境変数 DB_PATH)")
    ap.add_argument("--dry-run", action="store_true", help="push: SQL ファイルを書くだけで D1 には流さない")
    a = ap.parse_args()
    if not a.db:
        print("d1sync: DB_PATH(または --db)を指定してください", file=sys.stderr)
        return 2
    db = Path(a.db).expanduser().resolve()
    try:
        return cmd_pull(db) if a.command == "pull" else cmd_push(db, a.dry_run)
    except (RuntimeError, sqlite3.Error, OSError) as e:
        print(f"d1sync {a.command}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
