"""twitterapi.io 使用量ロガー（共有・stdlib のみ・fail-open）。

このファイルは twitterapi-usage プロジェクトが正本。x-research / XNewsBot / XAgent の
各リポジトリへ同一内容でコピーして使う（クロスリポジトリ import を避けるため）。

設計の肝:
- record() は何があっても例外を投げない（API 呼び出し本体を絶対に壊さない）。
- 記録するのは「観測できる生の事実」だけ: project / endpoint(path) / query_type /
  返ってきた件数 / HTTP ステータス / 鍵の末尾4桁(マスク) / 時刻。
  クレジット換算・コスト換算はダッシュボード側(pricing.py)が読み取り時に行う
  （単価が変わってもロガーを差し替えなくて済む）。
- API キーそのものは受け取らない・保存しない。呼び出し側が末尾4桁だけ key_label で渡す。

中央 DB のパス: 環境変数 TWITTERAPI_USAGE_DB 優先、無ければ DEFAULT_DB。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = "/Users/tomato/Project/twitterapi-usage/data/usage.db"

# requests テーブルの定義は db.py の正本と一致させること（IF NOT EXISTS で冪等）。
_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    ts_epoch    REAL    NOT NULL,
    project     TEXT    NOT NULL,
    endpoint    TEXT    NOT NULL,
    query_type  TEXT,
    items       INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    ok          INTEGER NOT NULL DEFAULT 1,
    key_label   TEXT,
    source      TEXT    NOT NULL DEFAULT 'live',
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_requests_project ON requests(project);
"""

# DDL と journal_mode は DB ごとに初回だけでよい（journal_mode=WAL は DB ファイルの
# 永続属性）。ホットパスで毎回 executescript するのを避ける
_schema_ready: set[str] = set()

# 中央 DB がロック中でも本体の API 呼び出しを待たせない上限（fail-open は維持）
_BUSY_TIMEOUT_MS = 500


def db_path() -> str:
    return os.environ.get("TWITTERAPI_USAGE_DB") or DEFAULT_DB


def count_items(data) -> int:
    """twitterapi.io のレスポンス dict から「課金対象の件数」を best-effort で数える。

    エンドポイントで件数の在処が違う: tweets / data.tweets / followings / members /
    users / followers。単一オブジェクト応答(user/info・単一ツイート)は 1 とみなす。
    数えられなければ 0（料金は最低 1 リクエスト分として pricing 側で下駄を履かせる）。
    """
    try:
        if not isinstance(data, dict):
            return 0
        v = data.get("tweets")
        if isinstance(v, list):
            return len(v)
        inner = data.get("data")
        if isinstance(inner, dict) and isinstance(inner.get("tweets"), list):
            return len(inner["tweets"])
        for k in ("followings", "members", "users", "followers"):
            v = data.get(k)
            if isinstance(v, list):
                return len(v)
        if isinstance(inner, dict) and inner:
            return 1
        if data.get("id") or data.get("userName") or data.get("userId"):
            return 1
        return 0
    except Exception:
        return 0


def mask(key) -> str | None:
    """API キーを末尾4桁だけに落とす（生の鍵は決して保存しない）。"""
    try:
        if not key:
            return None
        s = str(key)
        return "…" + s[-4:] if len(s) >= 4 else "…"
    except Exception:
        return None


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_CREATE_SQL)
    _schema_ready.add(path)


def record(
    project: str,
    endpoint: str,
    *,
    items: int = 0,
    query_type: str | None = None,
    http_status: int | None = None,
    ok: bool = True,
    key_label: str | None = None,
    source: str = "live",
    detail: str | None = None,
    ts_epoch: float | None = None,
) -> None:
    """1 リクエスト分を中央 DB に追記する。例外は握り潰す（fail-open）。"""
    try:
        path = db_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        if ts_epoch is None:
            now = datetime.now(timezone.utc)
            ts_epoch = now.timestamp()
            ts_iso = now.isoformat()
        else:
            ts_iso = datetime.fromtimestamp(ts_epoch, timezone.utc).isoformat()
        conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000)
        try:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            if path not in _schema_ready:
                _ensure_schema(conn, path)
            sql = (
                "INSERT INTO requests (ts, ts_epoch, project, endpoint, query_type, "
                "items, http_status, ok, key_label, source, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            )
            params = (
                ts_iso, float(ts_epoch), str(project), str(endpoint),
                query_type, int(items or 0), http_status,
                1 if ok else 0, key_label, source, detail,
            )
            try:
                conn.execute(sql, params)
            except sqlite3.OperationalError as exc:
                # DB を作り直された等でテーブルが無い場合だけ自己修復して再試行する
                # （毎回 DDL を流していた頃と同じ挙動）。ロック時に再試行すると
                # busy_timeout を二重に待って呼び出し元を余計に止めるので対象外
                if "no such table" not in str(exc):
                    raise
                _ensure_schema(conn, path)
                conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        # 使用量記録の失敗で本体(API呼び出し)を絶対に壊さない。
        pass
