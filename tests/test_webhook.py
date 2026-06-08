"""Webhook の署名検証・イベント正規化・配線を検証(DBはインメモリ・LINE送信はフェイク)。"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from xnewsbot.api import line_webhook
from xnewsbot.api.main import app
from xnewsbot.config import Settings
from xnewsbot.models import Subscriber

SECRET = "testsecret"


class FakeLineMessenger:
    last: "FakeLineMessenger | None" = None

    def __init__(self, *_a, **_k):
        self.replies = []
        FakeLineMessenger.last = self

    def reply(self, token, specs):
        self.replies.append((token, specs))

    def push(self, to, specs):
        pass


def _sign(body: str) -> str:
    return base64.b64encode(
        hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).digest()
    ).decode()


@pytest.fixture
def wired(monkeypatch):
    # StaticPool: 単一の in-memory 接続を全スレッドで共有(webhook は別スレッドで処理される)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    sess = Session(engine)

    @contextlib.contextmanager
    def fake_session():
        yield sess

    dummy = Settings(line_channel_secret=SECRET, line_channel_access_token="tok")
    monkeypatch.setattr(line_webhook, "get_settings", lambda: dummy)
    monkeypatch.setattr(line_webhook, "get_session", fake_session)
    monkeypatch.setattr(line_webhook, "LineMessenger", FakeLineMessenger)
    monkeypatch.setattr(line_webhook, "make_deliver_now", lambda settings: (lambda sub: None))
    return sess


def _follow_body(uid="U1") -> str:
    return json.dumps({
        "destination": "x",
        "events": [{
            "type": "follow", "follow": {"isUnblocked": False},
            "mode": "active", "timestamp": 1,
            "source": {"type": "user", "userId": uid},
            "replyToken": "tok", "webhookEventId": "e1",
            "deliveryContext": {"isRedelivery": False},
        }],
    })


def test_valid_signature_runs_onboarding(wired):
    body = _follow_body()
    with TestClient(app) as c:
        r = c.post("/line/callback", content=body,
                   headers={"X-Line-Signature": _sign(body)})
    assert r.status_code == 200
    sub = wired.exec(select(Subscriber).where(Subscriber.line_user_id == "U1")).first()
    assert sub is not None and sub.onboarding_step == "genres"
    assert FakeLineMessenger.last.replies  # welcome 等が返信された


def test_invalid_signature_400(wired):
    body = _follow_body()
    with TestClient(app) as c:
        r = c.post("/line/callback", content=body,
                   headers={"X-Line-Signature": "wrong"})
    assert r.status_code == 400
