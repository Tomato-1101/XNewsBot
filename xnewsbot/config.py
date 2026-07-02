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
    # カンマ区切りで複数キーを優先度順に指定可(先頭が最優先・429/失敗で次へ)。詳細は xclient.load_keys。
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
    # ページングが深いほど遅い(twitterapi.io は1ページ数秒)。キュレーションへ渡す候補は
    # 上位 CURATE_INPUT_LIMIT(=60) 件。フィルタ(返信除外/除外語/いいね or 表示下限)で減るため、
    # 60件揃えられるよう取得上限はそれより多めにする(候補を広く取り Claude の選択肢を増やす)。
    collect_max_tweets: int = 100   # 1ジャンルあたり取得上限(課金/レート/速度対策)
    collect_hours: float = 24       # 収集対象の直近時間
    collect_min_faves: int = 200    # 最低いいね数(ノイズ除去)
    # 出たばかりで「いいね」が伸びる前の速報を取りこぼさないため、表示回数(viewCount)が
    # この値以上なら、いいね下限を満たさなくても採用する(いいね OR 表示回数)。
    collect_min_views_floor: int = 20000

    # --- 定時ダイジェストの無料ソース併用(質向上・コスト度外視) ---
    # X(twitterapi.io)に加え Google ニュースRSS を各ジャンルの候補に足して Claude の選択肢を厚くする。
    collect_use_newsfeeds: bool = True      # False で従来どおり X のみ
    collect_newsfeeds_per_genre: int = 15   # 1ジャンルに足す無料ニュース記事の上限(raw肥大→キュレーション時間を抑える)

    # --- 速報リアルタイム監視(scripts/monitor_breaking.py) ---
    breaking_enabled: bool = True
    # 速報の配信先グループID。空なら DB(subscriber.push_to のグループ=今のグループ)を自動解決。
    breaking_group_id: str | None = None
    # 検出の積極度: "strict"=大事件のみ / "medium"=各ジャンルの大きめも(既定) / "broad"=鮮度のみ。
    breaking_level: str = "medium"
    # 直近この分数以内に公開された記事だけを速報候補にする(監視間隔15分+取りこぼし余裕)。
    breaking_lookback_min: int = 25
    # 1日の最大push件数(グループを荒らさない/LINE無料枠200通/月を守る。既定5=月150+ダイジェスト分)。
    breaking_max_per_day: int = 5

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
