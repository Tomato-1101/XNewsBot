"""「今日の予定」(経済指標・金融政策・要人発言・決算)を無料の公開ページから集める。APIキー不要。

配信(08:00 JST)の要点に「次の配信までに何があるか」を出すためと(本人要望 2026-10-01)、
キュレーションが指標の「予想比」を書く材料(直近24時間に出た結果)を渡すため。
戻り値の形は表示担当(line_client)・models.ScheduleSnapshot との契約なので変えない:
  {"schedule": [item, ...], "results": [item, ...], "surprises": [item, ...]}
  item = {"at": ISO8601(JST) | None, "time_label", "kind", "country", "name",
          "forecast", "previous", "result", "importance"}
  time_label と name は表示にそのまま使う完成形(表示側は加工しない)。
  決算の注目銘柄には "notable": True、株探が取れず IRBANK で代用した日本の項目には "fallback": True が付く。
  surprises は前営業日の決算への市場の反応(kind="surprise"。move_pct・move_label・headline が増える)。

取得元(どれも鍵なし。2026-10-01 に実ページを1回ずつ取得して構造を確認):
- みんかぶFX 経済指標カレンダー(HTML): 重要度1〜5・国・JST 時刻・予想/前回/結果。
  `date=D&days=N` で D から N 日分の表(日付ごとの caption)が返る。前日分は results 用。
- FRB calendar.json(UTF-8 BOM 付き): FOMC 声明・議長会見・議事録・議長の講演/証言だけ。時刻は米東部。
- 日銀 金融政策決定会合の日程(HTML): 会合の最終日に結果発表と総裁会見。
- Nasdaq 決算カレンダー(JSON。ブラウザ風 UA が必要): 注目決算=時価総額500億ドル以上の全社。
- 株探「今週の決算発表予定」(HTML): 日本の注目決算(★)の全銘柄。取れない日だけ IRBANK の上位8社で代用。
- IRBANK 決算発表予定(HTML): 株探の銘柄の会社名・発表目安の突き合わせ。
- 決算サプライズ(前営業日の決算への反応。2026-10-02 追加): 日本は株探 PTS ランキング(騰落率5%以上・ETF/REIT 除く)の
  上位20銘柄のうち、株探の個別ニュースに決算・修正の見出しがあるものだけ(決算と無関係の値動きを出さない)。
  米国は Nasdaq(時価総額200億ドル以上)+ Yahoo chart の5分足(時間外・当日の騰落)。
  株探へのアクセスは全体で直列・0.3秒以上の間隔を空ける。
取得元ごとに失敗しても他は返す(失敗は stderr に1行・その取得元は空)。配信は止めない。
"""

from __future__ import annotations

import gzip
import html
import http.client
import json
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from xnewsbot import market

JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")

MINKABU_URL = "https://fx.minkabu.jp/indicators?country=all&date={d}&days=3"
FRB_URL = "https://www.federalreserve.gov/json/calendar.json"
BOJ_URL = "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
IRBANK_URL = "https://irbank.net/market/kessan?y={d}"
KABUTAN_TOP_URL = "https://kabutan.jp/"
KABUTAN_BASE = "https://kabutan.jp"
KABUTAN_NEWS_URL = "https://kabutan.jp/stock/news?code={code}"
KABUTAN_PTS_URL = ("https://kabutan.jp/warning/pts_night_price_{kind}"
                   "?market=0&capitalization={cap}&dispmode=normal&stc=&stm=0&page=1")
YAHOO_CHART_URL = ("https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}"
                   "?interval=5m&range=5d&includePrePost=true")

# Nasdaq は素の urllib UA だと応答しないことがあるのでブラウザ風 UA を付ける(newsfeeds と同じ)。
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
_TIMEOUT = 20

DELIVER_HOUR = 8          # 翌朝のこの時刻(JST)までを「今日の予定」にする(次の定時配信まで)
MAX_ITEMS = 20            # 予定の最大件数(多いときは重要度の低いものから落とす)
# 「昼ごろ」「寄り前」「引け後」の at は並べ替え用の近似なので、過ぎてもこの時間までは未発表とみなして残す
APPROX_GRACE = timedelta(hours=3)
EARNINGS_MAX = 8          # 株探が取れない日の代用(IRBANK)は時価総額の上位この数まで
US_NOTABLE_CAP = 50_000_000_000   # 米国の注目決算の時価総額下限(ドル=500億ドル。該当は全社出す)
JP_MIN_CAP_OKU = 1000         # 日本の代用(IRBANK)の時価総額下限(億円)

# 決算サプライズ(前営業日の決算への市場の反応)
SURPRISE_MIN_PCT = 5.0        # 騰落率の絶対値がこれ以上
SURPRISE_MAX = 10             # 日米それぞれ最大この件数(|%|の大きい順)
SURPRISE_US_MIN_CAP = 20_000_000_000   # 米国の母集団の時価総額下限(ドル)
SURPRISE_JP_CANDIDATES = 20   # 日本は PTS の |%| 上位この数まで個別ニュースを引く(株探へのアクセスの上限)
# PTS ランキングの市場区分の末尾(全角)。ETF=Ｅ・REIT=Ｒ・インフラファンド=Ｉ は決算サプライズの対象外
SURPRISE_JP_EXCLUDED_MARKETS = ("Ｅ", "Ｒ", "Ｉ")
KABUTAN_GAP = 0.3             # 株探への連続アクセスの最小間隔(秒)
YAHOO_WORKERS = 4             # Yahoo chart の同時取得数

# みんかぶの国コード → (表示の接頭辞, 採用する最低重要度)。ここに無い国は出さない。
# ユーロ圏・中国・英国は最高重要度(5)だけ(本人要望: 日米中心、他国は大きいものだけ)。
COUNTRIES: dict[str, tuple[str, int]] = {
    "US": ("米", 4), "JP": ("日", 4), "EU": ("ユーロ圏", 5), "CN": ("中国", 5), "GB": ("英", 5),
}
# 名前がこれで始まるなら国名の接頭辞は付けない(「日 日銀短観」のような重複を避ける)。
_SELF_NAMED = ("日銀", "FRB", "ECB")


class _FetchError(Exception):
    pass


def _get(url: str) -> bytes | None:
    """URL を GET。失敗(タイムアウト/HTTP/接続断)は None。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "ja,en;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError,
            http.client.HTTPException):  # 読み込み途中の切断(IncompleteRead 等)
        return None
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            return None
    return data


def _need(url: str) -> bytes:
    body = _get(url)
    if not body:
        raise _FetchError(f"取得できませんでした: {url}")
    return body


_TAG = re.compile(r"<[^>]+>")
_TD = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
_SPAN = re.compile(r"<span[^>]*>(.*?)</span>", re.S)
_HM = re.compile(r"(\d{1,2}):(\d{2})")


def _text(s: str | None) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", s or "")).split())


def _jst(d: date, hour: int, minute: int) -> datetime:
    return datetime.combine(d, dtime(0, 0), JST) + timedelta(hours=hour, minutes=minute)


def _item(at: datetime | None, kind: str, country: str, name: str, importance: int, *,
          day: date | None = None, label: str = "", forecast: str = "", previous: str = "",
          result: str = "", **extra) -> dict:
    """内部表現(at は datetime、day は at が無い予定の日付、label は固定の表示時刻)。"""
    return {"at": at, "day": day or (at.date() if at else None), "label": label, "kind": kind,
            "country": country, "name": name, "forecast": forecast, "previous": previous,
            "result": result, "importance": importance, **extra}


# --- みんかぶFX 経済指標 ---

_MK_TOKEN = re.compile(
    r"<caption[^>]*>\s*(\d{4})年(\d{1,2})月(\d{1,2})日"
    r'|<tr[^>]*data_importance="(\d)"[^>]*data_country="(\w+)"[^>]*>(.*?)</tr>', re.S)


def _mk_value(td: str) -> str:
    """値セルの先頭の値(前回の「（改定値）」は捨てる)。未発表の「---」は空。"""
    m = _SPAN.search(td)
    v = _text(m.group(1) if m else td)
    return "" if v in ("---", "-") else v


def parse_minkabu(body: bytes) -> list[dict]:
    """みんかぶの指標カレンダー HTML → 全行(国・重要度で絞らない)。

    行は [時刻(JST), 国旗, 名前, 重要度の星, 前回ドル円変動幅, 前回, 予想, 結果] の td。
    時刻が「未定」の行は at=None(day は caption の日付)。
    """
    s = body.decode("utf-8", "replace")
    out: list[dict] = []
    day: date | None = None
    for m in _MK_TOKEN.finditer(s):
        if m.group(1):
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            continue
        tds = [td for _attr, td in _TD.findall(m.group(6))]
        if day is None or len(tds) < 8:
            continue
        hm = _HM.fullmatch(_text(tds[0]))
        at = _jst(day, int(hm.group(1)), int(hm.group(2))) if hm else None
        out.append({"day": day, "at": at, "importance": int(m.group(4)), "country": m.group(5),
                    "raw_name": _text(tds[2]), "previous": _mk_value(tds[5]),
                    "forecast": _mk_value(tds[6]), "result": _mk_value(tds[7])})
    return out


_FW = str.maketrans({chr(c): chr(c - 0xFEE0) for c in range(0xFF10, 0xFF5B)
                     if chr(c - 0xFEE0).isalnum()})
# 名前の後ろの対象期間(「09月」「第2四半期」「09/21 - 09/27」など)
_PERIOD = re.compile(r"\s+(?:\d{1,2}月|第\d四半期|\d{1,2}/\d{1,2}\s*-\s*\d{1,2}/\d{1,2}|\d{4}年\S*|[上下]期)\s*$")
# 詳細([...])の末尾にあっても情報にならない語(同じ時刻の行が並ぶときだけ残して区別する)
_QUALIFIERS = ("前月比", "前年比", "前期比", "前期比年率", "前年同月比", "前年同期比", "前週比",
               "確報値", "速報値", "改定値", "季調済", "季調前")


def indicator_name(raw: str, country: str, *, full: bool = False) -> str:
    """「アメリカ・雇用統計 09月 [非農業部門雇用者数・前月比]」→「米 雇用統計（非農業部門雇用者数）」。

    full=True は詳細の「前月比」なども残す(同じ時刻に同じ名前の行が並んだときの区別用)。
    """
    raw = raw.translate(_FW)
    m = re.match(r"^\s*[^・\s]+・(.*?)\s*(?:\[(.*)\])?\s*$", raw)
    if not m:
        return raw.strip()
    body, detail = m.group(1).strip(), (m.group(2) or "").strip()
    prev = None
    while prev != body:
        prev, body = body, _PERIOD.sub("", body).strip()
    body = body.replace("購買担当者景気指数・", "").replace("（購買担当者景気指数）", "")
    parts = [p for p in detail.split("・") if p]
    if not full:
        while parts and parts[-1] in _QUALIFIERS:
            parts.pop()
    det = "・".join(parts)
    if not det or det in body:
        name = body
    elif body in det:
        name = det                      # [コアPCE価格指数] は「PCE価格指数」より詳しい
    elif body.endswith("）"):
        name = f"{body[:-1]}・{det}）"  # 「耐久財受注（確報値・輸送除くコア）」
    else:
        name = f"{body}（{det}）"
    if name.startswith(_SELF_NAMED):
        return name
    return f"{COUNTRIES[country][0]} {name}" if country in COUNTRIES else name


def indicators(rows: list[dict]) -> list[dict]:
    """みんかぶの行 → 予定の要素(国・重要度で絞る)。政策金利の行は kind=policy・1国1時刻1件。"""
    picked = [r for r in rows
              if r["country"] in COUNTRIES and r["importance"] >= COUNTRIES[r["country"]][1]]
    names = [indicator_name(r["raw_name"], r["country"]) for r in picked]
    # 同じ時刻に同じ名前が並ぶ(GDP の前期比/前年比など)ときは詳細を残して区別する
    seen: dict[tuple, int] = {}
    for n, r in zip(names, picked):
        seen[(r["day"], r["at"], n)] = seen.get((r["day"], r["at"], n), 0) + 1
    out: list[dict] = []
    done: set[tuple] = set()
    for n, r in zip(names, picked):
        policy = "政策金利" in r["raw_name"]
        if policy:
            # 上限金利/下限金利のように同じ政策金利が複数行あるときは先頭(上限金利)だけ
            n = indicator_name(r["raw_name"].split("[")[0], r["country"])
        elif seen[(r["day"], r["at"], n)] > 1:
            n = indicator_name(r["raw_name"], r["country"], full=True)
        key = (r["day"], r["at"], n)
        if key in done:
            continue
        done.add(key)
        out.append(_item(r["at"], "policy" if policy else "indicator", r["country"], n,
                         r["importance"], day=r["day"], forecast=r["forecast"],
                         previous=r["previous"], result=r["result"], rate_row=policy))
    return out


# --- FRB(FOMC・議長) ---

_ET_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])\.?\s*m", re.I)
# 「Speech - Chair Jerome H. Powell」「Testimony - Chairman Kevin Warsh」(2026年夏から Chairman 表記)
_CHAIR = re.compile(r"^(Speech|Discussion|Testimony)\s*-+\s*Chair(?:man)?\s+(.+)$", re.I)
# 議長名のカタカナ。載っていない議長は名前なしの「FRB議長」にする(誤った読みを出さない)。
_FRB_CHAIRS = {"Powell": "パウエル", "Warsh": "ウォーシュ"}
_CHAIR_VERB = {"speech": "講演", "discussion": "討論会", "testimony": "議会証言"}


def _et_hm(raw: str | None) -> tuple[int, int] | None:
    m = _ET_TIME.search(raw or "")
    if not m:
        return None
    return int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0), int(m.group(2))


def _frb_spec(title: str) -> dict | None:
    t = title.lower()
    if t == "fomc meeting":   # 2日目の 2:00 p.m.(ET) が声明=政策金利の発表
        return {"kind": "policy", "name": "米 FOMC 政策金利発表", "importance": 5, "decision": True}
    if t == "fomc press conference":
        return {"kind": "speech", "name": "FRB議長 会見", "importance": 5}
    if t == "fomc minutes":
        return {"kind": "policy", "name": "米 FOMC 議事録", "importance": 4}
    m = _CHAIR.match(title)   # 「Vice Chair」は「- 」の直後が Vice なので一致しない
    if m:
        who = _FRB_CHAIRS.get(m.group(2).split()[-1], "")
        return {"kind": "speech", "name": f"{who}FRB議長 {_CHAIR_VERB[m.group(1).lower()]}",
                "importance": 4}
    return None


def parse_frb(body: bytes) -> list[dict]:
    """FRB calendar.json → FOMC 声明・議長会見・議事録・議長の講演/証言の予定(時刻は ET→JST)。"""
    data = json.loads(body.decode("utf-8-sig"))  # 先頭に BOM が付いている
    out: list[dict] = []
    for e in data.get("events") or []:
        if not isinstance(e, dict):
            continue
        spec = _frb_spec(" ".join((e.get("title") or "").split()))
        if spec is None:
            continue
        try:
            y, mo = (int(x) for x in (e.get("month") or "").split("-"))
        except ValueError:
            continue
        days = [int(x) for x in re.findall(r"\d+", e.get("days") or "")]
        if spec.get("decision"):
            days = days[-1:]   # 2日間の会合は最終日が発表日
        hm = _et_hm(e.get("time"))
        for dd in days:
            try:
                d_et = date(y, mo, dd)
            except ValueError:
                continue
            at = (datetime.combine(d_et, dtime(*hm), ET).astimezone(JST) if hm else None)
            out.append(_item(at, spec["kind"], "US", spec["name"], spec["importance"], day=d_et,
                             decision=spec.get("decision", False)))
    return out


# --- 日銀 金融政策決定会合 ---

_BOJ_TABLE = re.compile(r"<caption[^>]*>[^<]*?(\d{4})年\s*</caption>(.*?)</table>", re.S)
_MD = re.compile(r"(?:(\d{1,2})月\s*)?(\d{1,2})日")


def parse_boj(body: bytes) -> list[date]:
    """日銀の会合日程 HTML → 各会合の最終日(「10月29日（木）・30日（金）」→ 10/30)。"""
    s = body.decode("utf-8", "replace")
    out: list[date] = []
    for t in _BOJ_TABLE.finditer(s):
        year = int(t.group(1))
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", t.group(2), re.S):
            tds = _TD.findall(tr)
            if not tds:
                continue
            month = None
            last = None
            for m in _MD.finditer(_text(tds[0][1])):
                if m.group(1):
                    month = int(m.group(1))
                if month:
                    last = (month, int(m.group(2)))
            if last:
                try:
                    out.append(date(year, *last))
                except ValueError:
                    continue
    return out


def boj_events(last_days: list[date]) -> list[dict]:
    """会合の最終日 → 結果発表(時刻は決まっていない。昼ごろが多い)と総裁会見(15:30)。"""
    out: list[dict] = []
    for d in last_days:
        # at は並べ替え用の近似(12:00)。表示は「昼ごろ」(発表時刻は会合の進み具合で前後する)。
        out.append(_item(_jst(d, 12, 0), "policy", "JP", "日銀 金融政策決定会合 結果発表", 5,
                         label="昼ごろ", decision=True))
        out.append(_item(_jst(d, 15, 30), "speech", "JP", "日銀総裁 会見", 4))
    return out


# --- 決算(米国: Nasdaq / 日本: IRBANK・株探) ---

_US_SUFFIX = re.compile(r",?\s+(?:Inc\.?|Incorporated|Corporation|Corp\.?|Co\.|plc|PLC|Ltd\.?|"
                        r"Limited|N\.V\.|S\.A\.|AG|SE)$")


def _us_company(name: str) -> str:
    """「Nike, Inc.」→「Nike」(法人格の語を落とす。何度か重なる場合もある)。"""
    name = " ".join((name or "").split())
    prev = None
    while prev != name:
        prev, name = name, _US_SUFFIX.sub("", name).strip()
    return name


def _usd(raw: str | None) -> int:
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits else 0


def _nasdaq_rows(body: bytes, min_cap: int) -> list[dict]:
    """Nasdaq 決算カレンダー → min_cap 以上の行を時価総額の降順で(同名の別クラス株は1社にまとめる)。"""
    rows = ((json.loads(body.decode("utf-8")).get("data") or {}).get("rows")) or []
    picked: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        cap = _usd(r.get("marketCap"))
        name, sym = _us_company(r.get("name")), (r.get("symbol") or "").strip()
        if cap < min_cap or not name or not sym or name in seen:
            continue
        seen.add(name)
        picked.append({"cap": cap, "name": name, "sym": sym, "time": r.get("time") or ""})
    picked.sort(key=lambda p: -p["cap"])
    return picked


def parse_nasdaq(body: bytes, d: date, min_cap: int = US_NOTABLE_CAP) -> list[dict]:
    """Nasdaq 決算カレンダー → 注目決算=時価総額500億ドル以上の全社(上限なし。同名の別クラス株は1社)。

    at は並べ替え用の近似: 寄り前≈当日22:00 JST(米国の寄り付き前)、引け後≈翌05:30 JST(引け後)。
    """
    out: list[dict] = []
    for r in _nasdaq_rows(body, min_cap):
        if r["time"] == "time-pre-market":
            at, label = _jst(d, 22, 0), "寄り前"
        elif r["time"] == "time-after-hours":
            at, label = _jst(d + timedelta(days=1), 5, 30), "引け後"
        else:
            at, label = None, "未定"
        out.append(_item(at, "earnings", "US", f"{r['name']}（{r['sym']}）決算", 3, day=d,
                         label=label, notable=True))
    return out


def _irbank_rows(body: bytes, d: date) -> list[dict]:
    """IRBANK の決算発表予定 → 全行 {code, name, cap(億円), hm((時,分) | None)}。

    行は [コード, 会社名, 決算種別, 発表目安, 時価総額(sortValue=億円), ...] の td。
    見出しの日付が d と違う一覧(休日に翌営業日が出る等)は空にする。
    """
    s = body.decode("utf-8", "replace")
    h = re.search(r"<h2[^>]*>\s*(\d{4})年(\d{1,2})月(\d{1,2})日発表予定", s)
    if not h or date(*(int(x) for x in h.groups())) != d:
        return []
    table = re.search(r"<table[^>]*>(.*?)</table>", s[h.end():], re.S)
    out: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1) if table else "", re.S):
        tds = _TD.findall(tr)
        if len(tds) < 5:
            continue
        code, name = _text(tds[0][1]), _text(tds[1][1])
        cap = re.search(r"sortValue:(\d+)", tds[4][0])
        if not code or not name or not cap:
            continue
        hm = _HM.match(_text(tds[3][1]))
        out.append({"code": code, "name": name, "cap": int(cap.group(1)),
                    "hm": (int(hm.group(1)), int(hm.group(2))) if hm else None})
    return out


def _jp_earnings_item(d: date, r: dict, **extra) -> dict:
    at = _jst(d, *r["hm"]) if r.get("hm") else None
    return _item(at, "earnings", "JP", f"{r['name']}（{r['code']}）決算", 3, day=d, **extra)


def parse_irbank(body: bytes, d: date) -> list[dict]:
    """IRBANK の決算発表予定 → 時価総額1000億円以上の上位8社(株探が取れない日の代用)。

    時刻は発表目安(なければ未定)。代用の項目は notable=True・fallback=True。
    """
    rows = sorted((r for r in _irbank_rows(body, d) if r["cap"] >= JP_MIN_CAP_OKU),
                  key=lambda r: -r["cap"])
    return [_jp_earnings_item(d, r, notable=True, fallback=True) for r in rows[:EARNINGS_MAX]]


# --- 株探(日本の注目決算・決算サプライズ) ---

_kabutan_lock = threading.Lock()
_kabutan_last = [0.0]
# 株探への最大26回の直列アクセスが「遅いが応答はある」状態で積み上がると collect 全体(deliver.sh の
# 600秒打ち切り)を超えて朝の配信が止まる。fetch() の開始から KABUTAN_BUDGET 秒を過ぎたら以降は読まない
# (超過は最後の1回の _TIMEOUT 分まで)。
KABUTAN_BUDGET = 90.0
_kabutan_deadline = [float("inf")]


def _kabutan_get(url: str) -> bytes | None:
    """株探への GET。全スレッド通して直列・前回から KABUTAN_GAP 秒以上空ける(負荷をかけない)。
    fetch() が決めた締め切りを過ぎていれば読まずに None(取得失敗と同じ扱い)。"""
    with _kabutan_lock:
        if time.monotonic() >= _kabutan_deadline[0]:
            return None
        wait = _kabutan_last[0] + KABUTAN_GAP - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return _get(url)
        finally:
            _kabutan_last[0] = time.monotonic()


_KB_WEEKLY_LINK = re.compile(r'<a[^>]*href="([^"]*)"[^>]*>\s*([^<]*今週の決算発表予定[^<]*)', re.S)
_KB_RANGE = re.compile(r"[(（](\d{1,2})月(\d{1,2})日\s*[～~〜]\s*(\d{1,2})月(\d{1,2})日[)）]")
_KB_HEAD = re.compile(r"^●\s*(\d{1,2})月\s*(\d{1,2})日")
_KB_ROW = re.compile(r"^<([0-9A-Za-z]{4})>\s*(.*)$")
_KB_PUB = re.compile(r'<time[^>]*class="s_news_date"[^>]*datetime="(\d{4})-(\d{2})-(\d{2})')


def _kb_text(seg: str) -> list[str]:
    """記事本文の HTML → 行のリスト(<br> で改行。タグを落としてから実体参照を戻す)。"""
    seg = re.sub(r"<br\s*/?>", "\n", seg)
    return html.unescape(_TAG.sub("", seg)).split("\n")


def pick_kabutan_weekly(body: bytes, d: date) -> str | None:
    """株探トップの「今週の決算発表予定」リンクから、d を含む週の記事 URL を返す(無ければ None)。

    リンク文字の「(9月28日～10月2日)」で週を判定する。期間が読めないリンクは次善として採る。
    """
    s = body.decode("utf-8", "replace")
    unknown: str | None = None
    seen: set[str] = set()
    for href, label in _KB_WEEKLY_LINK.findall(s):
        url = urllib.parse.urljoin(KABUTAN_BASE, html.unescape(href))
        if url in seen:
            continue
        seen.add(url)
        m = _KB_RANGE.search(html.unescape(label))
        if not m:
            unknown = unknown or url
            continue
        m1, d1, m2, d2 = (int(x) for x in m.groups())
        try:
            start = date(d.year, m1, d1)
            if start - d > timedelta(days=200):
                start = date(d.year - 1, m1, d1)
            end = date(start.year + (1 if m2 < m1 else 0), m2, d2)
        except ValueError:
            continue
        if start <= d <= end:
            return url
    return unknown


def parse_kabutan_weekly(body: bytes, d: date) -> list[dict] | None:
    """株探「今週の決算発表予定」記事 → d の注目決算(★)の [{code, name}]。d の見出しが無ければ None。

    日別の見出し「● 8月 5日―― 178銘柄」の下に「<コード> 社名 [市場] ★」の行が並ぶ。★の前の
    空白(全角・半角・タブ)の数は行ごとにずれるので、行に★があるかだけを見る。名前は全角英数を半角に。
    見出しがあって★が0件なら []。「など」(件数が多い日の省略)以降は読まない。
    """
    s = body.decode("utf-8", "replace")
    pub = _KB_PUB.search(s)
    if pub and abs((d - date(*(int(x) for x in pub.groups()))).days) > 14:
        return None   # 古い(または先の)週の記事
    m = re.search(r'<div class="mono">(.*?)</div>\s*<!--/\.mono-->', s, re.S)
    out: list[dict] = []
    in_day = found = False
    for line in _kb_text(m.group(1) if m else s):
        line = line.strip().strip("　").strip()
        h = _KB_HEAD.match(line)
        if h:
            in_day = (int(h.group(1)), int(h.group(2))) == (d.month, d.day)
            found = found or in_day
            continue
        if not in_day:
            continue
        if line.startswith("など"):
            in_day = False
            continue
        r = _KB_ROW.match(line)
        if not r or "★" not in r.group(2):
            continue
        rest = r.group(2)
        nm = re.match(r"(.*?)\s*\[[^\]]*\]", rest)
        name = unicodedata.normalize("NFKC", (nm.group(1) if nm else rest.replace("★", "")).strip())
        out.append({"code": r.group(1), "name": name})
    return out if found else None


def jp_notable_items(weekly: list[dict] | None, rows: list[dict], d: date) -> list[dict]:
    """日本の注目決算。weekly=株探の★銘柄(None=株探が取れない/見出しが無い)、rows=IRBANK の d の全行。

    株探があれば★を全部(会社名・発表目安は IRBANK とコードで突き合わせ、無ければ株探の名前・未定)。
    株探が無いときだけ IRBANK の時価総額上位8社で代用する(fallback=True)。
    """
    if weekly is None:
        top = sorted((r for r in rows if r["cap"] >= JP_MIN_CAP_OKU), key=lambda r: -r["cap"])
        return [_jp_earnings_item(d, r, notable=True, fallback=True) for r in top[:EARNINGS_MAX]]
    by_code = {r["code"]: r for r in rows}
    out: list[dict] = []
    seen: set[str] = set()
    for w in weekly:
        if w["code"] in seen:
            continue
        seen.add(w["code"])
        r = by_code.get(w["code"])
        out.append(_jp_earnings_item(d, {"code": w["code"], "name": r["name"] if r else w["name"],
                                         "hm": r["hm"] if r else None}, notable=True))
    return out


def _kabutan_weekly(d: date) -> list[dict] | None:
    top = _kabutan_get(KABUTAN_TOP_URL)
    if not top:
        raise _FetchError("株探トップを取得できませんでした")
    url = pick_kabutan_weekly(top, d)
    if url is None:
        return None
    body = _kabutan_get(url)
    if not body:
        raise _FetchError(f"取得できませんでした: {url}")
    return parse_kabutan_weekly(body, d)


def _jp_notable(d: date) -> list[dict]:
    """日本の注目決算(株探の★＋IRBANK の発表目安)。株探が駄目な日は IRBANK の上位8社で代用。"""
    try:
        weekly = _kabutan_weekly(d)
    except Exception as e:
        print(f"  予定: 株探(今週の決算発表予定) 取得失敗のため IRBANK で代用 ({type(e).__name__}: {e})",
              file=sys.stderr)
        weekly = None
    try:
        rows = _irbank_rows(_need(IRBANK_URL.format(d=d)), d)
    except _FetchError:
        if weekly is None:
            raise
        rows = []   # 株探の名前・未定のまま出す
    return jp_notable_items(weekly, rows, d)


def parse_pts(body: bytes) -> list[dict]:
    """株探 PTS ランキング → [{code, name, market, pct}]。pct は通常取引の終値比(%・符号つき)。

    列の数は表示条件で変わる(時価総額の列の有無)ので、%の付いたセルを探す。
    """
    s = body.decode("utf-8", "replace")
    out: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", s, re.S):
        code = re.search(r'<td class="tac"><a href="/stock/\?code=(\w+)"', tr)
        pct = re.search(r"<span[^>]*>\s*([+\-−]?[\d,.]+)\s*</span>\s*%", tr)
        name = re.search(r'<th scope="row"[^>]*>(.*?)</th>', tr, re.S)
        tac = re.findall(r'<td class="tac">(.*?)</td>', tr, re.S)   # [コード, 市場区分]
        if not code or not pct:
            continue
        try:
            v = float(pct.group(1).replace("−", "-").replace(",", ""))
        except ValueError:
            continue
        out.append({"code": code.group(1), "name": _text(name.group(1)) if name else "",
                    "market": _text(tac[1]) if len(tac) > 1 else "", "pct": v})
    return out


_KB_NEWS_ROW = re.compile(
    r'<td class="news_time"><time datetime="([^"]+)"[^>]*>.*?'
    r'<div class="newslist_ctg[^"]*"[^>]*>([^<]*)</div>.*?<td[^>]*><a [^>]*>(.*?)</a>', re.S)
_KB_RESULT_CATS = ("決算", "修正")


def parse_kabutan_news(body: bytes, p: date, d: date) -> str:
    """株探の個別銘柄ニュース → p の15時以降〜d の9時前に出た決算・修正の見出し(最も早い1つ。無ければ "")。

    決算発表は15時台に集中するので、その時間帯以降の「決算」「修正」カテゴリを対象にする。
    同時刻なら「決算」を優先する。
    """
    start, end = _jst(p, 15, 0), _jst(d, 9, 0)
    best: tuple[datetime, int, str] | None = None
    for dt_raw, cat, title in _KB_NEWS_ROW.findall(body.decode("utf-8", "replace")):
        cat = _text(cat)
        if cat not in _KB_RESULT_CATS:
            continue
        try:
            at = datetime.fromisoformat(dt_raw)
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=JST)
        text = _text(title)
        key = (at, _KB_RESULT_CATS.index(cat))
        if start <= at < end and text and (best is None or key < best[:2]):
            best = (at, key[1], text)
    return best[2] if best else ""


def prev_weekday(d: date) -> date:
    """d より前の直近の平日(祝日は考えない。見出しの日付が合わなければ結果が空になるだけ)。"""
    p = d - timedelta(days=1)
    while p.weekday() >= 5:
        p -= timedelta(days=1)
    return p


def _surprise(country: str, name: str, pct: float, label: str, headline: str) -> dict:
    return {"kind": "surprise", "country": country, "name": name, "move_pct": round(pct, 2),
            "move_label": label, "headline": headline, "at": None, "time_label": "",
            "forecast": "", "previous": "", "result": "", "importance": 3}


def jp_surprises(pts: list[dict], headline) -> list[dict]:
    """日本の決算サプライズ。pts=PTS ランキングの行、headline(code) → 決算・修正の見出し("" なら無し)。

    |騰落率| 5%以上(ETF・REIT 除く)を |%| の大きい順に最大 SURPRISE_JP_CANDIDATES 件選び、株探の個別ニュースに
    前営業日の決算・修正の見出しがある銘柄だけを残す(決算と無関係の値動きを出さない)。最大10件。
    |%| 順に見出しを引くので、10件そろった時点で打ち切る(株探へのアクセスを減らす)。
    銘柄名は PTS ランキングのもの。
    """
    cands: dict[str, dict] = {}
    for q in pts:
        if (abs(q["pct"]) >= SURPRISE_MIN_PCT and q["code"] not in cands
                and not q.get("market", "").endswith(SURPRISE_JP_EXCLUDED_MARKETS)):
            cands[q["code"]] = q
    top = sorted(cands.values(), key=lambda q: -abs(q["pct"]))[:SURPRISE_JP_CANDIDATES]
    out: list[dict] = []
    for q in top:
        h = headline(q["code"])
        if h:
            out.append(_surprise("JP", f"{q['name']}（{q['code']}）", q["pct"], "PTS", h))
            if len(out) >= SURPRISE_MAX:
                break
    return out


def _jp_surprise_job(d: date, p: date) -> list[dict]:
    pts: list[dict] = []
    ok = 0
    for kind in ("increase", "decrease"):
        for cap in (4, 5):   # 4=時価総額300〜1000億円、5=1000億円以上
            body = _kabutan_get(KABUTAN_PTS_URL.format(kind=kind, cap=cap))
            if body:
                ok += 1
                pts += parse_pts(body)
    if not ok:
        raise _FetchError("株探 PTS ランキングを取得できませんでした")

    def headline(code: str) -> str:
        body = _kabutan_get(KABUTAN_NEWS_URL.format(code=code))
        return parse_kabutan_news(body, p, d) if body else ""

    return jp_surprises(pts, headline)


# --- 米国の決算サプライズ(Nasdaq + Yahoo chart の5分足) ---

_ET_REG = (9 * 60 + 30, 16 * 60)    # 通常取引(ET の分)
_ET_POST = (16 * 60, 20 * 60)       # 時間外(引け後)


def _yahoo_chart(symbol: str) -> dict | None:
    """Yahoo chart の5分足(時間外込み・5日分)。Yahoo は UA を "Mozilla/5.0" だけにしないと 429。"""
    q = urllib.parse.quote(symbol.replace(".", "-"), safe="^=")
    for host in market._HOSTS:
        req = urllib.request.Request(YAHOO_CHART_URL.format(host=host, symbol=q),
                                     headers={"User-Agent": market._UA})
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                continue
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
            return None
    return None


def us_reaction(data: dict, p: date, flag: str) -> tuple[float, str] | None:
    """chart JSON → (p の決算への反応の騰落率%, "当日" | "時間外")。計算できなければ None。

    当日 = p の通常取引の終値 / 前営業日の終値。時間外 = p の時間外の最終値 / p の終値。
    flag は Nasdaq の time: 寄り前なら当日だけ、引け後なら時間外だけ、不明なら両方計算して
    絶対値の大きい方。p の終値は、取れていれば Yahoo の確定値(regularMarketPrice)を使う。
    """
    try:
        res = data["chart"]["result"][0]
        meta = res.get("meta") or {}
        ts = res.get("timestamp") or []
        closes = res["indicators"]["quote"][0].get("close") or []
    except (KeyError, IndexError, TypeError):
        return None
    try:
        tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")
    except Exception:
        tz = ET
    reg: dict[date, list[float]] = {}
    post: dict[date, list[float]] = {}
    for t, c in zip(ts, closes):
        if c is None:
            continue
        dt = datetime.fromtimestamp(int(t), tz)
        hm = dt.hour * 60 + dt.minute
        if _ET_REG[0] <= hm < _ET_REG[1]:
            reg.setdefault(dt.date(), []).append(float(c))
        elif _ET_POST[0] <= hm < _ET_POST[1]:
            post.setdefault(dt.date(), []).append(float(c))
    close = reg[p][-1] if reg.get(p) else None
    rmt, price = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and price:
        rdt = datetime.fromtimestamp(int(rmt), tz)
        if rdt.date() == p and rdt.hour * 60 + rdt.minute >= _ET_REG[1]:
            close = float(price)   # 引け後に更新された確定終値
    cands: list[tuple[float, str]] = []
    if flag != "time-after-hours":
        earlier = [dd for dd in reg if dd < p]
        if close and earlier:
            prev = reg[max(earlier)][-1]
            if prev:
                cands.append(((close / prev - 1) * 100, "当日"))
    if flag != "time-pre-market" and close and post.get(p):
        cands.append(((post[p][-1] / close - 1) * 100, "時間外"))
    return max(cands, key=lambda c: abs(c[0])) if cands else None


def us_surprises(rows: list[dict], reaction) -> list[dict]:
    """米国の決算サプライズ。rows=Nasdaq の前営業日の決算(時価総額200億ドル以上)、
    reaction(row) → (騰落率, ラベル) | None。|%| 5%以上を大きい順に最大10件。"""
    got: list[tuple[float, str, dict]] = []
    for r in rows:
        v = reaction(r)
        if v and abs(v[0]) >= SURPRISE_MIN_PCT:
            got.append((v[0], v[1], r))
    got.sort(key=lambda g: -abs(g[0]))
    return [_surprise("US", f"{r['name']}（{r['sym']}）", pct, label, "") for pct, label, r in got[:SURPRISE_MAX]]


def _us_surprise_job(p: date) -> list[dict]:
    rows = _nasdaq_rows(_need(NASDAQ_URL.format(d=p)), SURPRISE_US_MIN_CAP)
    failed: list[str] = []

    def reaction(r: dict):
        try:
            data = _yahoo_chart(r["sym"])
            v = us_reaction(data, p, r["time"]) if data else None
        except Exception:
            v = None
        if v is None:
            failed.append(r["sym"])
        return v

    with ThreadPoolExecutor(max_workers=YAHOO_WORKERS) as pool:
        got = list(pool.map(reaction, rows))
    if failed:
        print(f"  予定: 決算サプライズ(米国) Yahoo の値が取れず飛ばした銘柄 {len(failed)} 件 "
              f"({', '.join(failed[:5])}{'…' if len(failed) > 5 else ''})", file=sys.stderr)
    by_sym = {r["sym"]: v for r, v in zip(rows, got)}
    return us_surprises(rows, lambda r: by_sym.get(r["sym"]))


# --- まとめ ---

def _out(it: dict, d: date) -> dict:
    at = it["at"]
    if it["label"]:
        label = it["label"]
    elif at is None:
        label = "未定"
    else:
        label = ("翌" if at.date() > d else "") + at.strftime("%H:%M")
    out = {"at": at.isoformat() if at else None, "time_label": label, "kind": it["kind"],
           "country": it["country"], "name": it["name"], "forecast": it["forecast"],
           "previous": it["previous"], "result": it["result"], "importance": it["importance"]}
    for flag in ("notable", "fallback"):   # 注目決算の印(付くのは決算だけ。無い項目にはキー自体を出さない)
        if it.get(flag):
            out[flag] = True
    return out


def select(items: list[dict], now: datetime) -> list[dict]:
    """now から翌朝 DELIVER_HOUR 時までの予定を時刻順(at 無しは最後)に最大 MAX_ITEMS 件。

    at の無い予定は配信日(now の日付)のものだけ。FRB/日銀の政策発表があれば、みんかぶの
    政策金利の行は重ねて出さず、予想・前回だけ政策発表の行へ移す。
    """
    d = now.date()
    end = _jst(d + timedelta(days=1), DELIVER_HOUR, 0)
    def upcoming(it: dict) -> bool:
        if not it["at"]:
            return it["day"] == d
        grace = APPROX_GRACE if it["label"] else timedelta(0)
        return now <= it["at"] + grace and it["at"] <= end

    win = [it for it in items if upcoming(it)]
    decisions = {it["country"]: it for it in win if it.get("decision")}
    kept: list[dict] = []
    for it in win:
        dec = decisions.get(it["country"])
        if it.get("rate_row") and dec is not None:
            for k in ("forecast", "previous", "result"):
                dec[k] = dec[k] or it[k]
            continue
        kept.append(it)
    # 注目決算は MAX_ITEMS の枠に入れず全部残す(絞るのは経済指標など他の予定だけ)
    notable = [it for it in kept if it.get("notable")]
    others = [it for it in kept if not it.get("notable")]
    kept = sorted(others, key=lambda it: -it["importance"])[:MAX_ITEMS] + notable
    kept.sort(key=lambda it: (it["at"] is None, it["at"] or end))
    return [_out(it, d) for it in kept]


def recent_results(items: list[dict], now: datetime) -> list[dict]:
    """直近24時間に結果が出た指標(時刻順)。at の無い行は前日・当日のものだけ。"""
    d = now.date()
    start = now - timedelta(hours=24)
    got = [it for it in items if it["result"] and
           (start <= it["at"] <= now if it["at"] else it["day"] in (d - timedelta(days=1), d))]
    got.sort(key=lambda it: (it["at"] is None, it["at"] or now))
    return [_out(it, d) for it in got]


def _safe(job: tuple[str, object]) -> list[dict]:
    name, fn = job
    try:
        return fn()
    except Exception as e:
        print(f"  予定: {name} 取得失敗のため省略 ({type(e).__name__}: {e})", file=sys.stderr)
        return []


def fetch(now: datetime | None = None) -> dict:
    """{"schedule": now〜翌朝8時の予定, "results": 直近24時間に出た指標の結果,
    "surprises": 前営業日の決算への市場の反応}。

    now の既定は現在の JST(収集は配信の45分前 07:15 JST に走る)。tz の無い now は JST とみなす。
    取得元は並列に1回ずつ読む(どれかが遅くても全体は最も遅い1つ分で済む)。
    株探へのアクセスは複数のジョブにまたがるが、_kabutan_get が全体で直列にする。
    """
    if now is None:
        now = datetime.now(JST)
    now = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    d = now.date()
    p = prev_weekday(d)
    _kabutan_deadline[0] = time.monotonic() + KABUTAN_BUDGET
    jobs: list[tuple[str, object]] = [
        # 前日〜翌日の3日分(前日分は results 用、翌日分は翌朝8時までの予定用)
        ("みんかぶ(経済指標)",
         lambda: indicators(parse_minkabu(_need(MINKABU_URL.format(d=d - timedelta(days=1)))))),
        ("FRB", lambda: parse_frb(_need(FRB_URL))),
        ("日銀", lambda: boj_events(parse_boj(_need(BOJ_URL)))),
        # 米国決算は配信日と同じ日付(米東部の当日)。日本時間の夜〜翌朝に出る。
        ("Nasdaq(米国の注目決算)", lambda: parse_nasdaq(_need(NASDAQ_URL.format(d=d)), d)),
        ("株探・IRBANK(日本の注目決算)", lambda: _jp_notable(d)),
    ]
    surprise_jobs: list[tuple[str, object]] = [
        ("決算サプライズ(日本)", lambda: _jp_surprise_job(d, p)),
        ("決算サプライズ(米国)", lambda: _us_surprise_job(p)),
    ]
    with ThreadPoolExecutor(max_workers=len(jobs) + len(surprise_jobs)) as pool:
        got = list(pool.map(_safe, jobs + surprise_jobs))
    items = [it for items in got[:len(jobs)] for it in items]
    surprises = [it for items in got[len(jobs):] for it in items]
    return {"schedule": select(items, now), "results": recent_results(got[0], now),
            "surprises": surprises}
