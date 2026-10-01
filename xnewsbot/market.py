"""前日の主要指数・為替・金利(市況)を Yahoo Finance の chart API から取る。APIキー不要・無料。

株ジャンルの冒頭に「前日の日経平均・米国株・ドル円・米10年債」を数字で出すため(本人要望 2026-10-01)。
戻り値の形はメイン(キュレーション)と表示担当(line_client)との契約なので変えない:
  [{"key","label","close","change","change_pct","asof","kind"}, ...]  kind = "index" | "fx" | "yield" | "crypto"

- 「確定終値」だけを使う: 取引時間中の当日足(途中値)は終値として扱わない。
  currentTradingPeriod.regular の時間内にいる足は捨てる(為替は24h取引なので当日足は常に捨てる)。
- TOPIX は Yahoo に指数そのものの記号が無い(^TOPX / ^TPX / 998405.T はいずれも 404 を実測)。
  ETF(1306.T 等)で代用すると指数と値が違うので入れない。
- 失敗した銘柄は入れない。全滅なら []。配信は市況なしで続ける。
- 仮想通貨(ビットコイン・イーサリアム, kind="crypto")は CoinGecko の simple/price(鍵なし)から。
  24時間取引で「終値」が無いので、close は取得時点の直近値(ドル)、change/change_pct は24時間比。
  asof は取得日(JST)。取れなければ仮想通貨の行だけ省く(本人要望 2026-10-01)。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CHART_URL = "https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
# Yahoo は UA によって 429 を返す(ブラウザ風の長い UA や curl は 429、素の "Mozilla/5.0" は 200 を実測)。
_UA = "Mozilla/5.0"
_TIMEOUT = 15
# query1 が 429 のとき query2 で取れることがある(実測)。順に試す。
_HOSTS = ("query1", "query2")

COINGECKO_URL = ("https://api.coingecko.com/api/v3/simple/price"
                 "?ids={ids}&vs_currencies=usd&include_24hr_change=true")
JST = ZoneInfo("Asia/Tokyo")

# (key, Yahoo 記号, 表示ラベル, kind)。並びは表示順。
SYMBOLS: list[tuple[str, str, str, str]] = [
    ("N225", "^N225", "日経平均", "index"),
    ("GSPC", "^GSPC", "S&P500", "index"),
    ("IXIC", "^IXIC", "ナスダック", "index"),
    ("DJI", "^DJI", "NYダウ", "index"),
    ("USDJPY", "JPY=X", "ドル円", "fx"),
    # ^TNX は利回り(%)そのもの(例 5.293 = 5.293%。2026-10-01 実データで確認。旧来の10倍表記ではない)。
    ("TNX", "^TNX", "米10年債利回り", "yield"),
]

# (key, CoinGecko の id, 表示ラベル)。指数・為替・金利の後ろにこの順で並べる。
CRYPTO: list[tuple[str, str, str]] = [
    ("BTC", "bitcoin", "ビットコイン"),
    ("ETH", "ethereum", "イーサリアム"),
]


def _fetch_chart(symbol: str) -> dict | None:
    q = urllib.parse.quote(symbol, safe="")
    for host in _HOSTS:
        req = urllib.request.Request(CHART_URL.format(host=host, symbol=q), headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                continue  # 別ホストで再挑戦
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
            return None
    return None


def _fetch_crypto() -> dict | None:
    ids = ",".join(cid for _key, cid, _label in CRYPTO)
    req = urllib.request.Request(COINGECKO_URL.format(ids=ids), headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return None


def parse_crypto(data: dict, asof: str) -> list[dict]:
    """CoinGecko simple/price → 仮想通貨の行(直近値と24時間比)。値の無い通貨は入れない。

    _row は前日比を (last-prev)/prev で出すので、24時間比から24時間前の値を逆算して渡す。
    """
    out: list[dict] = []
    for key, cid, label in CRYPTO:
        try:
            v = data[cid]
            last, pct = float(v["usd"]), float(v["usd_24h_change"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(_row(key, label, "crypto", last, last / (1 + pct / 100), asof))
    return out


def _local_date(ts: int, meta: dict) -> str:
    """足の時刻を取引所の現地日付に。夏時間の切替を跨いでもずれないよう tz 名を優先する。"""
    tzname = meta.get("exchangeTimezoneName")
    try:
        tz = ZoneInfo(tzname) if tzname else None
    except (ZoneInfoNotFoundError, ValueError):
        tz = None
    if tz is not None:
        return datetime.fromtimestamp(ts, tz).date().isoformat()
    off = int(meta.get("gmtoffset") or 0)
    return datetime.fromtimestamp(ts + off, timezone.utc).date().isoformat()


def parse_chart(data: dict, now: float | None = None) -> tuple[float, float, str] | None:
    """chart JSON から (前日の確定終値, その前の確定終値, 前日の現地日付) を返す。取れなければ None。

    now はテスト用(既定は現在時刻)。
    """
    try:
        res = data["chart"]["result"][0]
        meta = res.get("meta") or {}
        ts = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0].get("close") or []
    except (KeyError, IndexError, TypeError):
        return None
    bars = [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None]
    now = time.time() if now is None else now
    reg = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
    start, end = reg.get("start"), reg.get("end")
    if start is not None and end is not None and now < end:
        # 今の取引時間がまだ終わっていない → その時間帯に入った足は途中値なので捨てる
        bars = [(t, c) for t, c in bars if t < start]
    if len(bars) < 2:
        return None
    (_, prev), (t_last, last) = bars[-2], bars[-1]
    return last, prev, _local_date(t_last, meta)


def _row(key: str, label: str, kind: str, last: float, prev: float, asof: str) -> dict:
    nd = 3 if kind == "yield" else 2  # 利回りは小数3桁(0.01%未満の動きも意味がある)
    change = last - prev
    return {
        "key": key,
        "label": label,
        "close": round(last, nd),
        "change": round(change, nd),
        "change_pct": round(change / prev * 100, 2) if prev else 0.0,
        "asof": asof,
        "kind": kind,
    }


def fetch_market() -> list[dict]:
    """主要指数・ドル円・米10年債の前日終値と前日比。失敗した銘柄は入れない。全滅なら []。
    仮想通貨は直近値と24時間比を最後に足す(取れなければ仮想通貨の行だけ省く)。

    Yahoo は短時間の連打で 429 になりやすいので並列にせず順に取る(6件で数秒)。
    """
    out: list[dict] = []
    for key, symbol, label, kind in SYMBOLS:
        data = _fetch_chart(symbol)
        parsed = parse_chart(data) if data else None
        if parsed is None:
            print(f"  市況: {label}({symbol}) 取得失敗のため省略", file=sys.stderr)
            continue
        last, prev, asof = parsed
        out.append(_row(key, label, kind, last, prev, asof))
    data = _fetch_crypto()
    crypto = parse_crypto(data, datetime.now(JST).date().isoformat()) if data else []
    if not crypto:
        print("  市況: 仮想通貨(CoinGecko) 取得失敗のため省略", file=sys.stderr)
    return out + crypto
