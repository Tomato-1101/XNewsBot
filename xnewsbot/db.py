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
    """テーブルを作成(なければ)する。"""
    SQLModel.metadata.create_all(get_engine())


@contextmanager
def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
