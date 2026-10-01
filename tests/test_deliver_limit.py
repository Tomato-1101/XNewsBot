"""ops/deliver.sh の limit_reset_wait(セッション上限の解除待ち秒数)を固定時刻で検証する。

関数本体は bash に埋め込んだ Python なので、その部分を取り出し、現在時刻だけ差し替えて実行する。
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_SH = Path(__file__).resolve().parent.parent / "ops" / "deliver.sh"
_SRC = re.search(r"limit_reset_wait\(\) \{\n  \"\$PY\" -c '\n(.*?)\n' \"\$1\"", _SH.read_text(), re.S).group(1)
JST = ZoneInfo("Asia/Tokyo")


def _wait(tmp_path, msg: str, now: dt.datetime) -> str:
    """解除待ち秒数の出力(何も出さなければ "")。now は JST の固定時刻。"""
    log = tmp_path / "out.txt"
    log.write_text(msg, encoding="utf-8")
    code = _SRC.replace("dt.datetime.now(", "_now(", 1)
    assert code != _SRC
    prelude = (f"import datetime as _d\n"
               f"def _now(tz=None):\n"
               f"    return _d.datetime.fromisoformat({now.isoformat()!r}).astimezone(tz)\n")
    r = subprocess.run([sys.executable, "-c", prelude + code, str(log)],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


MSG = "You've hit your session limit · resets 7:50am (Asia/Tokyo)"


@pytest.mark.parametrize("hm, expected", [
    ((7, 30), str(20 * 60 + 60)),   # 解除前: 残り20分 + 60s
    ((8, 0), "60"),                 # 並列の他パートを待つ間に10分過ぎた: すぐ再試行
    ((9, 15), "60"),                # 85分過ぎ(90分以内): すぐ再試行
    ((9, 30), ""),                  # 100分過ぎ: 翌日扱い(45分より先なので待たない)
    ((6, 0), ""),                   # 解除が110分先: 待たない
])
def test_limit_reset_wait_fixed_time(tmp_path, hm, expected):
    now = dt.datetime(2026, 10, 2, *hm, tzinfo=JST)
    assert _wait(tmp_path, MSG, now) == expected


def test_limit_reset_wait_next_day(tmp_path):
    # 23:50 に「resets 12:10am」= 翌日 0:10 まで20分
    now = dt.datetime(2026, 10, 2, 23, 50, tzinfo=JST)
    assert _wait(tmp_path, "session limit · resets 12:10am (Asia/Tokyo)", now) == str(20 * 60 + 60)


def test_limit_reset_wait_no_message(tmp_path):
    assert _wait(tmp_path, "some other error", dt.datetime(2026, 10, 2, 7, 0, tzinfo=JST)) == ""
