"""「今日の予定」(経済指標・金融政策・要人発言・決算)を無料の公開ページから集める。APIキー不要。

配信(08:00 JST)の要点に「次の配信までに何があるか」を出すためと(本人要望 2026-10-01)、
キュレーションが指標の「予想比」を書く材料(直近24時間に出た結果)を渡すため。
戻り値の形は表示担当(line_client)・models.ScheduleSnapshot との契約なので変えない:
  {"schedule": [item, ...], "results": [item, ...], "surprises": [item, ...]}
  item = {"at": ISO8601(JST) | None, "time_label", "kind", "country", "name",
          "forecast", "previous", "result", "importance"}
  time_label と name は表示にそのまま使う完成形(表示側は加工しない)。
  決算の注目銘柄には "notable": True が付く。日本の決算・説明会(kind="ir")には "session"
  (intraday=場中/after=引け後/unknown)、株探の★銘柄には "star": True も付く。
  surprises は前営業日の決算への市場の反応(kind="surprise"。move_pct・move_label・headline が増える)。

取得元(どれも鍵なし。2026-10-01 に実ページを1回ずつ取得して構造を確認):
- みんかぶFX 経済指標カレンダー(HTML): 重要度1〜5・国・JST 時刻・予想/前回/結果。
  `date=D&days=N` で D から N 日分の表(日付ごとの caption)が返る。前日分は results 用。
- FRB calendar.json(UTF-8 BOM 付き): FOMC 声明・議長会見・議事録・議長の講演/証言だけ。時刻は米東部。
- 日銀 金融政策決定会合の日程(HTML): 会合の最終日に結果発表と総裁会見。
- Nasdaq 決算カレンダー(JSON。ブラウザ風 UA が必要): 注目決算=時価総額500億ドル以上の全社。
- IRBANK 決算発表予定(HTML): 日本の d に発表する全社(発表目安・過去の発表時刻・時価総額)。
- 株探「今週の決算発表予定」(HTML): 注目(★)の銘柄。★は条件に関係なく出す。
- Yahoo!ファイナンス 売買代金ランキング(HTML 内の JSON): 前営業日の上位300位。日本の決算の絞り込みと説明会の銘柄。
- Google ニュース RSS: 当日の説明会の告知記事(見出しに日付と売買代金上位の社名があるものだけ)。
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
import subprocess
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

from xnewsbot import market, newsfeeds

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
US_NOTABLE_CAP = 50_000_000_000   # 米国の注目決算の時価総額下限(ドル=500億ドル。該当は全社出す)
# 日本の決算予定(2026-11-11 本人要望「場中は全部・引け後は注目だけ。売買代金が大きいか日経平均採用なら」)。
# 日経225 の銘柄表は取れる所が無いので、時価総額 3000 億円(225 採用銘柄のおおむね下限)で代える。
JP_CLOSE = (15, 30)               # 東証の大引け。これより前の発表を「場中」とする
JP_INTRADAY_RANK = 300            # 場中: 前営業日の売買代金の上位この順位まで
JP_INTRADAY_CAP_OKU = 500         # 場中: または時価総額(億円)がこれ以上
JP_AFTER_RANK = 100               # 引け後: 売買代金の上位この順位まで
JP_AFTER_CAP_OKU = 3000           # 引け後: または時価総額(億円)がこれ以上
YAHOO_TV_URL = ("https://finance.yahoo.co.jp/stocks/ranking/tradingValueHigh"
                "?market=all&term=daily&page={page}")
YAHOO_TV_PAGES = 6                # 1ページ50件。6ページで上位300
YAHOO_TV_GAP = 0.5                # ページ間の間隔(秒)
YAHOO_TV_BUDGET = 60.0            # これを過ぎたら残りのページは読まない(朝の収集を止めない)
# 当日の説明会(株価が動きやすい臨時の説明会。例: 11/10 データセクションの AI データセンター説明会)
IR_QUERIES = ("説明会 開催へ", "説明会を開催")
IR_WITHIN_DAYS = 7

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


def _curl(url: str) -> bytes | None:
    """Nasdaq は Python の TLS 接続を 403 で弾き curl なら通す(2026-11-11 実測)ので curl で取る。失敗は None。"""
    try:
        proc = subprocess.run(["/usr/bin/curl", "-sf", "--compressed", "-m", str(_TIMEOUT), "-A", _UA, url],
                              capture_output=True, timeout=_TIMEOUT + 5)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def _need_nasdaq(url: str) -> bytes:
    body = _curl(url)
    if not body:
        raise _FetchError(f"取得できませんでした: {url}")
    return body


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
    """IRBANK の決算発表予定 → 全行 {code, name, cap(億円), hm((時,分) | None), past([(時,分)])}。

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
        # 発表目安の下の小さい「（16:40,16:20）」は過去の発表時刻(日付だけの括弧もある)
        past = [(int(h), int(m)) for sp in re.findall(r'<span class="co_sm">(.*?)</span>', tds[3][1], re.S)
                for h, m in _HM.findall(_text(sp))]
        out.append({"code": code, "name": name, "cap": int(cap.group(1)),
                    "hm": (int(hm.group(1)), int(hm.group(2))) if hm else None, "past": past})
    return out


def _jp_earnings_item(d: date, r: dict, **extra) -> dict:
    at = _jst(d, *r["hm"]) if r.get("hm") else None
    return _item(at, "earnings", "JP", f"{r['name']}（{r['code']}）決算", 3, day=d,
                 cap=r.get("cap", 0), **extra)


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


def jp_session(r: dict) -> tuple[str, tuple[int, int] | None, str]:
    """IRBANK の1行 → (場中か, 表示の時刻, 固定の表示ラベル)。

    発表目安が 15:30 より前なら場中。目安が引け後でも過去の発表時刻に場中があれば場中とし、
    時刻はその過去の時刻を「前回12:00」と出す(11/10 の実績で、目安だけでは場中の 77/90 しか
    当たらず、過去の時刻も見ると IRBANK に載る場中の全社が当たった)。
    """
    hm = r.get("hm")
    if hm and hm < JP_CLOSE:
        return "intraday", hm, ""
    past = next((p for p in r.get("past") or () if p < JP_CLOSE), None)
    if past:
        return "intraday", past, f"前回{past[0]:02d}:{past[1]:02d}"
    return ("after" if hm else "unknown"), hm, ""


def jp_notable_items(weekly: list[dict] | None, rows: list[dict], d: date,
                     ranks: dict[str, int] | None = None) -> list[dict]:
    """日本の決算予定。weekly=株探の★銘柄(None=取れない)、rows=IRBANK の d の全行、
    ranks={コード: 前営業日の売買代金順位}(取れない日は空=時価総額だけで判定)。

    場中(〜15:30)は値動きを見ながら売買できるので広く出す: ★・売買代金上位 JP_INTRADAY_RANK 位・
    時価総額 JP_INTRADAY_CAP_OKU 億円以上のどれか。引け後は★・上位 JP_AFTER_RANK 位・
    JP_AFTER_CAP_OKU 億円以上のどれか。IRBANK に無い★銘柄は株探の名前・時刻未定で出す。
    各項目に session(intraday/after/unknown)と star が付く。
    """
    ranks = ranks or {}
    stars = {w["code"]: w["name"] for w in weekly or []}
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        if r["code"] in seen:
            continue
        seen.add(r["code"])
        session, hm, label = jp_session(r)
        rank = ranks.get(r["code"])
        star = r["code"] in stars
        if session == "intraday":
            keep = star or (rank is not None and rank <= JP_INTRADAY_RANK) or r["cap"] >= JP_INTRADAY_CAP_OKU
        else:
            keep = star or (rank is not None and rank <= JP_AFTER_RANK) or r["cap"] >= JP_AFTER_CAP_OKU
        if keep:
            out.append(_jp_earnings_item(d, {**r, "hm": hm}, label=label, notable=True,
                                         session=session, star=star))
    for code, name in stars.items():
        if code not in seen:
            seen.add(code)
            out.append(_jp_earnings_item(d, {"code": code, "name": name, "cap": 0}, notable=True,
                                         session="unknown", star=True))
    # 同じ時刻の中は★ → 時価総額の大きい順(select の時刻順の並べ替えは安定なのでこの順が残る)
    out.sort(key=lambda it: (not it["star"], -it["cap"]))
    return out


def parse_yahoo_tv(body: bytes) -> list[dict]:
    """Yahoo!ファイナンスの売買代金ランキング(1ページ50件) → [{rank, code, name}]。
    ページ内の window.__PRELOADED_STATE__ の JSON を読む。読めなければ []。"""
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*</script>",
                  body.decode("utf-8", "replace"), re.S)
    try:
        res = json.loads(m.group(1))["mainRankingList"]["results"] if m else []
    except (ValueError, KeyError, TypeError):
        return []
    out = []
    for r in res or []:
        try:
            out.append({"rank": int(r["rank"]), "code": str(r["stockCode"]),
                        "name": unicodedata.normalize("NFKC", str(r["stockName"]))})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _yahoo_tv() -> list[dict]:
    """売買代金ランキングの上位 YAHOO_TV_PAGES×50 位(直列・間隔を空ける)。1ページ目が駄目なら例外。
    朝07:15 に読むので前営業日の確定値。"""
    out: list[dict] = []
    deadline = time.monotonic() + YAHOO_TV_BUDGET
    for page in range(1, YAHOO_TV_PAGES + 1):
        if page > 1:
            if time.monotonic() >= deadline:
                break
            time.sleep(YAHOO_TV_GAP)
        got = parse_yahoo_tv(_get(YAHOO_TV_URL.format(page=page)) or b"")
        if not got:
            if page == 1:
                raise _FetchError("Yahoo 売買代金ランキングを取得できませんでした")
            break
        out += got
    return out


_IR_EXCLUDE = ("決算説明会", "見合わせ", "中止", "延期", "終了", "報告", "開催した", "開催しました")
# 「10日に」「11月25日(水)」「11/25」。「7日ぶり」「11月5日(適時開示)」(開示日)は日付にしない
_IR_DAY = re.compile(r"(?:(\d{1,2})月)?(\d{1,2})日(?:に|の|午前|午後|\d{1,2}時|\s|\([月火水木金土日])"
                     r"|(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
_IR_TIME = re.compile(r"(\d{1,2})時(?:(\d{2})分|半)?|(\d{1,2}):(\d{2})")
_CORP = re.compile(r"\(株\)|株式会社")


def _corp_names(name: str) -> list[str]:
    """Yahoo の銘柄名 → 見出しで探す名前(「(株)」を除き、ホールディングスは HD・無しも、グループは G も)。
    グループを外した名前は別会社と重なる(ソフトバンクグループ→ソフトバンク)ので使わない。"""
    base = _CORP.sub("", unicodedata.normalize("NFKC", name)).strip()
    names = [base]
    if "ホールディングス" in base:
        names += [base.replace("ホールディングス", "HD"), base.replace("ホールディングス", "")]
    if "グループ" in base:
        names.append(base.replace("グループ", "G"))
    return [n for n in dict.fromkeys(names) if len(n) >= 2]


def ir_events(titles: list[str], d: date, tv: list[dict]) -> list[dict]:
    """「説明会」の告知記事の見出し → d に開く説明会のうち、売買代金上位(tv)の銘柄のもの。

    決算説明会は決算と重なるので除き、中止・延期・終わった報告も除く。日付は見出しの
    「10日に」「11月10日」で判定する(「7日ぶり」は日付にしない)。時刻が見出しにあれば使う。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for raw in titles:
        t = unicodedata.normalize("NFKC", raw)
        if "説明会" not in t or any(x in t for x in _IR_EXCLUDE):
            continue
        days = [(m or m2, dd or dd2) for m, dd, m2, dd2 in _IR_DAY.findall(t)]
        if not any((not m or int(m) == d.month) and int(dd) == d.day for m, dd in days):
            continue
        hit = next((r for r in tv if r["code"] not in seen
                    and any(n in t for n in _corp_names(r["name"]))), None)
        if not hit:
            continue
        seen.add(hit["code"])
        tm = _IR_TIME.search(t)
        at = None
        if tm:
            h = int(tm.group(1) or tm.group(3))
            mi = int(tm.group(2) or tm.group(4) or (30 if tm.group(0).endswith("半") else 0))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                at = _jst(d, h, mi)
        name = _CORP.sub("", hit["name"]).strip()
        out.append(_item(at, "ir", "JP", f"{name}（{hit['code']}）説明会", 3, day=d, notable=True,
                         session="intraday" if at and (at.hour, at.minute) < JP_CLOSE else "unknown"))
    return out


def _ir_titles() -> list[str]:
    """説明会の告知記事の見出し(Google ニュース RSS・直近 IR_WITHIN_DAYS 日)。"""
    titles: list[str] = []
    for q in IR_QUERIES:
        url = newsfeeds.GOOGLE_NEWS + "?" + urllib.parse.urlencode(
            {"q": f"{q} when:{IR_WITHIN_DAYS}d", "hl": "ja", "gl": "JP", "ceid": "JP:ja"})
        body = _get(url)
        if body:
            titles += [it.title for it in newsfeeds.parse_google_rss(body)]
    return titles


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
    """日本の決算予定(株探の★・IRBANK の全社・Yahoo の売買代金順位)と、当日の説明会。
    株探・Yahoo・Google ニュースが駄目でも IRBANK があれば出す(★や順位の条件が抜けるだけ)。"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_tv = pool.submit(_yahoo_tv)
        f_ir = pool.submit(_ir_titles)
        try:
            weekly = _kabutan_weekly(d)
        except Exception as e:
            print(f"  予定: 株探(今週の決算発表予定) 取得失敗のため★なしで判定 ({type(e).__name__}: {e})",
                  file=sys.stderr)
            weekly = None
        try:
            rows = _irbank_rows(_need(IRBANK_URL.format(d=d)), d)
        except _FetchError:
            if not weekly:
                raise
            rows = []   # 株探の★だけ(名前は株探・時刻未定)
        try:
            tv = f_tv.result()
        except Exception as e:
            print(f"  予定: Yahoo(売買代金) 取得失敗のため時価総額だけで判定 ({type(e).__name__}: {e})",
                  file=sys.stderr)
            tv = []
        try:
            titles = f_ir.result()
        except Exception as e:
            print(f"  予定: Google ニュース(説明会) 取得失敗のため省略 ({type(e).__name__}: {e})",
                  file=sys.stderr)
            titles = []
    ranks = {r["code"]: r["rank"] for r in tv}
    return jp_notable_items(weekly, rows, d, ranks) + ir_events(titles, d, tv)


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
    rows = _nasdaq_rows(_need_nasdaq(NASDAQ_URL.format(d=p)), SURPRISE_US_MIN_CAP)
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
    for flag in ("notable", "star"):   # 注目決算の印(付くのは決算だけ。無い項目にはキー自体を出さない)
        if it.get(flag):
            out[flag] = True
    if it.get("session"):                # 日本の決算・説明会: intraday(場中)/after(引け後)/unknown
        out["session"] = it["session"]
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


def _jp_dump(items: list[dict]) -> list[dict]:
    """日本の決算の内部表現 → JSON にできる形(at・day を ISO 文字列に。キーの無い項目には足さない)。"""
    return [{**it, **{k: it[k].isoformat() for k in ("at", "day") if it.get(k)}} for it in items]


def _jp_load(items: list[dict]) -> list[dict]:
    """_jp_dump の逆。JSON から読んだ項目を live 取得と同じ内部表現(at=JST の datetime、day=date)に戻す。"""
    out = []
    for it in items:
        it = dict(it)
        if it.get("at"):
            it["at"] = datetime.fromisoformat(it["at"]).astimezone(JST)
        if it.get("day"):
            it["day"] = date.fromisoformat(it["day"])
        out.append(it)
    return out


def _jp_dates(now: datetime | None) -> tuple[datetime, date, date]:
    if now is None:
        now = datetime.now(JST)
    now = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    d = now.date()
    return now, d, prev_weekday(d)


def fetch_jp(now: datetime | None = None) -> dict:
    """日本の注目決算と決算サプライズ(日本)だけを取る。株探・IRBANK はクラウドの IP を弾くので、
    Mac(scripts/push_jp_schedule.py)で取って D1 経由でクラウドの fetch(jp=...) に渡す。
    失敗した側は None(空で取れた [] と区別する)。値は JSON にできる形(_jp_dump)。"""
    now, d, p = _jp_dates(now)
    _kabutan_deadline[0] = time.monotonic() + KABUTAN_BUDGET

    def run(name: str, fn) -> list[dict] | None:
        try:
            return _jp_dump(fn())
        except Exception as e:
            print(f"  予定: {name} 取得失敗のため省略 ({type(e).__name__}: {e})", file=sys.stderr)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_n = pool.submit(run, "株探・IRBANK(日本の注目決算)", lambda: _jp_notable(d))
        f_s = pool.submit(run, "決算サプライズ(日本)", lambda: _jp_surprise_job(d, p))
        notable, surprises = f_n.result(), f_s.result()
    return {"date": d.isoformat(), "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
            "notable": notable, "surprises": surprises}


def fetch(now: datetime | None = None, jp: dict | None = None) -> dict:
    """{"schedule": now〜翌朝8時の予定, "results": 直近24時間に出た指標の結果,
    "surprises": 前営業日の決算への市場の反応}。

    now の既定は現在の JST(収集は配信の45分前 07:15 JST に走る)。tz の無い now は JST とみなす。
    取得元は並列に1回ずつ読む(どれかが遅くても全体は最も遅い1つ分で済む)。
    株探へのアクセスは複数のジョブにまたがるが、_kabutan_get が全体で直列にする。
    jp(fetch_jp の結果)が同じ日付なら、None でない方(注目決算/サプライズ)は live で取らずにそれを使う。
    """
    now, d, p = _jp_dates(now)
    pre_notable = pre_surprise = None
    if jp and jp.get("date") == d.isoformat():
        if jp.get("notable") is not None:
            pre_notable = _jp_load(jp["notable"])
        if jp.get("surprises") is not None:
            pre_surprise = _jp_load(jp["surprises"])
        if pre_notable is not None or pre_surprise is not None:
            print(f"  予定: 日本の決算(注目{'○' if pre_notable is not None else '×'}"
                  f"/サプライズ{'○' if pre_surprise is not None else '×'})は Mac で取得した分を使用 "
                  f"({jp.get('fetched_at')})", file=sys.stderr)
    _kabutan_deadline[0] = time.monotonic() + KABUTAN_BUDGET
    jobs: list[tuple[str, object]] = [
        # 前日〜翌日の3日分(前日分は results 用、翌日分は翌朝8時までの予定用)
        ("みんかぶ(経済指標)",
         lambda: indicators(parse_minkabu(_need(MINKABU_URL.format(d=d - timedelta(days=1)))))),
        ("FRB", lambda: parse_frb(_need(FRB_URL))),
        ("日銀", lambda: boj_events(parse_boj(_need(BOJ_URL)))),
        # 米国決算は配信日と同じ日付(米東部の当日)。日本時間の夜〜翌朝に出る。
        ("Nasdaq(米国の注目決算)", lambda: parse_nasdaq(_need_nasdaq(NASDAQ_URL.format(d=d)), d)),
        ("株探・IRBANK(日本の注目決算)",
         (lambda: pre_notable) if pre_notable is not None else (lambda: _jp_notable(d))),
    ]
    surprise_jobs: list[tuple[str, object]] = [
        ("決算サプライズ(日本)",
         (lambda: pre_surprise) if pre_surprise is not None else (lambda: _jp_surprise_job(d, p))),
        ("決算サプライズ(米国)", lambda: _us_surprise_job(p)),
    ]
    with ThreadPoolExecutor(max_workers=len(jobs) + len(surprise_jobs)) as pool:
        got = list(pool.map(_safe, jobs + surprise_jobs))
    items = [it for items in got[:len(jobs)] for it in items]
    surprises = [it for items in got[len(jobs):] for it in items]
    return {"schedule": select(items, now), "results": recent_results(got[0], now),
            "surprises": surprises}
