#!/usr/bin/env python3
"""日本の注目決算と決算サプライズ(日本)を Mac で取り、D1 の kv jp_schedule に書く(launchd -jp が毎朝 07:00)。

株探(AWS WAF)・IRBANK(CloudFront)はクラウドの IP を弾くので、家庭回線の Mac で取ってクラウドに渡す。
クラウドの collect は d1sync pull で jp_schedule.json に落とし、日付が配信日と同じときだけ使う
(Mac が寝ていて書けなかった日は、クラウドが live で試して取れない分が出ないだけ。配信は止めない)。

    .venv/bin/python scripts/push_jp_schedule.py

両方取れなかった日は何も書かずに終了コード 1。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import d1sync  # noqa: E402
from xnewsbot import schedule  # noqa: E402

WORKER_DIR = ROOT / "cloud" / "worker"   # wrangler.jsonc(D1 の設定)がある場所。.wrangler/ は gitignore 済み


def build_sql(data: dict, now: datetime | None = None) -> str:
    """kv に jp_schedule を書く SQL。列と updated_at の形式は d1sync の KV_DDL / read_state_files と同じ。"""
    now = now or datetime.now(timezone.utc)
    value = json.dumps(data, ensure_ascii=False)
    return ("INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES "
            f"('jp_schedule', {d1sync.lit(value)}, {d1sync.lit(now.strftime('%Y-%m-%dT%H:%M:%SZ'))});\n")


def main() -> int:
    t0 = time.monotonic()
    print(f"push_jp_schedule: 開始 {datetime.now(schedule.JST).isoformat(timespec='seconds')}")
    data = schedule.fetch_jp()
    n = {k: (None if data[k] is None else len(data[k])) for k in ("notable", "surprises")}
    print(f"  {data['date']}: 注目決算 {n['notable']} 件・サプライズ {n['surprises']} 件"
          f" ({time.monotonic() - t0:.1f} 秒)  ※None=取得失敗")
    if data["notable"] is None and data["surprises"] is None:
        print("push_jp_schedule: 注目決算もサプライズも取れなかったため D1 に書かずに終了")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "jp_schedule.sql"
        path.write_text(build_sql(data), encoding="utf-8")
        try:
            d1sync.run_wrangler("execute", d1sync.DB_NAME, "--remote", "--yes", "--file", str(path),
                                cwd=WORKER_DIR)
        except RuntimeError as e:
            print(f"push_jp_schedule: D1 への書き込みに失敗 {e}")
            return 1
    print(f"push_jp_schedule: D1 の kv jp_schedule を更新 ({time.monotonic() - t0:.1f} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
