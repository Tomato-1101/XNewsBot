"""管理Web UI のルート/設定保存のテスト(DBはインメモリ、実ファイル/実launchd/実キーには触れない)。"""

from __future__ import annotations

import contextlib
import plistlib
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from xnewsbot.admin import news, stores
from xnewsbot.admin.main import app
from xnewsbot.models import GenreDigest, NewsItem

AUTH = ("admin", "testpw")

PLIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tomato.xnewsbot-deliver</string>
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
        <dict>
            <key>Hour</key><integer>20</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
    </array>
</dict>
</plist>
"""

DELIVER_SH = 'MORNING_HHMM="0800"\nEVENING_HHMM="2100"\n'


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("XNEWSBOT_ADMIN_PASSWORD", "testpw")
    # 設定ファイルは全て tmp に向ける(実ファイルを汚さない)
    monkeypatch.setattr(stores, "KEY_FILE", tmp_path / ".key")
    monkeypatch.setattr(stores, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(stores, "GENRES_FILE", tmp_path / "genres.toml")
    monkeypatch.setattr(stores, "DELIVER_SH", tmp_path / "deliver.sh")
    monkeypatch.setattr(stores, "REPO_PLIST", tmp_path / "repo.plist")
    monkeypatch.setattr(stores, "INSTALLED_PLIST", tmp_path / "installed.plist")
    monkeypatch.setattr(stores, "reload_deliver_agent", lambda: None)  # 実 launchd に触れない

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    sess = Session(engine)

    @contextlib.contextmanager
    def fake_session():
        yield sess

    monkeypatch.setattr(news, "get_session", fake_session)
    return SimpleNamespace(session=sess, tmp=tmp_path)


# --- 認証 ---

def test_requires_auth(wired):
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("admin", "wrong")).status_code == 401
        assert c.get("/", auth=AUTH).status_code == 200


def test_refuses_start_without_password(monkeypatch, tmp_path):
    """パスワードが環境変数にも .env にも無ければ起動を拒否する。"""
    monkeypatch.delenv("XNEWSBOT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(stores, "ENV_FILE", tmp_path / ".env")  # 空(存在しない)
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


# --- ニュース閲覧 ---

def test_news_view_shows_items(wired):
    gd = GenreDigest(digest_date=date(2026, 6, 15), slot="morning", genre="AI")
    wired.session.add(gd)
    wired.session.commit()
    wired.session.refresh(gd)
    wired.session.add(NewsItem(
        genre_digest_id=gd.id, genre="AI", genres=["AI"], importance="big", rank=0,
        title="重大ニュースの見出しABC", summary="要約サマリDEF", detail="詳細ボディGHI",
        source_urls=["https://x.com/u/status/1"], source_tweets=[], top_view_count=12345,
    ))
    wired.session.commit()

    with TestClient(app) as c:
        r = c.get("/?date_str=2026-06-15&slot=morning", auth=AUTH)
    assert r.status_code == 200
    assert "重大ニュースの見出しABC" in r.text
    assert "要約サマリDEF" in r.text


# --- ジャンル(genres.toml) ---

def test_genres_invalid_toml_not_saved(wired):
    with TestClient(app) as c:
        r = c.post("/manage/genres", data={"toml_text": "これは TOML ではない {{{"}, auth=AUTH)
    assert r.status_code == 200  # リダイレクト後の /manage
    assert "失敗" in r.text
    assert not stores.GENRES_FILE.exists()  # 不正なら書き込まれない


def test_genres_valid_toml_saved(wired):
    text = '[[genre]]\nkey = "テスト"\nkeywords = ["a"]\n'
    with TestClient(app) as c:
        r = c.post("/manage/genres", data={"toml_text": text}, auth=AUTH)
    assert r.status_code == 200
    assert stores.GENRES_FILE.read_text(encoding="utf-8") == text


def test_validate_genres_rejects_duplicate_key():
    with pytest.raises(ValueError):
        stores.validate_genres_toml('[[genre]]\nkey="X"\n[[genre]]\nkey="X"\n')


# --- APIキー(.key) ---

def test_keys_add_reorder_delete_and_mask(wired):
    secret = "abcd1234secretXYZ"
    with TestClient(app) as c:
        c.post("/manage/keys", data={"action": "add", "value": secret}, auth=AUTH)
        c.post("/manage/keys", data={"action": "add", "value": "BBBBBBBBBBBB"}, auth=AUTH)
        assert stores.read_keys() == [secret, "BBBBBBBBBBBB"]

        c.post("/manage/keys", data={"action": "up", "index": 1}, auth=AUTH)
        assert stores.read_keys() == ["BBBBBBBBBBBB", secret]

        # マスク表示で、生キーは HTML に出ない
        page = c.get("/manage", auth=AUTH).text
        assert "abcd…tXYZ" in page
        assert secret not in page

        c.post("/manage/keys", data={"action": "delete", "index": 0}, auth=AUTH)
        assert stores.read_keys() == [secret]


# --- 収集パラメータ(.env) ---

def test_collect_params_preserve_other_lines(wired):
    stores.ENV_FILE.write_text(
        "# コメント\nLINE_CHANNEL_ACCESS_TOKEN=secret-token\nCOLLECT_MAX_TWEETS=100\n",
        encoding="utf-8",
    )
    with TestClient(app) as c:
        c.post("/manage/collect", data={
            "COLLECT_MAX_TWEETS": "250", "COLLECT_HOURS": "12",
            "COLLECT_MIN_FAVES": "300", "COLLECT_MIN_VIEWS_FLOOR": "40000",
        }, auth=AUTH)
    out = stores.ENV_FILE.read_text(encoding="utf-8")
    assert "# コメント" in out
    assert "LINE_CHANNEL_ACCESS_TOKEN=secret-token" in out  # 秘密は保持・変更しない
    assert "COLLECT_MAX_TWEETS=250" in out
    assert "COLLECT_HOURS=12" in out
    assert "COLLECT_MAX_TWEETS=100" not in out


def test_collect_params_reject_non_numeric(wired):
    stores.ENV_FILE.write_text("COLLECT_MAX_TWEETS=100\n", encoding="utf-8")
    with TestClient(app) as c:
        r = c.post("/manage/collect", data={"COLLECT_MAX_TWEETS": "abc"}, auth=AUTH)
    assert "不正" in r.text
    assert "COLLECT_MAX_TWEETS=100" in stores.ENV_FILE.read_text(encoding="utf-8")


# --- 配信時刻(deliver.sh + plist) ---

def test_delivery_time_updates_files(wired):
    stores.DELIVER_SH.write_text(DELIVER_SH, encoding="utf-8")
    stores.INSTALLED_PLIST.write_text(PLIST_XML, encoding="utf-8")
    stores.REPO_PLIST.write_text(PLIST_XML, encoding="utf-8")

    with TestClient(app) as c:
        r = c.post("/manage/delivery", data={"slot": "morning", "hhmm": "07:30"}, auth=AUTH)
    assert r.status_code == 200

    assert 'MORNING_HHMM="0730"' in stores.DELIVER_SH.read_text(encoding="utf-8")
    assert 'EVENING_HHMM="2100"' in stores.DELIVER_SH.read_text(encoding="utf-8")
    pl = plistlib.loads(stores.INSTALLED_PLIST.read_bytes())
    intervals = pl["StartCalendarInterval"]
    assert intervals[0] == {"Hour": 7, "Minute": 15}   # 定刻07:30の15分前
    assert intervals[1] == {"Hour": 20, "Minute": 45}  # 夜は変更なし


def test_delivery_time_rejects_bad_time(wired):
    stores.DELIVER_SH.write_text(DELIVER_SH, encoding="utf-8")
    stores.INSTALLED_PLIST.write_text(PLIST_XML, encoding="utf-8")
    with TestClient(app) as c:
        r = c.post("/manage/delivery", data={"slot": "morning", "hhmm": "99:99"}, auth=AUTH)
    assert "失敗" in r.text
    assert 'MORNING_HHMM="0800"' in stores.DELIVER_SH.read_text(encoding="utf-8")  # 変更前のまま
