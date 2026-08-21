"""管理UIの共有部品: Jinja2 テンプレートと認証(Basic + 永続クッキー + 総当たり対策)。

公開(Tailscale Funnel)するため全ルートに認証を必須化する。パスワードは環境変数 XNEWSBOT_ADMIN_PASSWORD。
未設定ならアクセスを拒否(無認証で APIキー管理画面を晒さない)。

一度ログインした端末には毎回パスワードを求めないよう、Basic 認証が通ったら永続クッキーを焼き、
以後はそのクッキーで素通しする。クッキー値はパスワード由来の不可逆トークン(HMAC)なので
平文パスワードは保存されず、パスワードを変えれば既存クッキーは自動的に失効する。

公開時の唯一の防壁がパスワードなので、同一IPからの連続失敗を数えて一定回数で一時ロックし、
短いパスワードでも総当たりを事実上不能にする(正規利用は初回成功→クッキーで以後素通しのため無影響)。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = _DIR / "templates"
STATIC_DIR = _DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# tailscale serve/funnel が TLS を終端し http で uvicorn に渡すため Secure は付けない(HttpOnly + 認証で保護)。
COOKIE_NAME = "xnb_auth"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1年: 端末に一度入れたら以後パスワードを求めない

# 総当たり対策: 直近 WINDOW 秒で MAX 回失敗したIPを LOCK 秒ロック(その間は正解でも拒否)。
_FAIL_MAX = 10
_FAIL_WINDOW = 600
_LOCK_SECONDS = 600
# 記録するIPの上限。大量のIP(偽装ヘッダ・ボットネット)で辞書が無制限に膨らむのを防ぐ。
_FAIL_IP_MAX = 1000
_fails: dict[str, list[float]] = defaultdict(list)

# 既定では X-Forwarded-For を信用しない(クライアントが自由に付けられるため)。
# 前段で tailscale serve/funnel 等が TLS を終端する構成のときだけ 1 にする。
TRUST_PROXY_ENV = "XNEWSBOT_TRUST_PROXY"

_security = HTTPBasic(auto_error=False)


def _admin_password() -> str:
    return os.environ.get("XNEWSBOT_ADMIN_PASSWORD", "")


def remember_token(password: str) -> str:
    """パスワード由来の不可逆トークン。クッキーに入れる値(パスワード変更で自動失効)。"""
    return hmac.new(password.encode(), b"xnewsbot-admin-remember", hashlib.sha256).hexdigest()


def _client_ip(request: Request) -> str:
    """ロックの単位に使う実クライアントIP。

    X-Forwarded-For はクライアントが自由に詐称でき、直アクセス(LAN)では偽装IPごとに
    失敗カウントが分散してロックが無効化されるため既定では見ない。前段に信頼できる
    プロキシが居る構成(XNEWSBOT_TRUST_PROXY=1)のときだけ、そのプロキシが末尾に足す
    **右端**の値を実クライアントとして使う(左側はクライアント由来で信用できない)。
    """
    if os.environ.get(TRUST_PROXY_ENV, "").strip().lower() in ("1", "true", "yes"):
        parts = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "?"


def _locked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < _FAIL_WINDOW]
    if recent:
        _fails[ip] = recent
    else:
        _fails.pop(ip, None)  # 期限切れのエントリは残さない(辞書を膨らませない)
    return len(recent) >= _FAIL_MAX


def _record_fail(ip: str) -> None:
    """失敗を1件記録する。上限を超えたら期限切れ→最古の順に捨てて件数を抑える。"""
    if ip not in _fails and len(_fails) >= _FAIL_IP_MAX:
        now = time.time()
        for stale in [k for k, v in _fails.items() if not v or now - v[-1] >= _FAIL_WINDOW]:
            _fails.pop(stale, None)
        while len(_fails) >= _FAIL_IP_MAX:
            _fails.pop(min(_fails, key=lambda k: _fails[k][-1] if _fails[k] else 0.0), None)
    _fails[ip].append(time.time())


def require_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """Basic 認証 or 永続クッキーで通す。Basic が通った端末には以後用のクッキーを焼く
    (実際のクッキー付与は main.py のミドルウェアが request.state.set_remember を見て行う)。
    連続失敗IPは一時ロックして総当たりを防ぐ。"""
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
    # 2) ロック中IPは正解でも拒否(総当たり遮断)
    ip = _client_ip(request)
    if _locked(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="試行回数が多すぎます。しばらく待ってから再試行してください。",
            headers={"Retry-After": str(_LOCK_SECONDS)},
        )
    # 3) Basic 認証が正しければ通し、失敗カウントを消し、以後のためにクッキーを焼く印を付ける
    if credentials and secrets.compare_digest(credentials.password, password):
        _fails.pop(ip, None)
        request.state.set_remember = token
        return credentials.username or "admin"
    # 4) それ以外は失敗を記録して Basic チャレンジ
    _record_fail(ip)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="認証に失敗しました。",
        headers={"WWW-Authenticate": "Basic"},
    )
