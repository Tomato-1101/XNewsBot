"""テスト共通フィクスチャ。インメモリDB・FakeMessenger・フェイクツイート/Claude。"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from xnewsbot import models  # noqa: F401  (テーブル登録)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class FakeMessenger:
    """spec を記録するだけ(LINE へは送らない)。"""

    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict]]] = []
        self.pushes: list[tuple[str, list[dict]]] = []

    def reply(self, token: str, specs: list[dict]) -> None:
        self.replies.append((token, specs))

    def push(self, to: str, specs: list[dict]) -> None:
        self.pushes.append((to, specs))

    # 直近 reply の spec 列
    @property
    def last_reply(self) -> list[dict]:
        return self.replies[-1][1] if self.replies else []


@pytest.fixture
def messenger() -> FakeMessenger:
    return FakeMessenger()


def make_tweets(n: int = 5) -> list[dict]:
    return [
        {
            "text": f"テストニュース {i} の本文です。",
            "viewCount": 10000 - i * 100,
            "likeCount": 500 - i,
            "url": f"https://x.com/u/status/{i}",
            "author": {"userName": f"user{i}", "followers": 1000},
        }
        for i in range(n)
    ]


@pytest.fixture
def tweets() -> list[dict]:
    return make_tweets(5)


def curated_items(big: int = 1, small: int = 2) -> list[dict]:
    """キュレーション済み(Claude Code 出力相当)のアイテム dict 列。source_idxs 付き。"""
    items = []
    for i in range(big):
        items.append({"title": f"大ニュース{i}", "summary": "要約big",
                      "importance": "big", "score": 90 - i, "source_idxs": [i]})
    for j in range(small):
        items.append({"title": f"小ニュース{j}", "summary": "要約small",
                      "importance": "small", "score": 50 - j, "source_idxs": [big + j]})
    return items
