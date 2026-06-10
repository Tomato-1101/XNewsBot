"""SQLModel データモデル。

- Subscriber : LINE購読者。有効ジャンル・朝/夜の配信時刻・オンボーディング状態を持つ。
- GenreDigest: 「日×スロット(朝/夜)×ジャンル」単位のキュレーション結果(複数購読者で再利用)。
- NewsItem   : GenreDigest 配下の1ニュース(大/小、見出し・要約・元ツイート)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

# 1日2回配信のスロット。morning=朝, evening=夜。
SLOTS: tuple[str, ...] = ("morning", "evening")
SLOT_LABEL: dict[str, str] = {"morning": "朝", "evening": "夜"}


class Subscriber(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    line_user_id: str = Field(index=True, unique=True)
    display_name: str | None = None

    # 配信対象ジャンル(確定値)。例: ["AI", "株"]
    enabled_genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # オンボーディング中のジャンル累積選択(確定前の一時値)
    pending_genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # ローカル(tz)壁時計での配信時刻(朝・夜)
    morning_hour: int = 8
    morning_minute: int = 0
    evening_hour: int = 21
    evening_minute: int = 0
    morning_enabled: bool = True
    evening_enabled: bool = True
    tz: str = "Asia/Tokyo"

    # "genres" -> "morning" -> "evening" -> "done"。編集時も一時的に "genres"/"morning"/"evening"。
    onboarding_step: str = "genres"
    # 初回オンボーディングを完了したか(編集フローと初回を区別する)
    is_onboarded: bool = False

    # スロットごとに「その日の配信を済ませた日付」(catch-up判定・二重配信防止)
    last_morning_on: date | None = None
    last_evening_on: date | None = None

    # 配信先の上書き。LINEのグループID/ルームID。None なら本人との1:1トークへ送る。
    # グループ内で合言葉を送ると、そのグループIDがここに入る(onboarding._handle_message)。
    push_to: str | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def push_target(self) -> str:
        """実際の push 送信先。push_to(グループ等)があればそれ、無ければ本人の1:1。"""
        return self.push_to or self.line_user_id

    # --- スロット別アクセサ(scheduler/onboarding を簡潔にするため) ---
    def slot_time(self, slot: str) -> tuple[int, int]:
        if slot == "morning":
            return self.morning_hour, self.morning_minute
        return self.evening_hour, self.evening_minute

    def slot_enabled(self, slot: str) -> bool:
        return self.morning_enabled if slot == "morning" else self.evening_enabled

    def last_on(self, slot: str) -> date | None:
        return self.last_morning_on if slot == "morning" else self.last_evening_on

    def set_slot_time(self, slot: str, hour: int, minute: int) -> None:
        if slot == "morning":
            self.morning_hour, self.morning_minute = hour, minute
        else:
            self.evening_hour, self.evening_minute = hour, minute

    def set_last_on(self, slot: str, d: date) -> None:
        if slot == "morning":
            self.last_morning_on = d
        else:
            self.last_evening_on = d


class GenreDigest(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    digest_date: date = Field(index=True)   # 名前が型 date と衝突しないよう digest_date
    slot: str = Field(default="morning", index=True)  # "morning" | "evening"
    genre: str = Field(index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NewsItem(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    genre_digest_id: int = Field(index=True, foreign_key="genredigest.id")
    genre: str = ""             # 所属ダイジェストのジャンル(主ジャンル)。出典idxの基準・非正規化保持
    # 表示用の該当ジャンルタグ(複数可)。横断重複排除で1件にまとめた話題が、どのジャンルに
    # 関係するかを示す(例: 利上げ → ["経済","株","政治"])。空なら表示時 [genre] で代替。
    genres: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    importance: str = "small"   # "big" | "small"
    rank: int = 0
    title: str = ""
    summary: str = ""          # 見出し一覧/大ニュース inline 用の簡潔な要約(2〜3文)
    detail: str = ""           # 「詳細を見る」タップ時に出す長め解説(背景・経緯。空なら summary で代替)
    source_urls: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # [{"text":..., "author":..., "url":..., "views":int}, ...]
    source_tweets: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    top_view_count: int = 0
