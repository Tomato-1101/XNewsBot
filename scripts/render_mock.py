#!/usr/bin/env python3
"""LINE の「テスト」で返すサンプル(架空データ)を描いて、kv の mock_specs に入れる(cloud/DESIGN.md)。

    DB_PATH=/tmp/x/xnewsbot.db python scripts/render_mock.py

クラウドでは Worker が「テスト」に返事をするが、レイアウトは Python(line_client.digest_specs)にしか無い。
Actions が毎回ここで描き直して kv に置き、Worker はそれをそのまま reply する(=配信と同じ見た目になる)。
モックの行は使い捨てのメモリ DB に入れる(本物の表に架空の記事を混ぜない)。DB_PATH に書くのは kv の1行だけ。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot import mockdata  # noqa: E402

KV_KEY = "mock_specs"
KV_DDL = ("CREATE TABLE IF NOT EXISTS kv (key VARCHAR NOT NULL PRIMARY KEY, value TEXT NOT NULL, "
          "updated_at VARCHAR NOT NULL)")


def render() -> list[dict]:
    """「テスト」への返事(onboarding._try_command と同じ組み立て)を、LINE API にそのまま渡せる形で返す。"""
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        grouped = mockdata.get_or_seed(session)
        specs = [lc.text_spec(mockdata.WARNING)] + lc.digest_specs(
            grouped, greeting=False, market=mockdata.MOCK_MARKET, schedule=mockdata.MOCK_SCHEDULE,
            x_usage=mockdata.MOCK_X_USAGE, line_quota=mockdata.MOCK_LINE_QUOTA,
            # 先頭の警告文と合わせて reply の上限(5通)に収める
            max_messages=lc.MAX_MESSAGES - 1)
    # 送信時と同じ変換(SDK のモデル)を通す。Worker 側に spec の解釈を二重に持たせないため
    return [lc._spec_to_message(s).to_dict() for s in specs]


def save(db: Path, messages: list[dict]) -> int:
    value = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    con = sqlite3.connect(db)
    try:
        con.execute(KV_DDL)
        row = con.execute("SELECT value FROM kv WHERE key=?", (KV_KEY,)).fetchone()
        if row is not None and row[0] == value:
            return len(value.encode())   # 同じ見た目なら書かない(毎回の D1 への書き込みを増やさない)
        con.execute("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                    (KV_KEY, value, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        con.commit()
    finally:
        con.close()
    return len(value.encode())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("DB_PATH"), help="kv を書く SQLite(既定は環境変数 DB_PATH)")
    a = ap.parse_args()
    if not a.db:
        print("render_mock: DB_PATH(または --db)を指定してください", file=sys.stderr)
        return 2
    messages = render()
    size = save(Path(a.db).expanduser(), messages)
    print(f"render_mock: {len(messages)} 通 {size} バイトを kv[{KV_KEY}] に保存")
    return 0


if __name__ == "__main__":
    sys.exit(main())
