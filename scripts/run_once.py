#!/usr/bin/env python3
"""手動実行CLI。収集+キュレーションの確認、または指定ユーザーへの即時配信。

  # 収集+キュレーション結果を表示(pushせず・DB不要)。twitterapi.io と Claude を実走。
  python scripts/run_once.py --genres AI --dry-run

  # 当日ダイジェストを組み立てて指定の LINE ユーザーへ push(実配信)。
  python scripts/run_once.py --genres AI,株 --user Uxxxxxxxx
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# scripts/ の親 = プロジェクトルートを import パスに
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot import xclient  # noqa: E402
from xnewsbot.config import get_settings  # noqa: E402
from xnewsbot.curator import Curator  # noqa: E402
from xnewsbot.genres import GENRE_KEYS, is_valid_genre  # noqa: E402


def _print_curated(genre: str, items) -> None:
    print(f"\n===== {genre} : {len(items)} 件 =====")
    for it in items:
        tag = "★大" if it.importance == "big" else "  小"
        print(f"[{tag}] (score={it.score}) {it.title}")
        if it.summary:
            print(f"      {it.summary}")


def main() -> None:
    p = argparse.ArgumentParser(description="XNewsBot 手動実行CLI")
    p.add_argument("--genres", default="AI", help="カンマ区切り(例 AI,株,経済,政治)")
    p.add_argument("--dry-run", action="store_true", help="収集+キュレーションのみ表示(push/DBなし)")
    p.add_argument("--user", help="push 先の LINE userId(--dry-run でないとき必須)")
    args = p.parse_args()

    genres = [g.strip() for g in args.genres.split(",") if g.strip()]
    bad = [g for g in genres if not is_valid_genre(g)]
    if bad:
        sys.exit(f"未知のジャンル: {bad}  有効: {GENRE_KEYS}")

    settings = get_settings()
    curator = Curator(settings)

    if args.dry_run or not args.user:
        for genre in genres:
            tweets = xclient.collect(genre, settings=settings)
            print(f"# {genre}: 収集 {len(tweets)} 件")
            items = curator.curate(genre, tweets)
            _print_curated(genre, items)
        return

    # 実配信
    if not settings.line_channel_access_token:
        sys.exit("LINE_CHANNEL_ACCESS_TOKEN が未設定です。.env を設定してください。")
    from xnewsbot import digest
    from xnewsbot.db import get_session, init_db

    init_db()
    local_date = datetime.now(ZoneInfo(settings.default_tz)).date()
    messenger = lc.LineMessenger(settings.line_channel_access_token)
    with get_session() as session:
        grouped = digest.assemble_for_genres(
            session, genres, local_date, curator=curator, settings=settings
        )
        specs = lc.digest_specs(grouped, greeting=True)
        messenger.push(args.user, specs)
    print(f"push 完了 → {args.user} ({len(specs)} メッセージ)")


if __name__ == "__main__":
    main()
