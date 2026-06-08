"""SQLModel データモデル。

- Subscriber : LINE購読者。有効ジャンル・配信時刻・オンボーディング状態を持つ。
- GenreDigest: 「日×ジャンル」単位のキュレーション結果(複数購読者で再利用)。
- NewsItem   : GenreDigest 配下の1ニュース(大/小、見出し・要約・元ツイート)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Subscriber(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    line_user_id: str = Field(index=True, unique=True)
    display_name: str | None = None

    # 配信対象ジャンル(確定値)。例: ["AI", "株"]
    enabled_genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # オンボーディング中のジャンル累積選択(確定前の一時値)
    pending_genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # ローカル(tz)壁時計での配信時刻
    deliver_hour: int = 7
    deliver_minute: int = 0
    tz: str = "Asia/Tokyo"

    # "genres" -> "time" -> "done"。編集時も一時的に "genres"/"time" を取る。
    onboarding_step: str = "genres"
    # 初回オンボーディングを完了したか(編集フローと初回を区別する)
    is_onboarded: bool = False

    # その日の配信を済ませた日付(catch-up判定・二重配信防止)
    last_delivered_on: date | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class GenreDigest(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    digest_date: date = Field(index=True)   # 名前が型 date と衝突しないよう digest_date
    genre: str = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NewsItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    genre_digest_id: int = Field(index=True, foreign_key="genredigest.id")
    genre: str = ""             # 表示・詳細で使うため非正規化して保持
    importance: str = "small"   # "big" | "small"
    rank: int = 0
    title: str = ""
    summary: str = ""
    source_urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # [{"text":..., "author":..., "url":..., "views":int}, ...]
    source_tweets: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    top_view_count: int = 0
