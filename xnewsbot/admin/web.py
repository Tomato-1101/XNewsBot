"""管理UIの共有部品: Jinja2 テンプレートと HTTP Basic 認証。

LAN 公開するため全ルートに認証を必須化する。パスワードは環境変数 XNEWSBOT_ADMIN_PASSWORD。
未設定ならアクセスを拒否(無認証で APIキー管理画面を晒さない)。
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _DIR / "templates"
STATIC_DIR = _DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

_security = HTTPBasic()


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> str:
    """Basic 認証。XNEWSBOT_ADMIN_PASSWORD と一致したパスワードのみ通す(ユーザー名は任意)。"""
    password = os.environ.get("XNEWSBOT_ADMIN_PASSWORD", "")
    if not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="XNEWSBOT_ADMIN_PASSWORD が未設定です。管理UIは認証なしでは使えません。",
        )
    if not secrets.compare_digest(credentials.password, password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証に失敗しました。",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
