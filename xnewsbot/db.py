"""DBエンジンとセッション。SQLite に作成する。流用元: XAgent/xagent/db.py。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

# import しておくことで create_all がテーブルを認識する
from . import models  # noqa: F401

_engine = None


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    """常駐サーバ(webhook) / 管理UI(:8011) / 配信ジョブが同じ xnewsbot.db を同時に触るため、
    接続ごとに WAL(読みと書きが並行できる)と busy_timeout(ロック中は即諦めず待つ)を入れる。
    既定は busy_timeout=0 で、収集中の書き込みと webhook が重なると即 "database is locked"
    になり取りこぼす。様式は tau_log.py に合わせる。"""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        # check_same_thread=False: FastAPI/スケジューラの別スレッドからも使うため
        # timeout: ロック待ちの上限秒(sqlite3.connect の引数。PRAGMA busy_timeout と同じ待ち)
        _engine = create_engine(
            settings.sqlite_url,
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        event.listen(_engine, "connect", _sqlite_pragmas)
    return _engine


def init_db() -> None:
    """テーブルを作成(なければ)し、後から増えた列を追補する。"""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _migrate(engine)


def _migrate(engine) -> None:
    """SQLite は create_all で既存テーブルに列を追加しないため、不足列を ALTER で補う(冪等)。"""
    from sqlalchemy import text

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(subscriber)"))}
        if cols and "push_to" not in cols:
            conn.execute(text("ALTER TABLE subscriber ADD COLUMN push_to VARCHAR"))

        ncols = {row[1] for row in conn.execute(text("PRAGMA table_info(newsitem)"))}
        if ncols and "detail" not in ncols:
            conn.execute(text("ALTER TABLE newsitem ADD COLUMN detail VARCHAR DEFAULT ''"))
        if ncols and "genres" not in ncols:
            # JSON 列。既存行は空配列にしておき、表示時は主ジャンル genre で代替する。
            conn.execute(text("ALTER TABLE newsitem ADD COLUMN genres JSON DEFAULT '[]'"))


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
