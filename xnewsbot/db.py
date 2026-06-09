"""DBエンジンとセッション。SQLite に作成する。流用元: XAgent/xagent/db.py。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, SQLModel, create_engine

from .config import get_settings

# import しておくことで create_all がテーブルを認識する
from . import models  # noqa: F401

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        # check_same_thread=False: FastAPI/スケジューラの別スレッドからも使うため
        _engine = create_engine(
            settings.sqlite_url, connect_args={"check_same_thread": False}
        )
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


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
