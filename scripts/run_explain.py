#!/usr/bin/env python3
"""AI解説を1件作る(GitHub Actions 用。cloud/DESIGN.md の explain.yml)。

    DB_PATH=/tmp/x/xnewsbot.db python scripts/run_explain.py --id <newsexplanation.id>

行は Worker が確保(claim)済み。ここでは生成して結果を保存し、push_to があればそこへ1通 push する
(中身は Mac の webhook が裏スレッドで呼んでいた explain.run_line_job と同じ)。
D1 との往復は cloud/actions/explain.sh が前後で行う(d1sync pull / push)。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xnewsbot import db, explain  # noqa: E402
from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot.config import get_settings  # noqa: E402
from xnewsbot.models import NewsExplanation  # noqa: E402


def run(explanation_id: int, messenger=None, session_factory=None) -> int:
    factory = session_factory or db.get_session
    with factory() as s:
        row = s.get(NewsExplanation, explanation_id)
        if row is None:
            print(f"run_explain: id={explanation_id} の行が無い", file=sys.stderr)
            return 1
        if row.status != "running":
            # 二重起動や、別の実行が先に終えた場合。作り直すと同じ解説を2通送ってしまう
            print(f"run_explain: id={explanation_id} は {row.status}(作成中ではない)ので何もしない")
            return 0
        item_id, token, to = row.news_item_id, row.claim_token, row.push_to
    if to:
        if messenger is None:
            messenger = lc.LineMessenger(get_settings().line_channel_access_token)
        explain.run_line_job(item_id, token, to, messenger, session_factory)
    else:
        explain.run_job(item_id, token, session_factory)
    with factory() as s:
        row = s.get(NewsExplanation, explanation_id)
        status = row.status if row else "なし"
    print(f"run_explain: id={explanation_id} item={item_id} 結果={status} 送り先={'あり' if to else 'なし'}")
    # 生成の失敗は行に failed と記録済み(利用者にも1通で知らせている)。結果を D1 へ戻せるよう正常終了にする
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--id", type=int, required=True, help="newsexplanation.id")
    return run(ap.parse_args().id)


if __name__ == "__main__":
    sys.exit(main())
