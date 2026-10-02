"""「今日の予定」(schedule)の単体テスト。ネットワークは使わない(実ページを削った tests/fixtures を読む)。"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from xnewsbot import schedule as sc

FIX = Path(__file__).resolve().parent / "fixtures"
J = sc.JST


def _fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def _at(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=J)


# --- みんかぶ(経済指標) ---

def test_parse_minkabu_rows_and_values():
    rows = sc.parse_minkabu(_fx("minkabu_2026-09-30.html"))
    adp = next(r for r in rows if "ADP" in r["raw_name"])
    assert adp["day"] == date(2026, 9, 30) and adp["at"] == _at(date(2026, 9, 30), 21, 15)
    assert (adp["country"], adp["importance"]) == ("US", 4)
    assert (adp["previous"], adp["forecast"], adp["result"]) == ("3.8万人", "7.0万人", "9.0万人")
    # 未発表の「---」は空文字
    nfp = next(r for r in rows if "非農業部門" in r["raw_name"])
    assert nfp["result"] == ""


def test_indicator_name_forms():
    assert sc.indicator_name("アメリカ・雇用統計 09月 [非農業部門雇用者数・前月比]", "US") \
        == "米 雇用統計（非農業部門雇用者数）"
    assert sc.indicator_name("アメリカ・PCE価格指数 09月 [コアPCE価格指数・前年比]", "US") \
        == "米 コアPCE価格指数"
    assert sc.indicator_name("アメリカ・耐久財受注（確報値） 08月 [輸送除くコア・前月比]", "US") \
        == "米 耐久財受注（確報値・輸送除くコア）"
    # 全角英数は半角、対象期間は落とす
    assert sc.indicator_name("アメリカ・実質ＧＤＰ（速報値） 第3四半期 [実質GDP・前期比年率]", "US") \
        == "米 実質GDP（速報値）"
    # 日銀/FRB/ECB で始まる名前には国名を付けない
    assert sc.indicator_name("日本・日銀短観 第3四半期 [大企業製造業・業況判断]", "JP") \
        == "日銀短観（大企業製造業・業況判断）"
    assert sc.indicator_name("日本・機械受注 08月 [前月比]", "JP") == "日 機械受注"
    assert sc.indicator_name("日本・機械受注 08月 [前月比]", "JP", full=True) == "日 機械受注（前月比）"


def test_indicators_filters_country_and_importance():
    items = sc.indicators(sc.parse_minkabu(_fx("minkabu_2026-09-30.html")))
    assert items and all(it["country"] in sc.COUNTRIES for it in items)
    assert all(it["importance"] >= sc.COUNTRIES[it["country"]][1] for it in items)
    names = [it["name"] for it in items]
    assert "米 雇用統計（非農業部門雇用者数）" in names and "米 雇用統計（失業率）" in names
    assert not any(it["country"] == "DE" for it in items)   # 表に無い国(ドイツ)は出さない


def test_indicators_policy_rows_and_duplicate_names():
    items = sc.indicators(sc.parse_minkabu(_fx("minkabu_2026-10-28.html")))
    fomc = [it for it in items if "FRB政策金利" in it["name"]]
    # 上限/下限の2行は1件にまとめ、kind=policy・前回は上限金利
    assert len(fomc) == 1 and fomc[0]["kind"] == "policy" and fomc[0]["previous"] == "4.00%"
    # 重要度3の ECB のファシリティ金利は出さない(EU は5だけ)
    assert [it["name"] for it in items if it["country"] == "EU" and it["kind"] == "policy"] \
        == ["ECB政策金利"]
    # 同じ時刻に同じ名前になる GDP の前期比/前年比は詳細を残して区別する
    eu_gdp = [it["name"] for it in items if it["country"] == "EU" and "GDP" in it["name"]]
    assert len(set(eu_gdp)) == len(eu_gdp)


# --- FRB ---

def test_parse_frb_et_to_jst_and_titles():
    got = {(it["name"], it["at"]) for it in sc.parse_frb(_fx("frb_calendar.json"))}
    # 10/28 14:00 EDT = 10/29 03:00 JST、12/9 14:00 EST(冬時間) = 12/10 04:00 JST
    assert ("米 FOMC 政策金利発表", _at(date(2026, 10, 29), 3)) in got
    assert ("米 FOMC 政策金利発表", _at(date(2026, 12, 10), 4)) in got
    assert ("FRB議長 会見", _at(date(2026, 10, 29), 3, 30)) in got
    assert ("米 FOMC 議事録", _at(date(2026, 10, 8), 3)) in got
    assert ("パウエルFRB議長 講演", _at(date(2026, 3, 22), 2, 30)) in got
    # 2026年夏からの「Chairman」表記も議長として拾う
    assert ("ウォーシュFRB議長 議会証言", _at(date(2026, 7, 14), 23)) in got
    names = {n for n, _ in got}
    assert not any("Jefferson" in n or "Waller" in n for n in names)   # 副議長・理事・統計は出さない
    assert len(got) == 6


def test_frb_unknown_chair_has_no_name():
    spec = sc._frb_spec("Speech - Chair Jane Doe")
    assert spec["name"] == "FRB議長 講演"
    assert sc._frb_spec("Speech - Vice Chair Philip N. Jefferson") is None


# --- 日銀 ---

def test_parse_boj_last_days_and_events():
    days = sc.parse_boj(_fx("boj_mpmsche.html"))
    assert date(2026, 10, 30) in days and date(2026, 12, 18) in days
    assert date(2026, 10, 29) not in days   # 2日間の会合は最終日だけ
    ev = sc.boj_events([date(2026, 10, 30)])
    assert [(e["name"], e["label"], e["at"]) for e in ev] == [
        ("日銀 金融政策決定会合 結果発表", "昼ごろ", _at(date(2026, 10, 30), 12)),
        ("日銀総裁 会見", "", _at(date(2026, 10, 30), 15, 30)),
    ]


# --- 決算 ---

def test_parse_nasdaq_cap_cut_and_labels():
    d = date(2026, 10, 1)
    out = sc.parse_nasdaq(_fx("nasdaq_earnings_2026-10-01.json"), d)
    # 注目決算は500億ドル以上(Nike 525億ドルは入る・McCormick 125億ドルは対象外)。法人格の語は落とす
    assert [(it["name"], it["label"], it["at"], it["notable"]) for it in out] == [
        ("Accenture（ACN）決算", "寄り前", _at(d, 22), True),
        ("Nike（NKE）決算", "引け後", _at(date(2026, 10, 2), 5, 30), True)]


def test_parse_nasdaq_no_limit_dedupe_and_undecided():
    import json
    rows = [{"name": f"Big{i} Corp.", "symbol": f"B{i}", "marketCap": f"${(130 + i) * 10**9:,}",
             "time": "time-not-supplied"} for i in range(10)]
    rows.append({"name": "Big9 Corp.", "symbol": "B9.X", "marketCap": "$139,000,000,000",
                 "time": "time-pre-market"})   # 同名の別クラス株
    body = json.dumps({"data": {"rows": rows}}).encode()
    out = sc.parse_nasdaq(body, date(2026, 10, 1))
    assert len(out) == 10                       # 上限なし(8社で切らない)
    assert out[0]["name"] == "Big9（B9）決算" and out[-1]["name"] == "Big0（B0）決算"
    assert all(it["at"] is None and it["label"] == "未定" for it in out)


def test_us_company_strips_suffixes():
    assert sc._us_company("Nike, Inc.") == "Nike"
    assert sc._us_company("McCormick & Company, Incorporated") == "McCormick & Company"
    assert sc._us_company("Accenture plc") == "Accenture"


def test_parse_irbank_cap_cut_and_date_check():
    d = date(2026, 10, 1)
    out = sc.parse_irbank(_fx("irbank_kessan_2026-10-01.html"), d)
    assert [it["name"] for it in out] == [
        "クスリのアオキホールディングス（3549）決算", "西松屋チェーン（7545）決算", "平和堂（8276）決算"]
    assert out[0]["at"] == _at(d, 15) and out[0]["country"] == "JP"
    # 見出しの日付が違う一覧(休日に翌営業日が出る等)は使わない
    assert sc.parse_irbank(_fx("irbank_kessan_2026-10-01.html"), date(2026, 10, 2)) == []


# --- まとめ(期間・並び・「翌」) ---

def _ev(at, name, importance=4, **kw):
    return sc._item(at, kw.pop("kind", "indicator"), kw.pop("country", "US"), name, importance, **kw)


def test_select_window_until_next_morning_8am():
    d = date(2026, 10, 1)
    now = _at(d, 7, 15)
    items = [
        _ev(_at(d, 7, 0), "過去"),
        _ev(_at(d, 7, 15), "ちょうど今"),
        _ev(_at(d, 21, 30), "今夜"),
        _ev(_at(date(2026, 10, 2), 8, 0), "翌朝8時ちょうど"),
        _ev(_at(date(2026, 10, 2), 8, 1), "翌朝8時過ぎ"),
        _ev(None, "当日未定", day=d),
        _ev(None, "翌日未定", day=date(2026, 10, 2)),
    ]
    out = sc.select(items, now)
    assert [(x["time_label"], x["name"]) for x in out] == [
        ("07:15", "ちょうど今"), ("21:30", "今夜"), ("翌08:00", "翌朝8時ちょうど"), ("未定", "当日未定")]
    assert out[1]["at"] == "2026-10-01T21:30:00+09:00"
    assert set(out[0]) == {"at", "time_label", "kind", "country", "name",
                           "forecast", "previous", "result", "importance"}


def test_select_caps_by_importance_then_sorts_by_time():
    d = date(2026, 10, 1)
    items = [_ev(_at(d, 9, i), f"低{i}", 3) for i in range(25)] + [_ev(_at(d, 23), "高", 5)]
    out = sc.select(items, _at(d, 7, 15))
    assert len(out) == sc.MAX_ITEMS
    assert out[-1]["name"] == "高"                       # 重要度の高いものは残る
    assert [x["at"] for x in out] == sorted(x["at"] for x in out)


def test_select_merges_minkabu_rate_row_into_fomc():
    """FRB の政策発表があれば、みんかぶの政策金利の行は重ねず予想・前回だけ移す。"""
    items = (sc.indicators(sc.parse_minkabu(_fx("minkabu_2026-10-28.html")))
             + sc.parse_frb(_fx("frb_calendar.json")))
    out = sc.select(items, _at(date(2026, 10, 28), 7, 15))
    assert [(x["time_label"], x["name"], x["previous"]) for x in out] == [
        ("翌03:00", "米 FOMC 政策金利発表", "4.00%"), ("翌03:30", "FRB議長 会見", "")]


def test_select_boj_day():
    items = (sc.indicators(sc.parse_minkabu(_fx("minkabu_2026-10-28.html")))
             + sc.boj_events(sc.parse_boj(_fx("boj_mpmsche.html"))))
    out = sc.select(items, _at(date(2026, 10, 30), 7, 15))
    labels = [(x["time_label"], x["name"]) for x in out]
    assert labels[:2] == [("昼ごろ", "日銀 金融政策決定会合 結果発表"), ("15:30", "日銀総裁 会見")]
    assert not any("日銀政策金利" in n for _, n in labels)   # みんかぶの未定行は結果発表へまとめた


def test_select_marks_next_day_and_keeps_fixed_labels():
    d = date(2026, 10, 1)
    items = (sc.parse_nasdaq(_fx("nasdaq_earnings_2026-10-01.json"), d, min_cap=sc.SURPRISE_US_MIN_CAP)
             + sc.parse_irbank(_fx("irbank_kessan_2026-10-01.html"), d)
             + sc.indicators(sc.parse_minkabu(_fx("minkabu_2026-09-30.html"))))
    out = sc.select(items, _at(d, 7, 15))
    labels = [x["time_label"] for x in out]
    assert "寄り前" in labels and "引け後" in labels   # 決算の時刻は近似なので固定の表示
    assert labels[-1] == "引け後"                       # 引け後≈翌05:30 で並ぶ
    assert all(not lb.startswith("翌") for lb in labels)  # 翌朝の指標は無い日


def test_recent_results_last_24h():
    items = sc.indicators(sc.parse_minkabu(_fx("minkabu_2026-09-30.html")))
    out = sc.recent_results(items, _at(date(2026, 10, 1), 7, 15))
    assert [x["name"] for x in out] == ["米 ADP雇用者数", "米 PCE価格指数", "米 コアPCE価格指数",
                                        "米 実質GDP（確報値）"]
    assert out[0]["result"] == "9.0万人" and out[0]["time_label"] == "21:15"


# --- fetch: 取得元の失敗は他に影響しない ---

_FILES = {
    "fx.minkabu.jp": "minkabu_2026-09-30.html", "federalreserve.gov": "frb_calendar.json",
    "boj.or.jp": "boj_mpmsche.html", "nasdaq.com": "nasdaq_earnings_2026-10-01.json",
    "irbank.net": "irbank_kessan_2026-10-01.html",
}


@pytest.fixture(autouse=True)
def _fast_fetch(monkeypatch):
    """fetch は株探・Yahoo にも触る。待ち時間とネットワークを無効にする(決算サプライズは別ファイルで検証)。"""
    monkeypatch.setattr(sc, "KABUTAN_GAP", 0)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: None)


def _fake_get(url):
    if "kabutan.jp" in url:
        return None                      # 株探は取れない(日本の注目決算は IRBANK の上位で代用される)
    for host, name in _FILES.items():
        if host in url:
            return _fx(name)
    raise AssertionError(url)


def test_fetch_combines_sources(monkeypatch):
    seen = []
    monkeypatch.setattr(sc, "_get", lambda url: seen.append(url) or _fake_get(url))
    got = sc.fetch(datetime(2026, 10, 1, 7, 15))   # tz 無しは JST とみなす
    names = [x["name"] for x in got["schedule"]]
    assert "米 ISM製造業景気指数" in names and "Accenture（ACN）決算" in names
    assert "Nike（NKE）決算" in names          # 注目決算は500億ドル以上(Nike 525億ドルは入る)
    assert "平和堂（8276）決算" in names       # 株探が取れないので IRBANK の上位で代用
    assert [x["name"] for x in got["results"]][0] == "米 ADP雇用者数"
    # みんかぶは前日から3日分、決算は配信日の日付で引く
    assert any("date=2026-09-30&days=3" in u for u in seen)
    assert any("nasdaq.com" in u and "date=2026-10-01" in u for u in seen)
    assert any("irbank.net" in u and "y=2026-10-01" in u for u in seen)


def test_fetch_source_failure_is_isolated(monkeypatch, capsys):
    def get(url):
        if "fx.minkabu.jp" in url:
            return None                       # 取得失敗(None)
        if "nasdaq.com" in url:
            raise RuntimeError("boom")        # 想定外の例外
        if "irbank.net" in url:
            return b"<html>broken</html>"     # 壊れたページ(結果は空)
        return _fake_get(url)
    monkeypatch.setattr(sc, "_get", get)
    got = sc.fetch(datetime(2026, 10, 28, 7, 15, tzinfo=J))
    assert [x["name"] for x in got["schedule"]] == ["米 FOMC 政策金利発表", "FRB議長 会見"]
    assert got["results"] == []
    err = capsys.readouterr().err
    assert "みんかぶ" in err and "Nasdaq" in err


def test_fetch_all_failed_returns_empty(monkeypatch):
    monkeypatch.setattr(sc, "_get", lambda url: None)
    assert sc.fetch(datetime(2026, 10, 1, 7, 15, tzinfo=J)) == {"schedule": [], "results": [], "surprises": []}


def test_select_keeps_approx_time_for_grace_period():
    """「昼ごろ」など近似時刻の予定は、近似時刻を過ぎても猶予の間は残す(12:30 の自動復旧で日銀が消えない)。"""
    d = date(2026, 10, 30)
    boj = _ev(_at(d, 12, 0), "日銀 金融政策決定会合 結果発表", 5, kind="policy", country="JP",
              label="昼ごろ")
    exact = _ev(_at(d, 12, 0), "確定時刻の予定", 5)
    names = [r["name"] for r in sc.select([boj, exact], _at(d, 12, 30))]
    assert names == ["日銀 金融政策決定会合 結果発表"]
    assert sc.select([boj], _at(d, 15, 30)) == []
