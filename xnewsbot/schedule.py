"""「今日の予定」(経済指標・金融政策・要人発言・決算)を無料の公開ページから集める。APIキー不要。

配信(08:00 JST)の要点に「次の配信までに何があるか」を出すためと(本人要望 2026-10-01)、
キュレーションが指標の「予想比」を書く材料(直近24時間に出た結果)を渡すため。
戻り値の形は表示担当(line_client)・models.ScheduleSnapshot との契約なので変えない:
  {"schedule": [item, ...], "results": [item, ...]}
  item = {"at": ISO8601(JST) | None, "time_label", "kind", "country", "name",
          "forecast", "previous", "result", "importance"}
  time_label と name は表示にそのまま使う完成形(表示側は加工しない)。

取得元(どれも鍵なし。2026-10-01 に実ページを1回ずつ取得して構造を確認):
- みんかぶFX 経済指標カレンダー(HTML): 重要度1〜5・国・JST 時刻・予想/前回/結果。
  `date=D&days=N` で D から N 日分の表(日付ごとの caption)が返る。前日分は results 用。
- FRB calendar.json(UTF-8 BOM 付き): FOMC 声明・議長会見・議事録・議長の講演/証言だけ。時刻は米東部。
- 日銀 金融政策決定会合の日程(HTML): 会合の最終日に結果発表と総裁会見。
- Nasdaq 決算カレンダー(JSON。ブラウザ風 UA が必要): 時価総額200億ドル以上の上位8社。
- IRBANK 決算発表予定(HTML): 時価総額1000億円以上の上位8社。
取得元ごとに失敗しても他は返す(失敗は stderr に1行・その取得元は空)。配信は止めない。
"""

from __future__ import annotations

import gzip
import html
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")

MINKABU_URL = "https://fx.minkabu.jp/indicators?country=all&date={d}&days=3"
FRB_URL = "https://www.federalreserve.gov/json/calendar.json"
BOJ_URL = "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
IRBANK_URL = "https://irbank.net/market/kessan?y={d}"

# Nasdaq は素の urllib UA だと応答しないことがあるのでブラウザ風 UA を付ける(newsfeeds と同じ)。
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
_TIMEOUT = 20

DELIVER_HOUR = 8          # 翌朝のこの時刻(JST)までを「今日の予定」にする(次の定時配信まで)
MAX_ITEMS = 20            # 予定の最大件数(多いときは重要度の低いものから落とす)
# 「昼ごろ」「寄り前」「引け後」の at は並べ替え用の近似なので、過ぎてもこの時間までは未発表とみなして残す
APPROX_GRACE = timedelta(hours=3)
EARNINGS_MAX = 8          # 決算は日米それぞれ時価総額の上位この数まで
US_MIN_CAP = 20_000_000_000   # 米国決算の時価総額下限(ドル)
JP_MIN_CAP_OKU = 1000         # 日本決算の時価総額下限(億円)

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
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
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


# --- 決算(米国: Nasdaq / 日本: IRBANK) ---

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


def parse_nasdaq(body: bytes, d: date) -> list[dict]:
    """Nasdaq 決算カレンダー → 時価総額200億ドル以上の上位8社(同名の別クラス株は1社にまとめる)。

    at は並べ替え用の近似: 寄り前≈当日22:00 JST(米国の寄り付き前)、引け後≈翌05:30 JST(引け後)。
    """
    rows = ((json.loads(body.decode("utf-8")).get("data") or {}).get("rows")) or []
    picked: list[tuple[int, str, str, str]] = []
    seen: set[str] = set()
    for r in rows:
        cap = _usd(r.get("marketCap"))
        name, sym = _us_company(r.get("name")), (r.get("symbol") or "").strip()
        if cap < US_MIN_CAP or not name or not sym or name in seen:
            continue
        seen.add(name)
        picked.append((cap, name, sym, r.get("time") or ""))
    picked.sort(key=lambda p: -p[0])
    out: list[dict] = []
    for _cap, name, sym, t in picked[:EARNINGS_MAX]:
        if t == "time-pre-market":
            at, label = _jst(d, 22, 0), "寄り前"
        elif t == "time-after-hours":
            at, label = _jst(d + timedelta(days=1), 5, 30), "引け後"
        else:
            at, label = None, "未定"
        out.append(_item(at, "earnings", "US", f"{name}（{sym}）決算", 3, day=d, label=label))
    return out


def parse_irbank(body: bytes, d: date) -> list[dict]:
    """IRBANK の決算発表予定 → 時価総額1000億円以上の上位8社。時刻は発表目安(なければ未定)。

    行は [コード, 会社名, 決算種別, 発表目安, 時価総額(sortValue=億円), ...] の td。
    見出しの日付が d と違う一覧(休日に翌営業日が出る等)は使わない。
    """
    s = body.decode("utf-8", "replace")
    h = re.search(r"<h2[^>]*>\s*(\d{4})年(\d{1,2})月(\d{1,2})日発表予定", s)
    if not h or date(*(int(x) for x in h.groups())) != d:
        return []
    table = re.search(r"<table[^>]*>(.*?)</table>", s[h.end():], re.S)
    rows: list[tuple[int, str, str, re.Match | None]] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1) if table else "", re.S):
        tds = _TD.findall(tr)
        if len(tds) < 5:
            continue
        code, name = _text(tds[0][1]), _text(tds[1][1])
        cap = re.search(r"sortValue:(\d+)", tds[4][0])
        if not code or not name or not cap or int(cap.group(1)) < JP_MIN_CAP_OKU:
            continue
        rows.append((int(cap.group(1)), code, name, _HM.match(_text(tds[3][1]))))
    rows.sort(key=lambda r: -r[0])
    return [_item(_jst(d, int(hm.group(1)), int(hm.group(2))) if hm else None, "earnings", "JP",
                  f"{name}（{code}）決算", 3, day=d)
            for _cap, code, name, hm in rows[:EARNINGS_MAX]]


# --- まとめ ---

def _out(it: dict, d: date) -> dict:
    at = it["at"]
    if it["label"]:
        label = it["label"]
    elif at is None:
        label = "未定"
    else:
        label = ("翌" if at.date() > d else "") + at.strftime("%H:%M")
    return {"at": at.isoformat() if at else None, "time_label": label, "kind": it["kind"],
            "country": it["country"], "name": it["name"], "forecast": it["forecast"],
            "previous": it["previous"], "result": it["result"], "importance": it["importance"]}


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
    kept = sorted(kept, key=lambda it: -it["importance"])[:MAX_ITEMS]
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
    """{"schedule": now〜翌朝8時の予定, "results": 直近24時間に出た指標の結果}。

    now の既定は現在の JST(収集は配信の45分前 07:15 JST に走る)。tz の無い now は JST とみなす。
    取得元は並列に1回ずつ読む(どれかが遅くても全体は最も遅い1つ分で済む)。
    """
    if now is None:
        now = datetime.now(JST)
    now = now.replace(tzinfo=JST) if now.tzinfo is None else now.astimezone(JST)
    d = now.date()
    jobs: list[tuple[str, object]] = [
        # 前日〜翌日の3日分(前日分は results 用、翌日分は翌朝8時までの予定用)
        ("みんかぶ(経済指標)",
         lambda: indicators(parse_minkabu(_need(MINKABU_URL.format(d=d - timedelta(days=1)))))),
        ("FRB", lambda: parse_frb(_need(FRB_URL))),
        ("日銀", lambda: boj_events(parse_boj(_need(BOJ_URL)))),
        # 米国決算は配信日と同じ日付(米東部の当日)。日本時間の夜〜翌朝に出る。
        ("Nasdaq(米国決算)", lambda: parse_nasdaq(_need(NASDAQ_URL.format(d=d)), d)),
        ("IRBANK(日本決算)", lambda: parse_irbank(_need(IRBANK_URL.format(d=d)), d)),
    ]
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        got = list(pool.map(_safe, jobs))
    items = [it for items in got for it in items]
    return {"schedule": select(items, now), "results": recent_results(got[0], now)}
