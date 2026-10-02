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

from xnewsbot import keychain_env
from xnewsbot.admin import news, stores, web
from xnewsbot.admin.main import app
from xnewsbot.models import GenreDigest, NewsItem, Subscriber, XUsageSnapshot

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
    monkeypatch.setattr(keychain_env, "load", lambda: None)  # 実 Keychain には触れない
    # 設定ファイルは全て tmp に向ける(実ファイルを汚さない)
    monkeypatch.setattr(stores, "KEY_FILE", tmp_path / ".key")
    monkeypatch.setattr(stores, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(stores, "GENRES_FILE", tmp_path / "genres.toml")
    monkeypatch.setattr(stores, "DELIVER_SH", tmp_path / "deliver.sh")
    monkeypatch.setattr(stores, "REPO_PLIST", tmp_path / "repo.plist")
    monkeypatch.setattr(stores, "INSTALLED_PLIST", tmp_path / "installed.plist")
    monkeypatch.setattr(stores, "reload_deliver_agent", lambda: None)  # 実 launchd に触れない
    web._fails.clear()  # 総当たりカウンタをテスト間で持ち越さない

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    sess = Session(engine)

    @contextlib.contextmanager
    def fake_session():
        yield sess

    monkeypatch.setattr(news, "get_session", fake_session)
    # LINE の残り通数は実 API に取りに行かない(既定は取得できない扱い)
    monkeypatch.setattr(news, "_fetch_line_quota", lambda session: None)
    monkeypatch.setattr(news, "_line_cache", {"at": float("-inf"), "value": None})
    return SimpleNamespace(session=sess, tmp=tmp_path)


# --- 認証 ---

def test_requires_auth(wired):
    with TestClient(app) as c:
        assert c.get("/").status_code == 401
        assert c.get("/", auth=("admin", "wrong")).status_code == 401
        assert c.get("/", auth=AUTH).status_code == 200


def test_refuses_start_without_password(monkeypatch, tmp_path):
    """パスワードが環境変数にも Keychain にも .env にも無ければ起動を拒否する。"""
    monkeypatch.delenv("XNEWSBOT_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(keychain_env, "load", lambda: None)  # 実 Keychain には触れない
    monkeypatch.setattr(stores, "ENV_FILE", tmp_path / ".env")  # 空(存在しない)
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_cookie_remembers_device_after_basic(wired):
    """Basic 認証が通った端末には永続クッキーを焼き、以後はパスワード無しで素通しする。"""
    with TestClient(app) as c:
        r = c.get("/manage", auth=AUTH)
        assert r.status_code == 200
        assert "xnb_auth" in r.cookies  # ログイン成功でクッキーが焼かれる
        # 以後は Basic ヘッダ無し(クッキーのみ)で 200 = 毎回パスワードを求めない
        assert c.get("/manage").status_code == 200


def test_bad_cookie_is_rejected(wired):
    """偽造クッキーは通さない(Basic も無ければ 401)。"""
    with TestClient(app) as c:
        c.cookies.set("xnb_auth", "deadbeef")
        assert c.get("/manage").status_code == 401


def test_repeated_failures_lock_out_ip(wired):
    """同一IPの連続失敗で一時ロック(429)。IPはソケットの接続元で数える
    (X-Forwarded-For は詐称できるため既定では見ない=偽装でロックを回避できない)。"""
    with TestClient(app, client=("203.0.113.9", 51000)) as c:
        for _ in range(web._FAIL_MAX):
            assert c.get("/manage", auth=("admin", "wrong")).status_code == 401
        # ロック後は正しいパスワードでも 429。XFF を付け替えても回避できない
        assert c.get("/manage", auth=AUTH).status_code == 429
        assert c.get("/manage", auth=AUTH,
                     headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 429
    # 別IP(別の接続元)は影響を受けない
    with TestClient(app, client=("198.51.100.1", 51001)) as other:
        assert other.get("/manage", auth=AUTH).status_code == 200


def test_trusted_proxy_uses_rightmost_forwarded_for(wired, monkeypatch):
    """信頼プロキシ構成(XNEWSBOT_TRUST_PROXY=1)では XFF の右端=プロキシが付けた値で数える。"""
    monkeypatch.setenv(web.TRUST_PROXY_ENV, "1")
    with TestClient(app, client=("127.0.0.1", 51002)) as c:
        for _ in range(web._FAIL_MAX):
            assert c.get("/manage", auth=("admin", "wrong"),
                         headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}).status_code == 401
        # 左側(クライアント由来)を差し替えてもロックは外れない
        assert c.get("/manage", auth=AUTH,
                     headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.9"}).status_code == 429
        # 右端が別IPなら影響を受けない
        assert c.get("/manage", auth=AUTH,
                     headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.7"}).status_code == 200


# --- 今すぐ実行(配信 / 更新) ---

def test_run_now_launches_delivery(wired, monkeypatch):
    """今すぐ配信ボタン: deliver.sh を now モードで起動し、開始メッセージを表示する。"""
    calls: list[str] = []
    monkeypatch.setattr(stores, "launch_deliver", lambda mode: calls.append(mode))
    with TestClient(app) as c:
        r = c.post("/manage/run", data={"mode": "now"}, auth=AUTH)
    assert r.status_code == 200  # PRG 後の /manage
    assert calls == ["now"]
    assert "今すぐ配信を開始" in r.text


def test_run_refresh_launches_without_push(wired, monkeypatch):
    """今すぐ更新ボタン: refresh モードで起動し、LINE送信なしと案内する。"""
    calls: list[str] = []
    monkeypatch.setattr(stores, "launch_deliver", lambda mode: calls.append(mode))
    with TestClient(app) as c:
        r = c.post("/manage/run", data={"mode": "refresh"}, auth=AUTH)
    assert r.status_code == 200
    assert calls == ["refresh"]
    assert "LINE へは送信しません" in r.text


def test_run_rejects_unknown_mode(wired, monkeypatch):
    """未知のモードは起動しない(誤った送信を防ぐ)。"""
    calls: list[str] = []
    monkeypatch.setattr(stores, "launch_deliver", lambda mode: calls.append(mode))
    with TestClient(app) as c:
        r = c.post("/manage/run", data={"mode": "bogus"}, auth=AUTH)
    assert calls == []
    assert "不正なモード" in r.text


def test_launch_deliver_validates(wired):
    """launch_deliver は不正モードを弾き、deliver.sh が無ければ実プロセスを起こさず例外。"""
    with pytest.raises(ValueError):
        stores.launch_deliver("bogus")
    with pytest.raises(FileNotFoundError):  # wired は DELIVER_SH を存在しない tmp に向けている
        stores.launch_deliver("now")


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


def test_news_view_shows_items_of_removed_genre(wired):
    """廃止したジャンル(2026-10-02 の特大)の過去記事も、DB のジャンル名のまま落ちずに出る。"""
    for genre, title in (("AI", "AIの見出しXYZ"), ("特大", "過去の特大見出しQRS")):
        gd = GenreDigest(digest_date=date(2026, 9, 30), slot="morning", genre=genre)
        wired.session.add(gd)
        wired.session.commit()
        wired.session.refresh(gd)
        wired.session.add(NewsItem(
            genre_digest_id=gd.id, genre=genre, genres=[genre], importance="big", rank=0,
            title=title, summary="要約", source_urls=[], source_tweets=[], top_view_count=1,
        ))
    wired.session.commit()

    with TestClient(app) as c:
        r = c.get("/?date_str=2026-09-30&slot=morning", auth=AUTH)
    assert r.status_code == 200
    assert "AIの見出しXYZ" in r.text and "過去の特大見出しQRS" in r.text
    assert r.text.index("AIの見出しXYZ") < r.text.index("過去の特大見出しQRS")  # 廃止ジャンルは後ろ


def test_news_view_shows_usage(wired, monkeypatch):
    for d, used, remaining in [(date(2026, 9, 30), 9000, 3_020_000), (date(2026, 10, 1), 11000, 3_009_000)]:
        wired.session.add(XUsageSnapshot(digest_date=d, slot="morning", used=used, remaining=remaining))
    wired.session.add(Subscriber(line_user_id="U1", enabled_genres=["AI"], is_onboarded=True,
                                 push_to="Cgroup"))
    wired.session.commit()
    monkeypatch.setattr(news, "_fetch_line_quota",
                        lambda session: {"limit": 200, "used": 150, "costs": {"Cgroup": 3}})

    with TestClient(app) as c:
        r = c.get("/", auth=AUTH)
    assert r.status_code == 200
    assert "残り 3,009,000 クレジット" in r.text
    assert "あと約<b>300</b>日" in r.text          # 3,009,000 ÷ 平均10,000
    assert "残り<b>50</b>通" in r.text
    assert "足りません" in r.text or "足ります" in r.text


def test_line_usage_counts_runs_to_month_end(monkeypatch, wired):
    """今日を数えるかは時刻ではなく、購読者ごとの配信済み記録(last_morning_on)で決める。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    sub = Subscriber(line_user_id="U1", enabled_genres=["AI"], is_onboarded=True, push_to="Cgroup")
    wired.session.add(sub)
    wired.session.commit()
    monkeypatch.setattr(news, "_fetch_line_quota",
                        lambda session: {"limit": 200, "used": 140, "costs": {"Cgroup": 3}})
    tz = ZoneInfo("Asia/Tokyo")
    # 10:00 でも今日の分が未配信(遅延・自動復旧待ち)なら今日も数える
    before = news._line_usage(wired.session, datetime(2026, 10, 30, 10, 0, tzinfo=tz))
    assert (before["remaining"], before["days_left"], before["need"], before["enough"]) == (60, 2, 6, True)
    assert before["cost"] == 3
    sub.last_morning_on = date(2026, 10, 30)
    wired.session.commit()
    after = news._line_usage(wired.session, datetime(2026, 10, 30, 7, 0, tzinfo=tz))
    assert (after["days_left"], after["need"]) == (1, 3)  # 今日の朝の配信は済み(キャッシュ中でも反映)


def test_line_usage_refetches_when_new_target_appears(monkeypatch, wired):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    calls = []

    def fetch(session):
        calls.append(1)
        return {"limit": 200, "used": 0, "costs": {"U1": 1, "U2": 1}} if len(calls) > 1 else \
            {"limit": 200, "used": 0, "costs": {"U1": 1}}
    monkeypatch.setattr(news, "_fetch_line_quota", fetch)
    wired.session.add(Subscriber(line_user_id="U1", enabled_genres=["AI"], is_onboarded=True))
    wired.session.commit()
    now = datetime(2026, 10, 30, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert news._line_usage(wired.session, now)["cost"] == 1
    wired.session.add(Subscriber(line_user_id="U2", enabled_genres=["AI"], is_onboarded=True))
    wired.session.commit()
    assert news._line_usage(wired.session, now)["cost"] == 2 and len(calls) == 2


def test_manage_shows_usage_panel(wired):
    with TestClient(app) as c:
        r = c.get("/manage", auth=AUTH)
    assert r.status_code == 200
    assert "残り使用量" in r.text and "LINE から取得できませんでした" in r.text


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


def test_collect_params_reject_below_minimum(wired):
    """0や負値は収集を無音で壊す(0件で配信中止・期間制限の消失)ので保存しない。"""
    stores.ENV_FILE.write_text("COLLECT_MAX_TWEETS=100\nCOLLECT_HOURS=24\n", encoding="utf-8")
    with TestClient(app) as c:
        assert c.post("/manage/collect", data={"COLLECT_MAX_TWEETS": "0"}, auth=AUTH,
                      follow_redirects=False).status_code == 303
        assert c.post("/manage/collect", data={"COLLECT_HOURS": "0"}, auth=AUTH,
                      follow_redirects=False).status_code == 303
    out = stores.ENV_FILE.read_text(encoding="utf-8")
    assert "COLLECT_MAX_TWEETS=100" in out and "COLLECT_HOURS=24" in out  # 元のまま


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


# --- AI解説(管理UI) ---

@pytest.fixture
def no_quiet(monkeypatch):
    """実行時刻が 07:00〜08:30 でもテストが揺れないよう、時間帯の制限を外す。"""
    from xnewsbot import explain
    monkeypatch.setattr(explain, "in_quiet_hours", lambda now: False)

def _explain_item(wired) -> int:
    gd = GenreDigest(digest_date=date(2026, 6, 15), slot="morning", genre="AI")
    wired.session.add(gd)
    wired.session.commit()
    wired.session.refresh(gd)
    it = NewsItem(genre_digest_id=gd.id, genre="AI", genres=["AI"], importance="big", rank=0,
                  title="解説対象の見出し", summary="要約", detail="詳細",
                  source_urls=["https://example.com/a"], source_tweets=[])
    wired.session.add(it)
    wired.session.commit()
    wired.session.refresh(it)
    return it.id


def test_explain_post_get_flow(wired, monkeypatch, no_quiet):
    from xnewsbot import explain
    item_id = _explain_item(wired)
    started = []
    monkeypatch.setattr(news, "_start_explain", lambda i, t: started.append((i, t)))
    with TestClient(app) as c:
        r = c.post(f"/explain/{item_id}", auth=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "running" and r.json()["elapsed"] == 0
        assert c.get(f"/explain/{item_id}", auth=AUTH).json()["status"] == "running"
        assert c.post(f"/explain/{item_id}", auth=AUTH).json()["status"] == "running"
        assert len(started) == 1                     # 作成中に押し直しても二重に作らない

        # 裏の生成が終わる(claude は呼ばずに差し替え)
        monkeypatch.setattr(explain, "generate", lambda payload: "管理画面で見る解説\n2行目")
        explain.run_job(item_id, started[0][1], session_factory=news.get_session)
        st = c.get(f"/explain/{item_id}", auth=AUTH).json()
        assert st == {"status": "done", "text": "管理画面で見る解説\n2行目", "error": "", "elapsed": None}
        assert c.post(f"/explain/{item_id}", auth=AUTH).json()["status"] == "done"
        assert len(started) == 1

        page = c.get("/?date_str=2026-06-15&slot=morning", auth=AUTH).text
    assert '<div class="explain-out" >管理画面で見る解説\n2行目</div>' in page
    assert '<button type="button" class="explain-btn" hidden>' in page


def test_explain_failed_can_retry(wired, monkeypatch, no_quiet):
    from xnewsbot import explain
    item_id = _explain_item(wired)
    started = []
    monkeypatch.setattr(news, "_start_explain", lambda i, t: started.append(t))

    def boom(payload):
        raise explain.ExplainError("時間切れ(300秒)")
    monkeypatch.setattr(explain, "generate", boom)
    with TestClient(app) as c:
        c.post(f"/explain/{item_id}", auth=AUTH)
        explain.run_job(item_id, started[0], session_factory=news.get_session)
        st = c.get(f"/explain/{item_id}", auth=AUTH).json()
        assert st["status"] == "failed" and "時間切れ" in st["error"]
        assert c.post(f"/explain/{item_id}", auth=AUTH).json()["status"] == "running"
    assert len(started) == 2 and started[0] != started[1]


def test_explain_unknown_item_and_none_state(wired):
    with TestClient(app) as c:
        assert c.post("/explain/999", auth=AUTH).status_code == 404
        assert c.get("/explain/999", auth=AUTH).json()["status"] == "none"


def test_explain_requires_auth(wired, monkeypatch):
    item_id = _explain_item(wired)
    started = []
    monkeypatch.setattr(news, "_start_explain", lambda i, t: started.append(t))
    with TestClient(app) as c:
        assert c.post(f"/explain/{item_id}").status_code == 401
        assert c.get(f"/explain/{item_id}").status_code == 401
        assert c.post(f"/explain/{item_id}", auth=("admin", "wrong")).status_code == 401
    assert not started


def test_explain_refused_in_quiet_hours(wired, monkeypatch):
    from xnewsbot import explain
    item_id = _explain_item(wired)
    started = []
    monkeypatch.setattr(news, "_start_explain", lambda i, t: started.append(t))
    monkeypatch.setattr(explain, "in_quiet_hours", lambda now: True)
    with TestClient(app) as c:
        st = c.post(f"/explain/{item_id}", auth=AUTH).json()
    assert st["status"] == "refused" and "7:00〜8:30" in st["error"]
    assert not started and explain.get(wired.session, item_id) is None


def test_explain_refused_when_two_running(wired, monkeypatch, no_quiet):
    from xnewsbot import explain
    item_id = _explain_item(wired)
    gd = wired.session.get(NewsItem, item_id).genre_digest_id
    others = []
    for r in (1, 2):
        it = NewsItem(genre_digest_id=gd, genre="AI", rank=r, title=f"別記事{r}")
        wired.session.add(it)
        wired.session.commit()
        others.append(it.id)
    for oid in others:
        assert explain.claim(wired.session, oid, title=f"別記事{others.index(oid) + 1}")
    started = []
    monkeypatch.setattr(news, "_start_explain", lambda i, t: started.append(t))
    with TestClient(app) as c:
        st = c.post(f"/explain/{item_id}", auth=AUTH).json()
    assert st["status"] == "refused" and "ほかのAI解説" in st["error"]
    assert not started and explain.get(wired.session, item_id) is None


def test_explain_reused_item_id_is_regenerated(wired, monkeypatch, no_quiet):
    """記事 id が別の記事に使い回されたら、古い解説は出さずに作り直す。"""
    from xnewsbot import explain
    item_id = _explain_item(wired)
    tok = explain.claim(wired.session, item_id, title="前の記事の見出し")
    explain.finish(wired.session, item_id, tok, text="前の記事の解説")
    started = []
    monkeypatch.setattr(news, "_start_explain", lambda i, t: started.append(t))
    with TestClient(app) as c:
        assert c.get(f"/explain/{item_id}", auth=AUTH).json()["status"] == "none"
        page = c.get("/?date_str=2026-06-15&slot=morning", auth=AUTH).text
        assert "前の記事の解説" not in page
        assert c.post(f"/explain/{item_id}", auth=AUTH).json()["status"] == "running"
    assert len(started) == 1
    assert explain.get(wired.session, item_id).title == "解説対象の見出し"


# --- 監視アカウント ---

def test_watch_add_toggle_delete(wired):
    from sqlmodel import select
    from xnewsbot.models import WatchedAccount

    with TestClient(app) as c:
        r = c.post("/manage/watch", data={"action": "add", "handle": " @ClaudeDevs "}, auth=AUTH,
                   follow_redirects=False)
        assert r.status_code == 303 and "ok=watch" in r.headers["location"]
        rows = wired.session.exec(select(WatchedAccount)).all()
        assert [(w.handle, w.enabled) for w in rows] == [("ClaudeDevs", True)]  # @ なし・大小文字は入力どおり
        assert "@ClaudeDevs" in c.get("/manage", auth=AUTH).text

        wid = rows[0].id
        c.post("/manage/watch", data={"action": "toggle", "id": wid}, auth=AUTH)
        wired.session.refresh(rows[0])
        assert rows[0].enabled is False
        assert "@ClaudeDevs（停止中）" in c.get("/manage", auth=AUTH).text

        c.post("/manage/watch", data={"action": "delete", "id": wid}, auth=AUTH)
        assert wired.session.exec(select(WatchedAccount)).all() == []


def test_watch_rejects_duplicate_and_invalid(wired):
    from sqlmodel import select
    from xnewsbot.models import WatchedAccount

    with TestClient(app) as c:
        c.post("/manage/watch", data={"action": "add", "handle": "ClaudeDevs"}, auth=AUTH)
        r = c.post("/manage/watch", data={"action": "add", "handle": "@claudedevs"}, auth=AUTH)
        assert "既に登録されています" in r.text  # 大小文字違いも重複
        for bad in ("", "@", "a-b", "x" * 16, "日本語"):
            r = c.post("/manage/watch", data={"action": "add", "handle": bad}, auth=AUTH)
            assert "英数字と _ の15文字まで" in r.text, bad
        r = c.post("/manage/watch", data={"action": "delete", "id": 999}, auth=AUTH)
        assert "不正な操作" in r.text
        assert len(wired.session.exec(select(WatchedAccount)).all()) == 1


def test_bundled_genres_toml_with_watch_genre_is_valid():
    stores.validate_genres_toml(stores.GENRES_FILE.read_text(encoding="utf-8"))
