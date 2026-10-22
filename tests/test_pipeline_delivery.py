"""送信失敗を成功にしないことと、本人向け障害通知の日次重複排除。"""
import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from xnewsbot.models import Subscriber

spec = importlib.util.spec_from_file_location("pipeline_delivery", Path(__file__).parents[1] / "scripts/pipeline.py")
pl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pl)


def setup_pipeline(monkeypatch, session, messenger):
    @contextmanager
    def get_session():
        yield session
    monkeypatch.setattr(pl, "get_session", get_session)
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace(
        line_channel_access_token="fake", default_tz="Asia/Tokyo"))
    monkeypatch.setattr(pl.lc, "LineMessenger", lambda _: messenger)
    session.add(Subscriber(line_user_id="test-user", enabled_genres=["AI"],
                           is_onboarded=True, push_to="test-group"))
    session.commit()


def test_due_push_failure_is_nonzero(monkeypatch, session):
    setup_pipeline(monkeypatch, session, object())
    def fail(*args, **kwargs):
        raise ConnectionError("DNS lookup failed")
    monkeypatch.setattr(pl, "deliver_to_subscriber", fail)
    with pytest.raises(SystemExit) as exc:
        pl.cmd_push(SimpleNamespace(user=None, due=True, slot="morning", date=None))
    assert exc.value.code == 1
    assert session.get(Subscriber, 1).last_morning_on is None


class Alerts:
    def __init__(self):
        self.calls = []
        self.fail = False

    def push(self, to, specs, *, retry_key=None):
        self.calls.append((to, specs, retry_key))
        if self.fail:
            raise ConnectionError("DNS lookup failed")


def test_alert_once_per_day_to_person(monkeypatch, session):
    messenger = Alerts()
    setup_pipeline(monkeypatch, session, messenger)
    args = SimpleNamespace(text="理由: Claude週間上限")
    pl.cmd_alert(args)
    pl.cmd_alert(args)
    assert len(messenger.calls) == 1
    assert messenger.calls[0][0] == "test-user"
    assert len(messenger.calls[0][1]) == 1
    assert messenger.calls[0][2]


def test_alert_network_failure_can_retry_with_same_key(monkeypatch, session):
    messenger = Alerts()
    messenger.fail = True
    setup_pipeline(monkeypatch, session, messenger)
    args = SimpleNamespace(text="理由: DNS障害")
    with pytest.raises(SystemExit) as exc:
        pl.cmd_alert(args)
    assert exc.value.code == 1
    messenger.fail = False
    pl.cmd_alert(args)
    pl.cmd_alert(args)
    assert len(messenger.calls) == 2
    assert messenger.calls[0][2] == messenger.calls[1][2]


def test_alert_already_accepted_retry_is_success(monkeypatch, session):
    class AlreadyAccepted:
        def push(self, *args, **kwargs):
            error = RuntimeError("retry key already accepted")
            error.status = 409
            raise error
    setup_pipeline(monkeypatch, session, AlreadyAccepted())
    pl.cmd_alert(SimpleNamespace(text="理由: 再試行"))
    row = session.get(pl.DeliveryAlert, f"{pl._today(pl.get_settings())}:test-user")
    assert row.sent


def test_alert_recovers_interrupted_reservation(monkeypatch, session):
    messenger = Alerts()
    setup_pipeline(monkeypatch, session, messenger)
    key = f"{pl._today(pl.get_settings())}:test-user"
    session.add(pl.DeliveryAlert(key=key, sent=False))
    session.commit()
    pl.cmd_alert(SimpleNamespace(text="理由: 前回途中で停止"))
    assert len(messenger.calls) == 1
    assert session.get(pl.DeliveryAlert, key).sent
