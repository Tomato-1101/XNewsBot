"""FastAPI バックエンド。LINE Webhook 受信を担う常駐サーバ。

定刻配信(収集→Claudeキュレーション→送信)は launchd の ops/deliver.sh がその時刻に行うため、
本サーバ内のスケジューラ(tick)は既定で無効(settings.scheduler_enabled=False)。
「今すぐ配信」は webhook 受信時に deliver.sh を個人向けに別プロセス起動する(scheduler.make_deliver_now)。

流用元: XAgent/xagent/api/main.py の lifespan + BackgroundScheduler パターン。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .. import __version__
from ..config import get_settings
from ..db import init_db
from .line_webhook import router as line_router

log = logging.getLogger("xnewsbot.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings = get_settings()
    sched = None
    if settings.scheduler_enabled:
        from apscheduler.schedulers.background import BackgroundScheduler

        from ..scheduler import tick

        sched = BackgroundScheduler(timezone="UTC")
        # 配信の発火点検。catch-up方式(時刻超過&当日未配信を検出)なので間隔発火で十分。
        sched.add_job(
            tick, "interval",
            seconds=settings.scheduler_interval_seconds,
            id="deliver", max_instances=1, coalesce=True,
        )
        sched.start()
        log.info("配信スケジューラを起動 (interval=%ss)", settings.scheduler_interval_seconds)
    try:
        yield
    finally:
        if sched is not None:
            sched.shutdown(wait=False)


app = FastAPI(title="XNewsBot", version=__version__, lifespan=lifespan)
app.include_router(line_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}
