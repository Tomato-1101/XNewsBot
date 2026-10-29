"""ops/deliver.sh の配信前レビューの持ち時間(定刻配信は定刻までの残りから決める)を検証する。

review_timeout 関数だけを取り出して bash で実行する(配信・Codex は動かさない)。
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_SH = Path(__file__).resolve().parent.parent / "ops" / "deliver.sh"
_TEXT = _SH.read_text()
_FUNC = re.search(r"^review_timeout\(\) \{\n.*?^\}\n", _TEXT, re.S | re.M).group(0)


@pytest.mark.parametrize("left, expected", [
    (1000, "900"),   # 残りが多ければ 900秒のまま
    (960, "900"),
    (300, "240"),    # 定刻までの残り − 60秒
    (120, "60"),
    (119, ""),       # 120秒未満は省略
    (-5, ""),        # 定刻を過ぎている
])
def test_review_timeout(left, expected):
    r = subprocess.run(["/bin/bash", "-c", f"set -uo pipefail\n{_FUNC}review_timeout {left}"],
                       capture_output=True, text=True, check=True)
    assert r.stdout.strip() == expected


def test_review_call_uses_timeout_and_unique_report_name():
    """内部のタイムアウトを --timeout で渡し、外側はそれ + 60秒で打ち切る。report 名に時刻(HHMM)を入れる。
    定刻を使うのは定刻配信(due)だけで、それ以外は 900秒のまま。"""
    assert 'run_with_timeout $((REVIEW_TIMEOUT + 60))' in _TEXT
    assert '--timeout "$REVIEW_TIMEOUT"' in _TEXT
    assert '_${SLOT}_$(date +%H%M).json' in _TEXT
    assert 'REVIEW_TIMEOUT=900\n' in _TEXT and 'if [ "$MODE" = due ]; then' in _TEXT
    assert 'Codex レビューを省略(${REVIEW_SKIP})' in _TEXT


def test_review_abnormal_exit_log_avoids_exit_equals():
    """障害通知の分類(notify_recover_failure)は「exit=142/124」を見るので、レビューの異常終了ログに
    「exit=」を書かない(タイムアウトと誤分類されないように)。"""
    assert '異常終了(終了コード $REVIEW_RC)' in _TEXT
    assert 'exit=$REVIEW_RC' not in _TEXT
