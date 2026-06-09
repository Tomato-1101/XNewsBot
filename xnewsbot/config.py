"""設定。環境変数 / .env から読み込む(pydantic-settings)。

流用元: XAgent/xagent/config.py の方針。
全フィールドを Optional/既定値ありにし、キー未設定でもインポート時にクラッシュしない
(LLM/LINE/X の実行時に未設定なら明示エラーを出す)。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # キュレーションは Claude Code(サブスク)の定期実行で行う(Anthropic API キーは使わない)。

    # --- LINE Messaging API ---
    line_channel_access_token: str | None = None
    line_channel_secret: str | None = None

    # --- twitterapi.io (Xの読み取り) ---
    # 空なら macOS Keychain(service=twitterapi_io_key) を自動で読む(x-research と鍵共有)。
    twitterapi_io_key: str | None = None

    # --- DB / サーバ ---
    db_path: str = "xnewsbot.db"
    port: int = 8010
    default_tz: str = "Asia/Tokyo"

    # --- スケジューラ ---
    # 定刻配信は launchd の配信ジョブ(ops/deliver.sh)が、その時刻ちょうどに
    # 「最新収集 → Claudeキュレーション → LINE送信」を一気通貫で行う(=リアルタイム)。
    # 常駐サーバ内の tick による DB 読み出し配信は既定で無効(二重送信・古いデータ送信を防ぐ)。
    scheduler_enabled: bool = False
    scheduler_interval_seconds: int = 60

    # --- 収集パラメータ ---
    collect_max_tweets: int = 120   # 1ジャンルあたり取得上限(課金/レート対策)
    collect_hours: float = 24       # 収集対象の直近時間
    collect_min_faves: int = 200    # 最低いいね数(ノイズ除去)

    @property
    def sqlite_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reload_settings() -> Settings:
    """.env/環境変数を編集した後に設定を読み直す(lru_cache を破棄)。
    稼働中プロセスは起動時値を保持するので、確実なのはプロセス再起動。"""
    get_settings.cache_clear()
    return get_settings()
