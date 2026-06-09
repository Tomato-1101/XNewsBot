#!/usr/bin/env python3
"""レイアウト確認用のモック(架空)ニュースを DB に投入する。

LINEで「テスト」等を送ると、収集せずにこのサンプルが現行レイアウトで返る。
(トリガー側 onboarding は未投入なら自動 seed するので、本スクリプトは任意の手動投入用。)

    python scripts/seed_mock.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xnewsbot import mockdata  # noqa: E402
from xnewsbot.db import get_session, init_db  # noqa: E402


def main() -> None:
    init_db()
    with get_session() as session:
        n = mockdata.seed(session)
        grouped = mockdata.assemble(session)
    print(f"モック投入: {n} 件 ({mockdata.MOCK_DATE} / {mockdata.MOCK_SLOT})", file=sys.stderr)
    for genre, items in grouped.items():
        if items:
            nb = sum(1 for it in items if it.importance == "big")
            print(f"  {genre}: {len(items)}件 (大{nb})", file=sys.stderr)


if __name__ == "__main__":
    main()
