"""管理Web UI の FastAPI アプリ(別プロセス・別ポート 8011・LAN 限定)。

LINE Webhook サーバ(xnewsbot.api.main)とは独立。ngrok は 8010 のみトンネルするため、
このアプリは外部公開されない。起動条件として XNEWSBOT_ADMIN_PASSWORD を必須にする。
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..db import init_db
from . import manage, news, stores
from .web import STATIC_DIR

log = logging.getLogger("xnewsbot.admin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 認証パスワードは環境変数優先、無ければ .env(秘密の置き場)から取り込む。
    if not os.environ.get("XNEWSBOT_ADMIN_PASSWORD"):
        pw = stores.get_env_var("XNEWSBOT_ADMIN_PASSWORD")
        if pw:
            os.environ["XNEWSBOT_ADMIN_PASSWORD"] = pw
    if not os.environ.get("XNEWSBOT_ADMIN_PASSWORD"):
        raise RuntimeError(
            "XNEWSBOT_ADMIN_PASSWORD が未設定です。LAN 公開する管理UIを無認証で起動できません。"
            " .env か環境変数に設定してください。"
        )
    init_db()
    yield


app = FastAPI(title="XNewsBot 管理", version=__version__, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(news.router)
app.include_router(manage.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}
