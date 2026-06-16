"""管理UIの共有部品: Jinja2 テンプレートと認証(Basic + 永続クッキー)。

LAN/tailnet 公開するため全ルートに認証を必須化する。パスワードは環境変数 XNEWSBOT_ADMIN_PASSWORD。
未設定ならアクセスを拒否(無認証で APIキー管理画面を晒さない)。

一度ログインした端末には毎回パスワードを求めないよう、Basic 認証が通ったら永続クッキーを焼き、
以後はそのクッキーで素通しする。クッキー値はパスワード由来の不可逆トークン(HMAC)なので
平文パスワードは保存されず、パスワードを変えれば既存クッキーは自動的に失効する。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _DIR / "templates"
STATIC_DIR = _DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# tailscale serve が TLS を終端し http で uvicorn に渡すため Secure は付けない(tailnet 限定 + HttpOnly)。
COOKIE_NAME = "xnb_auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1年: 端末に一度入れたら以後パスワードを求めない

_security = HTTPBasic(auto_error=False)


def _admin_password() -> str:
    return os.environ.get("XNEWSBOT_ADMIN_PASSWORD", "")


def remember_token(password: str) -> str:
    """パスワード由来の不可逆トークン。クッキーに入れる値(パスワード変更で自動失効)。"""
    return hmac.new(password.encode(), b"xnewsbot-admin-remember", hashlib.sha256).hexdigest()


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """Basic 認証 or 永続クッキーで通す。Basic が通った端末には以後用のクッキーを焼く
    (実際のクッキー付与は main.py のミドルウェアが request.state.set_remember を見て行う)。"""
    password = _admin_password()
    if not password:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="XNEWSBOT_ADMIN_PASSWORD が未設定です。管理UIは認証なしでは使えません。",
        )
    token = remember_token(password)
    # 1) 既ログイン端末はクッキーで素通し(毎回パスワードを求めない)
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie and secrets.compare_digest(cookie, token):
        return "admin"
    # 2) Basic 認証が正しければ通し、以後のためにクッキーを焼くよう印を付ける
    if credentials and secrets.compare_digest(credentials.password, password):
        request.state.set_remember = token
        return credentials.username or "admin"
    # 3) それ以外は Basic チャレンジ
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="認証に失敗しました。",
        headers={"WWW-Authenticate": "Basic"},
    )
