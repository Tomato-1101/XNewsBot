"""中央 Keychain から環境変数を補う。

launchd から起動されるとログインシェルを通らないため `~/.config/apikeys.zsh` の
export が効かない。`.env` に秘密を置かずに済ませるため、未設定の変数だけ Keychain から読む。

置き場所の規則（2026-08-15 Tomato 指示）:
- プロバイダー共通のキー … service=変数名 / account=`shared`
- このプロジェクト固有の値 … service=変数名 / account=`XNewsBot`

`SecItem` を直接叩くと項目ごとにアクセス承認ダイアログが出るため `/usr/bin/security` を子プロセスで使う。
"""

from __future__ import annotations

import os
import subprocess

#: 全プロジェクト共通の置き場（account=shared）から読む変数
SHARED_KEYS = (
    "TWITTERAPI_IO_KEY",
    "LINE_CHANNEL_ACCESS_TOKEN",
    "LINE_CHANNEL_SECRET",
)

#: このプロジェクト専用の置き場（account=XNewsBot）から読む変数
PROJECT_KEYS = ("XNEWSBOT_ADMIN_PASSWORD",)

PROJECT_ACCOUNT = "XNewsBot"


def load() -> None:
    """未設定の環境変数を Keychain の値で埋める（既に入っている値は上書きしない）。"""
    for name in SHARED_KEYS:
        _fill(name, "shared")
    for name in PROJECT_KEYS:
        _fill(name, PROJECT_ACCOUNT)


def _fill(name: str, account: str) -> None:
    if os.environ.get(name):
        return
    value = read(name, account)
    if value:
        os.environ[name] = value


def read(service: str, account: str) -> str | None:
    """Keychain の 1 項目を読む。無ければ None（値はログに出さない）。"""
    try:
        proc = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None
