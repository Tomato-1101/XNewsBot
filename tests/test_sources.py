"""取得系(市況 market・本文 articles・pipeline collect の補助関数)の単体テスト。ネットワークは使わない。"""

from __future__ import annotations

import contextlib
import email.message
import importlib.util
import io
import ipaddress
import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from xnewsbot import articles, market
from xnewsbot import newsfeeds as nf
from xnewsbot.models import GenreDigest, NewsItem

# scripts/pipeline.py を import(パッケージ外なのでパス指定でロード)
_PL_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pipeline.py"
_spec = importlib.util.spec_from_file_location("pipeline", _PL_PATH)
pl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pl)


# --- market: Yahoo chart JSON の解析 ---

def _chart(ts, closes, *, start=None, end=None, tz="Asia/Tokyo"):
    meta = {"exchangeTimezoneName": tz, "gmtoffset": 32400}
    if start is not None:
        meta["currentTradingPeriod"] = {"regular": {"start": start, "end": end}}
    return {"chart": {"result": [{"meta": meta, "timestamp": ts,
                                  "indicators": {"quote": [{"close": closes}]}}]}}


# 2026-09-29 / 09-30 / 10-01 の 09:00 JST(=00:00 UTC)の日足
D29, D30, D01 = 1790640000, 1790726400, 1790812800


def test_parse_chart_uses_confirmed_bars():
    """取引時間が終わっていれば最終足は確定終値。前日比はその1本前との差。"""
    data = _chart([D29, D30, D01], [100.0, 110.0, 99.0], start=D01, end=D01 + 6 * 3600)
    last, prev, asof = market.parse_chart(data, now=D01 + 7 * 3600)
    assert (last, prev, asof) == (99.0, 110.0, "2026-10-01")


def test_parse_chart_drops_in_session_bar():
    """取引時間中(now < regular.end)はその日の足が途中値なので捨て、前日を確定終値にする。"""
    data = _chart([D29, D30, D01], [100.0, 110.0, 99.0], start=D01, end=D01 + 6 * 3600)
    last, prev, asof = market.parse_chart(data, now=D01 + 3600)
    assert (last, prev, asof) == (110.0, 100.0, "2026-09-30")


def test_parse_chart_skips_missing_close_and_needs_two_bars():
    data = _chart([D29, D30, D01], [100.0, None, 99.0])
    assert market.parse_chart(data, now=D01 + 86400)[:2] == (99.0, 100.0)  # None の足は飛ばす
    assert market.parse_chart(_chart([D01], [99.0]), now=D01 + 86400) is None
    assert market.parse_chart({"chart": {"result": None}}) is None
    assert market.parse_chart({}) is None


def test_parse_chart_local_date_uses_exchange_timezone():
    """米国市場の足(UTC 13:30 = NY 09:30)は NY の日付で返す。"""
    t1, t2 = 1790775000, 1790861400  # 2026-09-30 / 10-01 13:30 UTC
    data = _chart([t1, t2], [1.0, 2.0], tz="America/New_York")
    assert market.parse_chart(data, now=t2 + 86400)[2] == "2026-10-01"


def test_row_rounding_and_contract_keys():
    r = market._row("TNX", "米10年債利回り", "yield", 5.29312, 5.2551, "2026-09-30")
    assert set(r) == {"key", "label", "close", "change", "change_pct", "asof", "kind"}
    assert (r["close"], r["change"], r["change_pct"]) == (5.293, 0.038, 0.72)
    r = market._row("N225", "日経平均", "index", 68956.7234, 66753.7, "2026-10-01")
    assert (r["close"], r["change"], r["change_pct"]) == (68956.72, 2203.02, 3.3)


def test_fetch_market_skips_failed_symbols(monkeypatch):
    good = _chart([D29, D30], [100.0, 101.0])
    monkeypatch.setattr(market, "_fetch_chart", lambda sym: None if sym == "^DJI" else good)
    monkeypatch.setattr(market, "_fetch_crypto", lambda: None)
    rows = market.fetch_market()
    assert [r["key"] for r in rows] == ["N225", "GSPC", "IXIC", "USDJPY", "TNX"]
    assert {r["kind"] for r in rows} == {"index", "fx", "yield"}
    assert rows[0]["label"] == "日経平均" and rows[0]["change_pct"] == 1.0


def test_fetch_market_all_failed_returns_empty(monkeypatch):
    monkeypatch.setattr(market, "_fetch_chart", lambda sym: None)
    monkeypatch.setattr(market, "_fetch_crypto", lambda: None)
    assert market.fetch_market() == []


# CoinGecko simple/price の実応答(2026-10-01 取得)
_CG = {"bitcoin": {"usd": 83979, "usd_24h_change": 0.08777272612729912},
       "ethereum": {"usd": 2702.05, "usd_24h_change": 0.3190850864792539}}


def test_parse_crypto_latest_and_24h_change():
    rows = market.parse_crypto(_CG, "2026-10-01")
    assert [(r["key"], r["label"], r["kind"]) for r in rows] == [
        ("BTC", "ビットコイン", "crypto"), ("ETH", "イーサリアム", "crypto")]
    assert set(rows[0]) == {"key", "label", "close", "change", "change_pct", "asof", "kind"}
    assert (rows[0]["close"], rows[0]["change_pct"], rows[0]["asof"]) == (83979.0, 0.09, "2026-10-01")
    assert rows[0]["change"] == 73.65   # 24時間前の値(逆算)との差
    # 値の欠けた通貨は入れない
    assert [r["key"] for r in market.parse_crypto({"bitcoin": {"usd": 1}}, "2026-10-01")] == []


def test_fetch_market_appends_crypto_after_indices(monkeypatch):
    good = _chart([D29, D30], [100.0, 101.0])
    monkeypatch.setattr(market, "_fetch_chart", lambda sym: good)
    monkeypatch.setattr(market, "_fetch_crypto", lambda: _CG)
    rows = market.fetch_market()
    assert [r["key"] for r in rows] == ["N225", "GSPC", "IXIC", "DJI", "USDJPY", "TNX", "BTC", "ETH"]


def test_fetch_market_crypto_failure_keeps_other_rows(monkeypatch, capsys):
    monkeypatch.setattr(market, "_fetch_chart", lambda sym: None)
    monkeypatch.setattr(market, "_fetch_crypto", lambda: _CG)
    assert [r["kind"] for r in market.fetch_market()] == ["crypto", "crypto"]
    good = _chart([D29, D30], [100.0, 101.0])
    monkeypatch.setattr(market, "_fetch_chart", lambda sym: good)
    monkeypatch.setattr(market, "_fetch_crypto", lambda: None)
    assert len(market.fetch_market()) == 6
    assert "仮想通貨" in capsys.readouterr().err


# --- articles: 本文取得の対象判定と並列取得 ---

def _news(url, **kw):
    return {"source": "news", "url": url, "text": "t", "body": "", **kw}


def test_is_target_skips_google_redirect_paywall_and_x():
    assert articles.is_target(_news("https://www.itmedia.co.jp/news/a.html"))
    assert not articles.is_target(_news("https://news.google.com/rss/articles/AAA"))
    assert not articles.is_target(_news("https://www.nikkei.com/article/X/"))
    assert not articles.is_target(_news("https://asia.nikkei.com/x"))          # サブドメインも有料扱い
    assert not articles.is_target(_news("https://www.bloomberg.com/news/x"))
    assert not articles.is_target(_news("https://example.com/report.pdf"))
    assert not articles.is_target({"url": "https://x.com/u/status/1", "text": "t"})  # X 投稿は対象外
    assert not articles.is_target(_news("ftp://example.com/a"))


def test_enrich_bodies_fetches_each_url_once(monkeypatch):
    calls = []

    def fake(url):
        calls.append(url)
        return "" if url.endswith("/fail") else f"本文:{url}"

    monkeypatch.setattr(articles, "_fetch_body", fake)
    a1, a2 = _news("https://e.com/a"), _news("https://e.com/a")      # 別ジャンルの同じ記事
    b, f = _news("https://e.com/b"), _news("https://e.com/fail")
    g = _news("https://news.google.com/rss/articles/Z")
    done = _news("https://e.com/done", body="既にある")
    n = articles.enrich_bodies([a1, a2, b, f, g, done], budget_s=10)
    assert n == 3
    assert sorted(calls) == ["https://e.com/a", "https://e.com/b", "https://e.com/fail"]
    assert a1["body"] == a2["body"] == "本文:https://e.com/a"
    assert f["body"] == "" and g["body"] == "" and done["body"] == "既にある"


def test_enrich_bodies_respects_budget(monkeypatch):
    import threading

    gate = threading.Event()

    def slow(url):
        if url.endswith("/slow"):
            gate.wait(5)
            return "遅い本文"
        return "速い本文"

    monkeypatch.setattr(articles, "_fetch_body", slow)
    fast, late = _news("https://e.com/fast"), _news("https://e.com/slow")
    try:
        n = articles.enrich_bodies([fast, late], budget_s=0.5)
    finally:
        gate.set()
    assert n == 1 and fast["body"] == "速い本文" and late["body"] == ""


def test_enrich_bodies_does_not_block_process_exit():
    """budget で戻ったあと、取得中のワーカーがプロセス終了を止めないこと(定刻配信に間に合わせるため)。
    実行中スレッドは Python 終了時に join されるので、別プロセスで実際に終わるまでの時間を測る。"""
    import subprocess
    import sys
    import textwrap
    import time

    budget = 0.5
    code = textwrap.dedent(f"""
        import sys, time, types
        sys.modules.setdefault("trafilatura", types.ModuleType("trafilatura"))  # 重い本物は不要
        from xnewsbot import articles

        def hang(url):
            print("called", flush=True)
            time.sleep(30)  # 応答しない取得先の代わり(ネットワークは使わない)
            return "x"

        articles._fetch_body = hang
        n = articles.enrich_bodies([{{"source": "news", "url": "https://e.com/a", "body": ""}}],
                                   budget_s={budget})
        print("filled", n, flush=True)
    """)
    root = Path(__file__).resolve().parent.parent
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                       timeout=budget + 15)
    elapsed = time.monotonic() - t0
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["called", "filled", "0"]
    assert elapsed < budget + 3


# --- articles: 接続先を公開インターネットに限定(SSRF 対策)・1件の総時間上限 ---

_DNS = {"localhost": ["127.0.0.1", "::1"], "intranet.example": ["10.1.2.3"],
        "mixed.example": ["93.184.216.34", "192.168.0.1"],      # 1つでも内部なら拒否
        "public.example": ["93.184.216.34", "2606:4700::1111"]}


def _fake_dns(monkeypatch):
    """名前解決を表引きに差し替える。表に無い名前は数値表記(IP リテラル)だけ解決し、DNS には出ない。"""
    real = socket.getaddrinfo

    def fake(host, port, *a, **kw):
        if host in _DNS:
            return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                     (ip, 0)) for ip in _DNS[host]]
        return real(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST)

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class _Resp:
    """urlopen の応答の代わり。read1 で chunks を順に返す(delay 秒ずつ待つ)。"""

    def __init__(self, chunks, delay=0.0, ctype="text/html; charset=utf-8"):
        self.headers = {"Content-Type": ctype}
        self._chunks, self._delay = iter(chunks), delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read1(self, n):
        time.sleep(self._delay)
        return next(self._chunks, b"")[:n]


def _fake_open(monkeypatch, resp=None):
    opened: list[str] = []

    def open_(req, timeout=None):
        opened.append(req.full_url)
        if resp is None:
            raise AssertionError("内部宛てに接続してはいけない")
        return resp

    monkeypatch.setattr(articles._OPENER, "open", open_)
    return opened


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://[::1]:18010/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/x", "http://192.168.1.1/", "http://0.0.0.0/", "http://[fe80::1]/",
    "http://[::ffff:127.0.0.1]/", "http://localhost:8010/", "http://intranet.example/",
    "http://mixed.example/", "http://unknown-host.invalid/",
    "http://2130706433/", "http://0x7f000001/", "http://127.1/",   # 10進・16進・省略表記の 127.0.0.1
])
def test_download_refuses_non_public_hosts(monkeypatch, url):
    _fake_dns(monkeypatch)
    opened = _fake_open(monkeypatch)
    assert articles._download(url) is None
    assert articles._fetch_body(url) == ""
    assert opened == []  # 接続前に止まる


def test_download_public_host_reads_in_chunks_up_to_cap(monkeypatch):
    _fake_dns(monkeypatch)
    opened = _fake_open(monkeypatch, _Resp([b"<html>", b"abcd", b"efgh"]))
    assert articles._download("https://public.example/a") == b"<html>abcdefgh"
    assert opened == ["https://public.example/a"]

    monkeypatch.setattr(articles, "_MAX_BYTES", 10)  # 巨大ページは上限で打ち切る
    _fake_open(monkeypatch, _Resp([b"0123", b"4567", b"89ab", b"cdef"]))
    assert articles._download("https://public.example/a") == b"0123456789"

    _fake_open(monkeypatch, _Resp([b"%PDF"], ctype="application/pdf"))
    assert articles._download("https://public.example/a.bin") is None


def test_download_gives_up_when_server_drips(monkeypatch):
    """少しずつ返し続けるサーバでも1件の総時間上限で打ち切る(ソケットのタイムアウトだけでは縛れない)。"""
    _fake_dns(monkeypatch)
    monkeypatch.setattr(articles, "_ITEM_TOTAL_S", 0.3)
    _fake_open(monkeypatch, _Resp(iter(lambda: b"x", None), delay=0.05))  # 無限に1バイトずつ
    t0 = time.monotonic()
    assert articles._download("https://public.example/slow") is None
    assert time.monotonic() - t0 < 1.5


def test_redirect_to_internal_is_refused(monkeypatch):
    _fake_dns(monkeypatch)
    h = articles._PublicOnlyRedirect()
    req = urllib.request.Request("https://public.example/a")
    for bad in ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/", "http://localhost/", "ftp://public.example/x",
                "file:///etc/passwd"]:
        with pytest.raises(urllib.error.URLError):
            h.redirect_request(req, None, 302, "Found", {}, bad)
    ok = h.redirect_request(req, None, 302, "Found", {}, "https://public.example/b")
    assert ok.full_url == "https://public.example/b"


def test_redirect_chain_is_limited_and_checked_each_hop(monkeypatch):
    _fake_dns(monkeypatch)
    h = articles._PublicOnlyRedirect()
    followed: list[str] = []
    h.parent = SimpleNamespace(open=lambda r, timeout=None: followed.append(r.full_url) or "next")

    def hop(location, visited):
        req = urllib.request.Request("https://public.example/start")
        req.timeout = 8  # 通常は OpenerDirector.open が付ける
        req.redirect_dict = {f"https://public.example/{i}": 1 for i in range(visited)}
        hdrs = email.message.Message()
        hdrs["Location"] = location
        return h.http_error_302(req, io.BytesIO(b""), 302, "Found", hdrs)

    assert hop("https://public.example/next", 3) == "next"
    assert followed == ["https://public.example/next"]
    with pytest.raises(urllib.error.URLError):  # 上限(5回)を超えるリダイレクトは辿らない
        hop("https://public.example/next", articles._MAX_REDIRECTS)
    with pytest.raises(urllib.error.URLError):  # 途中の段で内部宛てになったら拒否
        hop("http://169.254.169.254/latest/meta-data/", 1)
    assert followed == ["https://public.example/next"]


# --- articles: 検査と接続を同じ名前解決結果で行う(DNS rebinding 対策) ---
# ここからは _OPENER.open を差し替えず、本物の opener → 接続クラスを通す。
# 名前解決と socket.create_connection・TLS の wrap_socket を差し替えるので外部へは一切繋がない。

def _seq_dns(monkeypatch, table):
    """host ごとに、呼ばれるたび次の答えを返す(最後の答えを繰り返す)。DNS rebinding の再現用。"""
    calls: dict[str, int] = {}

    def fake(host, port, *a, **kw):
        n = calls.get(host, 0)
        calls[host] = n + 1
        ip = table[host][min(n, len(table[host]) - 1)]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


class _FakeSock:
    """create_connection の戻り値の代わり。送られた要求を貯め、決まった HTTP 応答を返す。"""

    def __init__(self, response: bytes):
        self._response, self.sent = response, b""

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode, *a, **kw):
        return io.BytesIO(self._response)

    def setsockopt(self, *a):  # 標準の HTTPConnection.connect が TCP_NODELAY を設定する
        pass

    def close(self):
        pass


def _http(status="200 OK", body=b"<html>ok", **headers):
    head = {"Content-Type": "text/html", "Content-Length": str(len(body)), **headers}
    lines = "".join(f"{k}: {v}\r\n" for k, v in head.items())
    return f"HTTP/1.1 {status}\r\n{lines}\r\n".encode() + body


def _fake_connect(monkeypatch, responses):
    """socket.create_connection を差し替え、渡された宛先を記録する。responses[i] が None なら接続失敗、
    応答が尽きた後の接続はテスト失敗にする。"""
    dialed: list[tuple] = []
    socks: list[_FakeSock] = []
    it, end = iter(responses), object()

    def fake(address, timeout=None, source_address=None, *a, **kw):
        dialed.append(address)
        resp = next(it, end)
        if resp is end:
            raise AssertionError(f"想定外の接続: {address}")
        if resp is None:
            raise ConnectionRefusedError("接続失敗")
        socks.append(_FakeSock(resp))
        return socks[-1]

    monkeypatch.setattr(socket, "create_connection", fake)
    return dialed, socks


def _all_global(dialed):
    return all(ipaddress.ip_address(host).is_global for host, _ in dialed)


def test_dns_rebinding_after_precheck_is_not_dialed(monkeypatch):
    """1回目の解決は公開 IP(事前判定は通る)、2回目以降は 127.0.0.1 を返す DNS でも内部へ繋がない。"""
    calls = _seq_dns(monkeypatch, {"rebind.example": ["93.184.216.34", "127.0.0.1"]})
    dialed, _ = _fake_connect(monkeypatch, [_http()] * 3)
    assert articles._download("http://rebind.example/a") is None
    assert _all_global(dialed)  # 内部 IP は一度も接続先に渡らない
    assert dialed == []
    assert calls == {"rebind.example": 2}  # 事前判定で1回、接続時に1回(接続時の答えで止まった)


def test_connect_dials_the_checked_public_ip(monkeypatch):
    """接続は検査に使った解決結果の IP へ直接行う(接続のために名前解決をやり直さない)。"""
    calls = _seq_dns(monkeypatch, {"public.example": ["93.184.216.34", "127.0.0.1"]})
    dialed, socks = _fake_connect(monkeypatch, [_http()])
    conn = articles._PublicHTTPConnection("public.example", 8080, timeout=1)
    conn.connect()
    assert dialed == [("93.184.216.34", 8080)] and calls == {"public.example": 1}

    _fake_dns(monkeypatch)  # public.example → 93.184.216.34, 2606:4700::1111
    dialed, socks = _fake_connect(monkeypatch, [_http(body=b"<html>body")])
    assert articles._download("http://public.example/a") == b"<html>body"
    assert dialed == [("93.184.216.34", 80)]
    assert b"Host: public.example\r\n" in socks[0].sent  # Host ヘッダは元のホスト名

    dialed, _ = _fake_connect(monkeypatch, [None, _http()])  # 1つ目が繋がらなければ次の検査済み IP へ
    assert articles._download("http://public.example/a") == b"<html>ok"
    assert dialed == [("93.184.216.34", 80), ("2606:4700::1111", 80)]


def test_https_verifies_certificate_for_original_hostname(monkeypatch):
    _fake_dns(monkeypatch)
    dialed, _ = _fake_connect(monkeypatch, [_http()])
    wrapped = []

    def fake_wrap(ctx, sock, server_hostname=None, **kw):
        wrapped.append((server_hostname, ctx.check_hostname, ctx.verify_mode))
        return sock  # TLS ハンドシェイクはしない(外部に繋がないため)

    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap)
    assert articles._download("https://public.example:8443/a") == b"<html>ok"
    assert dialed == [("93.184.216.34", 8443)]                         # TCP は検査済み IP へ
    assert wrapped == [("public.example", True, ssl.CERT_REQUIRED)]    # SNI・証明書検証は元のホスト名で


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "169.254.169.254", "localhost",
                                  "intranet.example", "mixed.example", "2130706433",
                                  "unknown-host.invalid"])
@pytest.mark.parametrize("cls", [articles._PublicHTTPConnection, articles._PublicHTTPSConnection])
def test_connection_refuses_non_public_on_its_own(monkeypatch, cls, host):
    """事前判定を経ない場合でも、接続クラス単体で内部宛てを拒否する(本体の検査は接続時)。"""
    _fake_dns(monkeypatch)
    dialed, _ = _fake_connect(monkeypatch, [])
    with pytest.raises(OSError):
        cls(host, 80, timeout=1).connect()
    assert dialed == []


def test_redirect_target_rebinding_to_internal_is_not_dialed(monkeypatch):
    """リダイレクト先の事前判定は公開 IP で通っても、接続時に内部 IP へ解決されたら繋がない。"""
    _seq_dns(monkeypatch, {"public.example": ["93.184.216.34"],
                           "rebind.example": ["93.184.216.34", "127.0.0.1"]})
    dialed, _ = _fake_connect(monkeypatch, [
        _http("302 Found", b"", Location="http://rebind.example/x"), _http(), _http()])
    assert articles._download("http://public.example/a") is None
    assert dialed == [("93.184.216.34", 80)]  # 最初のホストだけ。リダイレクト先には繋いでいない


def test_opener_ignores_proxy_env():
    """環境変数のプロキシ設定で接続先(=検査対象)が変わらない。opener は import 時に組まれるので、
    プロキシを設定した別プロセスで import して確かめる(既定の opener は拾うことも併せて確認)。"""
    import os
    import subprocess
    import sys

    code = ("import urllib.request\n"
            "from xnewsbot import articles\n"
            "has = lambda o: any(isinstance(h, urllib.request.ProxyHandler) for h in o.handlers)\n"
            "print(has(urllib.request.build_opener()), has(articles._OPENER))\n")
    env = {**os.environ, "http_proxy": "http://127.0.0.1:3128", "https_proxy": "http://127.0.0.1:3128"}
    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-c", code], cwd=root, env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["True", "False"]


def test_extract_body_normalizes_and_truncates():
    html = ("<html><head><title>t</title></head><body><nav>メニュー</nav><article><h1>見出し</h1>"
            + "".join(f"<p>本文の段落{i}です。" + "あ" * 80 + "</p>" for i in range(12))
            + "</article><footer>フッター</footer></body></html>")
    body = articles.extract_body(html, "https://e.com/a")
    assert "本文の段落0です。" in body[:30]
    assert "メニュー" not in body and "フッター" not in body   # ナビ・フッターは落とす
    assert len(body) == articles.BODY_MAX_CHARS
    assert "\n" not in body


# --- pipeline collect の補助関数 ---

def _fi(title, url, source="S", hours_ago=1, summary=""):
    pub = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return nf.FeedItem(title=title, url=url, source=source, published=pub, origin="feed",
                       summary=summary)


def test_trim_adds_media_kind_official():
    t = {"text": "公式の\n発表", "viewCount": 5, "likeCount": 1, "url": "u", "createdAt": "c",
         "author": {"userName": "OpenAI", "followers": 9}, "_official": True}
    r = pl._trim(t)
    assert r["text"] == "公式の 発表"
    assert (r["media"], r["kind"], r["official"]) == ("@OpenAI", "x", True)
    assert pl._trim({"author": {"userName": "u"}})["official"] is False
    assert len(pl._trim({"text": "あ" * 5000})["text"]) == pl.X_TEXT_MAX


def test_dump_raw_one_candidate_per_line_with_index():
    """1行1候補・`i` はジャンル内の位置。JSON として読み戻せ、Read が切る2000字を超える行が無い。"""
    out = {"date": "2026-10-01", "tz": "Asia/Tokyo", "slot": "morning",
           "market": [{"key": "N225", "close": 1}],
           "schedule": [{"time_label": "21:30", "name": "米 雇用統計（非農業部門雇用者数）"},
                        {"time_label": "翌03:00", "name": "米 FOMC 政策金利発表"}],
           "indicator_results": [{"name": "米 ADP雇用者数", "result": "9.0万人"}],
           "recent_titles": {"AI": ["前日の見出し"], "株": []},
           "genres": {"AI": [{"text": "a" * 900, "body": "本" * 600}, {"text": "b"}], "株": []}}
    text = pl._dump_raw(out)
    back = json.loads(text)
    assert [c["i"] for c in back["genres"]["AI"]] == [0, 1]
    assert [{k: v for k, v in c.items() if k != "i"} for c in back["genres"]["AI"]] == out["genres"]["AI"]
    assert back["genres"]["株"] == [] and back["recent_titles"] == out["recent_titles"]
    assert back["market"] == out["market"]
    assert back["schedule"] == out["schedule"] and back["indicator_results"] == out["indicator_results"]
    assert '{"time_label": "翌03:00", "name": "米 FOMC 政策金利発表"}' in text.splitlines()  # 1件1行
    assert sum('"i": ' in line for line in text.splitlines()) == 2
    assert max(len(line) for line in text.splitlines()) < 2000
    empty = {**out, "market": [], "schedule": [], "indicator_results": [], "recent_titles": {},
             "genres": {}}
    back = json.loads(pl._dump_raw(empty))
    assert back["genres"] == {} and back["schedule"] == [] and back["indicator_results"] == []


def test_cap_x_keeps_all_official_then_top_views():
    off = [{"id": f"o{i}", "_official": True, "viewCount": 1} for i in range(3)]
    rest = [{"id": f"r{i}", "viewCount": i} for i in range(10)]
    out = pl._cap_x(rest + off, limit=6, min_general=0)
    assert [t["id"] for t in out] == ["o0", "o1", "o2", "r9", "r8", "r7"]


def test_merge_news_round_robin_and_dedup():
    ja = [_fi("日銀が利上げ", "https://a/1"), _fi("株価が反落", "https://a/2"), _fi("東証が新記録", "https://a/3")]
    en = [_fi("Fed holds rates", "https://b/1"), _fi("ＮＶＩＤＩＡ決算", "https://b/2")]
    feed = [_fi("日銀が利上げ!", "https://c/1"),     # 正規化見出しが ja[0] と同じ → 落ちる
            _fi("別媒体の記事", "https://a/2"),       # URL が ja[1] と同じ → 落ちる
            _fi("nvidia決算", "https://c/3")]         # 全角/大小文字違いで en[1] と同じ → 落ちる
    out = pl.merge_news([ja, en, feed])
    assert [c["text"] for c in out] == ["日銀が利上げ", "Fed holds rates", "株価が反落", "ＮＶＩＤＩＡ決算", "東証が新記録"]
    assert all(c["source"] == "news" and c["body"] == "" for c in out)
    assert len(pl.merge_news([ja, en, feed], limit=2)) == 2


def test_merge_news_accepts_trend_candidates_first():
    """trends の候補(dict・trend 付き)も混ぜられる。同じ記事なら先に並べた trend 付きが残る。"""
    trend = nf.as_candidate(_fi("話題の記事", "https://t/1"))
    trend["trend"] = {"source": "hatena", "bookmarks": 300}
    feed = [_fi("話題の記事", "https://t/1"), _fi("別の記事", "https://t/2")]
    out = pl.merge_news([[trend], feed])
    assert [c["text"] for c in out] == ["話題の記事", "別の記事"]
    assert out[0]["trend"] == {"source": "hatena", "bookmarks": 300} and "trend" not in out[1]


def test_newsfeed_candidates_adds_trend_sources_and_news_max(monkeypatch, capsys):
    """trend_sources のあるジャンルは話題の候補を先に足す(未知の名前は飛ばす)。上限は news_max。"""
    def fake_trend(hours):
        out = []
        for i in range(3):
            c = nf.as_candidate(_fi(f"話題{i}", f"https://t/{i}"))
            c["trend"] = {"source": "hatena", "bookmarks": 100 - i, "hours": hours}
            out.append(c)
        return out

    monkeypatch.setitem(pl.trends.SOURCES, "hatena", fake_trend)
    monkeypatch.setattr(pl, "trend_sources", lambda g: ["hatena", "no_such_source"])
    monkeypatch.setattr(pl, "keywords", lambda g: [])
    monkeypatch.setattr(pl, "keywords_en", lambda g: [])
    monkeypatch.setattr(pl, "feeds", lambda g: [{"url": "https://f/plain", "name": "P", "filter": False}])
    monkeypatch.setattr(pl.newsfeeds, "fetch_feed", lambda url, name, within_hours=24: [
        _fi(f"媒体記事{i}", f"https://f/{i}", hours_ago=i) for i in range(5)])
    monkeypatch.setattr(pl, "news_max", lambda g: 4)
    settings = SimpleNamespace(collect_use_newsfeeds=True, collect_hours=24)

    out = pl._newsfeed_candidates("話題", settings)
    assert [c["text"] for c in out] == ["話題0", "媒体記事0", "話題1", "媒体記事1"]
    assert out[0]["trend"]["hours"] == 24
    assert "no_such_source" in capsys.readouterr().err
    monkeypatch.setattr(pl, "news_max", lambda g: None)   # 未指定は既定の上限
    assert len(pl._newsfeed_candidates("話題", settings)) == 8


def _collect_env(monkeypatch, tmp_path, sched):
    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace(default_tz="Asia/Tokyo"))
    monkeypatch.setattr(pl.xclient, "load_keys", lambda settings: ["k"])
    monkeypatch.setattr(pl.xclient, "collect", lambda g, settings=None, keys=None: [])
    monkeypatch.setattr(pl, "_newsfeed_candidates", lambda g, settings: [
        {"source": "news", "text": "見出し", "url": "https://n/1", "body": "本文あり"}])
    monkeypatch.setattr(pl.articles, "enrich_bodies", lambda cands: None)
    monkeypatch.setattr(pl.market, "fetch_market", lambda: [])
    monkeypatch.setattr(pl.schedule, "fetch", sched)
    monkeypatch.setattr(pl, "_recent_titles", lambda genres, day: {g: [] for g in genres})
    out = tmp_path / "raw.json"
    pl.cmd_collect(SimpleNamespace(user=None, due=False, genres="株", slot="morning", out=str(out)))
    return json.loads(out.read_text(encoding="utf-8"))


def test_collect_writes_schedule_and_indicator_results(monkeypatch, tmp_path):
    calls = []
    ev = {"at": None, "time_label": "未定", "kind": "earnings", "country": "US", "name": "Nike（NKE）決算",
          "forecast": "", "previous": "", "result": "", "importance": 3}
    res = {**ev, "kind": "indicator", "name": "米 ADP雇用者数", "result": "9.0万人"}
    raw = _collect_env(monkeypatch, tmp_path,
                       lambda: calls.append(1) or {"schedule": [ev], "results": [res]})
    assert calls == [1]                                    # 全体で1回だけ
    assert raw["schedule"] == [ev] and raw["indicator_results"] == [res]


def test_collect_schedule_failure_gives_empty(monkeypatch, tmp_path, capsys):
    def boom():
        raise RuntimeError("down")
    raw = _collect_env(monkeypatch, tmp_path, boom)
    assert raw["schedule"] == [] and raw["indicator_results"] == []
    assert raw["genres"]["株"][0]["text"] == "見出し"      # 予定が取れなくても収集は続く
    assert "今日の予定: 取得失敗" in capsys.readouterr().err


def test_ingest_saves_schedule(monkeypatch, tmp_path, session):
    from xnewsbot import digest
    ev = {"at": "2026-10-01T21:30:00+09:00", "time_label": "21:30", "kind": "indicator",
          "country": "US", "name": "米 雇用統計（失業率）", "forecast": "4.3%", "previous": "4.3%",
          "result": "", "importance": 5}
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps({"date": "2026-10-01", "slot": "morning", "market": [],
                               "schedule": [ev], "genres": {"株": []}}), encoding="utf-8")
    cur = tmp_path / "cur.json"
    cur.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pl, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "get_session", lambda: contextlib.nullcontext(session))
    pl.cmd_ingest(SimpleNamespace(raw=str(raw), curated=str(cur), date=None, slot=None))
    assert digest.get_schedule(session, date(2026, 10, 1), "morning") == [ev]


def test_quote_pages_are_dropped():
    items = [_fi("トヨタ自動車(株)【7203】：株価・株式情報 - Yahoo!ファイナンス", "https://q/1"),
             _fi("トヨタが通期予想を上方修正", "https://q/2")]
    assert [i.title for i in pl._not_quote_page(items)] == ["トヨタが通期予想を上方修正"]


def test_gn_query_quotes_phrases_and_limits_terms():
    assert pl._gn_query(["Claude Code", "LLM"]) == '("Claude Code" OR LLM)'
    assert pl._gn_query(["日銀"]) == "日銀"
    assert pl._gn_query([str(i) for i in range(10)]).count(" OR ") == pl.GN_QUERY_TERMS - 1


def test_newsfeed_candidates_combines_sources(monkeypatch):
    gn_calls = []

    def fake_gn(query, lang="ja", region="JP", within_hours=24, hl=None):
        gn_calls.append((query, lang, region, hl))
        if lang == "ja":
            return [_fi(f"日本語{i}", f"https://ja/{i}") for i in range(15)] + [
                _fi("ソニーG(株)【6758】：株価・株式情報", "https://ja/q")]
        return [_fi(f"english {i}", f"https://en/{i}") for i in range(12)]

    feed_items = {
        "https://f/plain": [_fi(f"媒体記事{i}", f"https://f/{i}", hours_ago=i) for i in range(12)],
        "https://f/filtered": [_fi("新しい LLM の論文", "https://g/1"), _fi("無関係な話題", "https://g/2")],
        "https://f/broken": None,
    }

    def fake_feed(url, name, within_hours=24):
        if feed_items[url] is None:
            raise RuntimeError("壊れた XML")
        return list(feed_items[url])

    monkeypatch.setattr(pl, "keywords", lambda g: ["生成AI", "Claude Code"])
    monkeypatch.setattr(pl, "keywords_en", lambda g: ["LLM"])
    monkeypatch.setattr(pl, "trend_sources", lambda g: [])   # genres.toml の AI は実ネットを引く取得元を持つ
    monkeypatch.setattr(pl, "feeds", lambda g: [
        {"url": "https://f/plain", "name": "P", "filter": False},
        {"url": "https://f/filtered", "name": "F", "filter": True},
        {"url": "https://f/broken", "name": "B", "filter": False}])
    monkeypatch.setattr(pl.newsfeeds, "google_news", fake_gn)
    monkeypatch.setattr(pl.newsfeeds, "fetch_feed", fake_feed)
    settings = SimpleNamespace(collect_use_newsfeeds=True, collect_hours=24)

    out = pl._newsfeed_candidates("AI", settings)
    texts = [c["text"] for c in out]
    assert sum(t.startswith("日本語") for t in texts) == pl.GN_JA_MAX
    assert sum(t.startswith("english") for t in texts) == pl.GN_EN_MAX
    assert sum(t.startswith("媒体記事") for t in texts) == pl.FEED_MAX
    assert "新しい LLM の論文" in texts and "無関係な話題" not in texts
    assert not any("株価・株式情報" in t for t in texts)
    assert len(out) == pl.GN_JA_MAX + pl.GN_EN_MAX + pl.FEED_MAX + 1
    assert sorted(gn_calls, key=str) == [('(生成AI OR "Claude Code")', "ja", "JP", None),
                                         ("LLM", "en", "US", "en-US")]

    settings.collect_use_newsfeeds = False
    assert pl._newsfeed_candidates("AI", settings) == []


def test_recent_titles_reads_previous_days(monkeypatch, session):
    day = date(2026, 10, 1)

    def add(d, genre, titles):
        gd = GenreDigest(digest_date=d, genre=genre)
        session.add(gd)
        session.commit()
        session.refresh(gd)
        for i, t in enumerate(titles):
            session.add(NewsItem(genre_digest_id=gd.id, genre=genre, rank=i, title=t))
        session.commit()

    add(day, "AI", ["当日の見出し"])                      # 当日分は含めない
    add(day - timedelta(days=1), "AI", ["昨日1", "昨日2"])
    add(day - timedelta(days=2), "AI", ["昨日1", "一昨日"])  # 重複は1回
    add(day - timedelta(days=4), "AI", ["古すぎる"])        # 範囲外
    add(day - timedelta(days=1), "株", ["株の昨日"])
    add(day - timedelta(days=1), "健康", ["対象外ジャンル"])

    monkeypatch.setattr(pl, "init_db", lambda: None)
    monkeypatch.setattr(pl, "get_session", lambda: contextlib.nullcontext(session))
    out = pl._recent_titles(["AI", "株", "特大"], day)
    assert out == {"AI": ["昨日1", "昨日2", "一昨日"], "株": ["株の昨日"], "特大": []}


def test_recent_titles_db_error_returns_empty(monkeypatch):
    def boom():
        raise RuntimeError("db locked")

    monkeypatch.setattr(pl, "init_db", boom)
    assert pl._recent_titles(["AI"], date(2026, 10, 1)) == {"AI": []}


# --- X クレジット消費(収集前後の残高差) ---

def test_total_balance_sums_only_positive_keys(monkeypatch):
    """残高マイナスの鍵(常に 402)は合計に入れない。"""
    bal = {"k0": -500, "k1": 3_000_000, "k2": 40_677}
    monkeypatch.setattr(pl.xclient, "fetch_balance", lambda k: bal[k])
    assert pl._total_balance(["k0", "k1", "k2"]) == 3_040_677


def test_total_balance_none_when_any_key_fails(monkeypatch):
    """1つでも取れなければ None(足す鍵の集合が前後でずれて使用量が狂うのを防ぐ)。"""
    bal = {"k1": 3_000_000, "k2": None}
    monkeypatch.setattr(pl.xclient, "fetch_balance", lambda k: bal[k])
    assert pl._total_balance(["k1", "k2"]) is None


def test_compute_x_usage():
    assert pl.compute_x_usage(3_050_547, 3_040_677) == {"used": 9870, "remaining": 3_040_677}
    assert pl.compute_x_usage(100, 100) == {"used": 0, "remaining": 100}
    assert pl.compute_x_usage(100, 150) == {"used": 0, "remaining": 150}  # チャージ等で増えたら 0
    assert pl.compute_x_usage(None, 100) is None
    assert pl.compute_x_usage(100, None) is None


def test_dump_raw_includes_x_usage_on_one_line():
    base = {"date": "2026-10-01", "tz": "Asia/Tokyo", "slot": "morning", "market": [], "schedule": [],
            "indicator_results": [], "recent_titles": {}, "genres": {}}
    text = pl._dump_raw({**base, "x_usage": {"used": 9870, "remaining": 3_040_677}})
    assert json.loads(text)["x_usage"] == {"used": 9870, "remaining": 3_040_677}
    assert '"x_usage": {"used": 9870, "remaining": 3040677},' in text.splitlines()
    assert json.loads(pl._dump_raw({**base, "x_usage": None}))["x_usage"] is None
