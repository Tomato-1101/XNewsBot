"""注目決算(株探の★・Nasdaq 500億ドル以上)と決算サプライズ(前営業日の決算への市場の反応)の単体テスト。

ネットワークは使わない(実ページを削った tests/fixtures と手組みのデータ)。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from xnewsbot import schedule as sc

FIX = Path(__file__).resolve().parent / "fixtures"
J = sc.JST
ET = ZoneInfo("America/New_York")


def _fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


@pytest.fixture(autouse=True)
def _no_kabutan_wait(monkeypatch):
    monkeypatch.setattr(sc, "KABUTAN_GAP", 0)


def _at(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=J)


# --- 株探: 今週の決算発表予定 ---

def test_pick_kabutan_weekly_by_date_range():
    top = _fx("kabutan_top_links.html")
    # リンク文字の「(9月28日～10月2日)」に d を含む週だけ採る。他の記事(サプライズ決算など)は無視
    assert sc.pick_kabutan_weekly(top, date(2026, 10, 1)) == "https://kabutan.jp/news/marketnews/?b=n202609270056"
    assert sc.pick_kabutan_weekly(top, date(2026, 9, 28)) is not None
    assert sc.pick_kabutan_weekly(top, date(2026, 10, 5)) is None
    assert sc.pick_kabutan_weekly(b"<html></html>", date(2026, 10, 1)) is None


def test_pick_kabutan_weekly_unparsable_range_is_second_choice():
    body = '<a href="/news/marketnews/?b=n1">今週の決算発表予定 トヨタなど</a>'.encode()
    assert sc.pick_kabutan_weekly(body, date(2026, 10, 1)) == "https://kabutan.jp/news/marketnews/?b=n1"


def test_parse_kabutan_weekly_stars_with_ragged_spacing():
    body = _fx("kabutan_weekly_2026-08-03.html")
    # ★の前の空白(全角・半角)の数は行ごとにずれる。★の行だけを、名前は全角英数を半角にして拾う
    assert sc.parse_kabutan_weekly(body, date(2026, 8, 3)) == [
        {"code": "7003", "name": "三井E&S"}, {"code": "7201", "name": "日産自"},
        {"code": "8001", "name": "伊藤忠"}, {"code": "8002", "name": "丸紅"},
        {"code": "8058", "name": "三菱商"}, {"code": "8306", "name": "三菱UFJ"}]
    # 件数の多い日(「など」で省略される)・★の後ろに「川重 [東Ｐ] ★」のようにずれのない行
    d7 = sc.parse_kabutan_weekly(body, date(2026, 8, 7))
    assert [w["code"] for w in d7][:3] == ["1605", "5706", "5803"] and len(d7) == 11
    assert {"code": "7012", "name": "川重"} in d7


def test_parse_kabutan_weekly_no_heading_vs_no_stars():
    body = _fx("kabutan_weekly_2026-09-28.html")
    assert sc.parse_kabutan_weekly(body, date(2026, 9, 28)) == [{"code": "8227", "name": "しまむら"}]
    assert sc.parse_kabutan_weekly(body, date(2026, 10, 1)) == []     # 見出しはあるが★は0件
    assert sc.parse_kabutan_weekly(body, date(2026, 10, 3)) is None   # 記事に見出しが無い日
    # 公開日から2週間以上離れた記事は使わない(古い週の記事を今日の予定にしない)
    assert sc.parse_kabutan_weekly(body, date(2026, 11, 20)) is None


# --- 日本の注目決算(株探 ★ + IRBANK の発表目安 / 代用) ---

def _rows():
    return sc._irbank_rows(_fx("irbank_kessan_2026-10-01.html"), date(2026, 10, 1))


def test_jp_notable_items_join_irbank_time_and_name():
    d = date(2026, 10, 1)
    weekly = [{"code": "7545", "name": "西松屋チェ"}, {"code": "9999", "name": "IRBANKに無い"},
              {"code": "7545", "name": "重複"}]
    out = sc.jp_notable_items(weekly, _rows(), d)
    # 会社名は IRBANK の正式名、IRBANK に無い銘柄は株探の名前・時刻は未定
    assert [(it["name"], it["at"], it["notable"]) for it in out] == [
        ("西松屋チェーン（7545）決算", _at(d, 15, 30), True), ("IRBANKに無い（9999）決算", None, True)]
    assert [sc._out(it, d)["time_label"] for it in out] == ["15:30", "未定"]
    assert not any(it.get("fallback") for it in out)
    assert all(it["kind"] == "earnings" and it["country"] == "JP" for it in out)


def test_jp_notable_items_empty_stars_does_not_fall_back():
    assert sc.jp_notable_items([], _rows(), date(2026, 10, 1)) == []


def test_jp_notable_items_fallback_top8_by_cap():
    d = date(2026, 10, 1)
    out = sc.jp_notable_items(None, _rows(), d)
    # 時価総額1000億円以上の上位(クスリのアオキ 3523億 > 西松屋 1364億 > 平和堂 1332億)。印は notable + fallback
    assert [it["name"] for it in out] == [
        "クスリのアオキホールディングス（3549）決算", "西松屋チェーン（7545）決算", "平和堂（8276）決算"]
    assert all(it["notable"] and it["fallback"] for it in out)
    many = [{"code": f"{1000 + i}", "name": f"社{i}", "cap": 2000 + i, "hm": None} for i in range(12)]
    assert len(sc.jp_notable_items(None, many, d)) == sc.EARNINGS_MAX


def test_parse_irbank_marks_fallback():
    out = sc.parse_irbank(_fx("irbank_kessan_2026-10-01.html"), date(2026, 10, 1))
    assert out and all(it["notable"] and it["fallback"] for it in out)


# --- 米国の注目決算(Nasdaq 500億ドル以上を全部) ---

def test_parse_nasdaq_notable_all_over_50bn_without_limit():
    rows = [{"name": f"Mega{i} Corp.", "symbol": f"M{i}", "marketCap": f"${(50 + i) * 10**9:,}",
             "time": "time-pre-market"} for i in range(12)]
    rows += [{"name": "Small Inc.", "symbol": "SM", "marketCap": "$49,000,000,000", "time": "time-after-hours"}]
    out = sc.parse_nasdaq(json.dumps({"data": {"rows": rows}}).encode(), date(2026, 10, 1))
    assert len(out) == 12 and out[0]["name"] == "Mega11（M11）決算" and out[-1]["name"] == "Mega0（M0）決算"
    assert all(it["notable"] for it in out)


# --- select: 注目決算は枠の外 ---

def test_select_keeps_all_notable_beyond_max_items():
    d = date(2026, 10, 1)
    others = [sc._item(_at(d, 9, i), "indicator", "US", f"指標{i}", 5) for i in range(sc.MAX_ITEMS + 5)]
    notable = [sc._item(_at(d, 22, 0), "earnings", "US", f"大型{i}（X{i}）決算", 3, notable=True)
               for i in range(sc.MAX_ITEMS)]
    out = sc.select(others + notable, _at(d, 7, 15))
    assert len(out) == sc.MAX_ITEMS * 2
    assert sum(1 for x in out if x.get("notable")) == sc.MAX_ITEMS
    assert [x["at"] for x in out] == sorted(x["at"] for x in out)     # 並びは時刻順
    assert "notable" not in next(x for x in out if x["kind"] == "indicator")


def test_select_outputs_notable_and_fallback_flags():
    d = date(2026, 10, 1)
    out = sc.select(sc.parse_irbank(_fx("irbank_kessan_2026-10-01.html"), d), _at(d, 7, 15))
    assert out and all(x["notable"] is True and x["fallback"] is True for x in out)


# --- 前営業日 ---

def test_prev_weekday():
    assert sc.prev_weekday(date(2026, 10, 2)) == date(2026, 10, 1)    # 金→木
    assert sc.prev_weekday(date(2026, 10, 5)) == date(2026, 10, 2)    # 月→金
    assert sc.prev_weekday(date(2026, 10, 4)) == date(2026, 10, 2)    # 日→金
    assert sc.prev_weekday(date(2026, 10, 3)) == date(2026, 10, 2)    # 土→金


# --- 決算サプライズ(日本: 株探 PTS ランキング + 個別ニュースの決算・修正見出し) ---

def test_parse_pts_both_layouts():
    up = sc.parse_pts(_fx("kabutan_pts_up_cap4.html"))      # 時価総額の列がある表
    assert up[0] == {"code": "9632", "name": "スバル", "market": "東Ｓ", "pct": 4.37}
    assert [q["code"] for q in up] == ["9632", "1357", "4419"]   # 間に挟まるチャート用の行は無視
    down = sc.parse_pts(_fx("kabutan_pts_down.html"))       # 列の少ない表
    assert down[0] == {"code": "9842", "name": "アークランズ", "market": "東Ｐ", "pct": -21.45}
    assert [q["code"] for q in down][3:] == ["2493", "367A"]


def test_parse_kabutan_news_picks_result_headline_in_window():
    body = _fx("kabutan_stock_news_7847.html")
    p, d = date(2026, 10, 1), date(2026, 10, 2)
    # 10/1 15:30 の「修正」。10/2 朝の「材料」(Ｓ高カイ気配)・「注目」・「開示」は対象外
    assert sc.parse_kabutan_news(body, p, d) == "グラファイト、上期経常を3.9倍上方修正、通期も増額"
    assert sc.parse_kabutan_news(body, date(2026, 9, 30), date(2026, 10, 1)) == ""   # 窓の外
    assert sc.parse_kabutan_news(body, date(2026, 10, 2), date(2026, 10, 3)) == ""
    assert sc.parse_kabutan_news(b"<html></html>", p, d) == ""


_NEWS_ROW = ('<tr><td class="news_time"><time datetime="{t}">x</time></td>'
             '<td><div class="newslist_ctg newsctg3_kk_b">{c}</div></td>'
             '<td><a href="/stock/news?code=1&b=k1">{h}</a></td></tr>')


def test_parse_kabutan_news_earliest_and_result_category_first():
    p, d = date(2026, 10, 1), date(2026, 10, 2)
    rows = "".join([
        _NEWS_ROW.format(t="2026-10-01T16:10:00+09:00", c="決算", h="遅い決算"),
        _NEWS_ROW.format(t="2026-10-01T15:00:00+09:00", c="修正", h="同時刻の修正"),
        _NEWS_ROW.format(t="2026-10-01T15:00:00+09:00", c="決算", h="同時刻の決算"),
        _NEWS_ROW.format(t="2026-10-01T14:59:00+09:00", c="決算", h="15時前"),
        _NEWS_ROW.format(t="2026-10-02T09:00:00+09:00", c="決算", h="朝9時以降"),
    ])
    assert sc.parse_kabutan_news(rows.encode(), p, d) == "同時刻の決算"


def _q(code, pct, market="東Ｐ", name="x"):
    return {"code": code, "name": name, "market": market, "pct": pct}


def test_jp_surprises_threshold_etf_reit_dedupe_and_headlines():
    pts = [_q("1001", 5.0, name="大手A"),                 # ちょうど5%は採る
           _q("1002", -14.77, "東Ｇ", "中堅B"),
           _q("1003", 4.99),                              # 5%未満
           _q("1306", 30.0, "東Ｅ"),                      # ETF は対象外
           _q("8951", -20.0, "東Ｒ"),                     # REIT は対象外
           _q("9999", 20.0, name="決算なし"),             # 決算・修正の見出しが無い
           _q("1001", 99.0, name="重複")]                 # 重複は先に出た方
    asked = []
    out = sc.jp_surprises(pts, lambda code: asked.append(code) or {"1001": "上方修正", "1002": "下方修正"}.get(code, ""))
    assert [(x["name"], x["move_pct"], x["move_label"], x["headline"]) for x in out] == [
        ("中堅B（1002）", -14.77, "PTS", "下方修正"), ("大手A（1001）", 5.0, "PTS", "上方修正")]
    assert asked == ["9999", "1002", "1001"]              # |%| 降順。ETF・REIT・5%未満は引かない
    assert set(out[0]) == {"kind", "country", "name", "move_pct", "move_label", "headline", "at",
                           "time_label", "forecast", "previous", "result", "importance"}
    assert (out[0]["kind"], out[0]["country"], out[0]["at"], out[0]["time_label"], out[0]["importance"]) \
        == ("surprise", "JP", None, "", 3)


def test_jp_surprises_candidates_capped_at_20_and_max_10_found():
    pts = [_q(f"{2000 + i}", (i + 5) * (-1 if i % 2 else 1)) for i in range(30)]   # |%| は 5〜34
    asked = []
    out = sc.jp_surprises(pts, lambda code: asked.append(code) or "決算")
    assert len(out) == sc.SURPRISE_MAX
    assert [abs(x["move_pct"]) for x in out] == [34, 33, 32, 31, 30, 29, 28, 27, 26, 25]
    assert len(asked) == sc.SURPRISE_MAX                  # 10件そろったら株探を引くのをやめる
    asked.clear()
    assert sc.jp_surprises(pts, lambda code: asked.append(code) and "") == []
    assert len(asked) == sc.SURPRISE_JP_CANDIDATES        # 見出しが取れなければ候補20件で打ち切り


# --- 決算サプライズ(米国: Nasdaq + Yahoo chart の5分足) ---

def _bar(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp())


def _chart(prev_close, p_close, post_last, *, p=date(2026, 10, 1), prev=date(2026, 9, 30),
           official=None) -> dict:
    """前営業日の通常取引(終値 prev_close)・p の通常取引(終値 p_close)・p の時間外(最終値 post_last)。"""
    bars = [(_bar(prev, 9, 30), prev_close * 0.99), (_bar(prev, 15, 55), prev_close),
            (_bar(p, 4, 0), p_close * 0.5),              # 寄り前は通常取引に数えない
            (_bar(p, 9, 30), p_close * 1.01), (_bar(p, 15, 55), p_close),
            (_bar(p, 16, 5), post_last * 1.01), (_bar(p, 19, 55), post_last),
            (_bar(p, 20, 0), post_last * 3)]             # 20:00 以降は時間外に数えない
    meta = {"exchangeTimezoneName": "America/New_York"}
    if official:
        meta.update(regularMarketTime=_bar(p, 16, 0) + 2, regularMarketPrice=official)
    return {"chart": {"result": [{"meta": meta, "timestamp": [t for t, _ in bars],
                                  "indicators": {"quote": [{"close": [c for _, c in bars]}]}}]}}


def test_us_reaction_by_time_flag():
    p = date(2026, 10, 1)
    data = _chart(100.0, 110.0, 95.0)      # 当日 +10%、時間外は終値比 -13.6%
    day, post = (110 / 100 - 1) * 100, (95 / 110 - 1) * 100
    assert sc.us_reaction(data, p, "time-pre-market") == pytest.approx((day, "当日"))
    assert sc.us_reaction(data, p, "time-after-hours") == pytest.approx((post, "時間外"))
    # 不明なら両方計算して絶対値の大きい方
    assert sc.us_reaction(data, p, "time-not-supplied") == pytest.approx((post, "時間外"))
    assert sc.us_reaction(_chart(100.0, 112.0, 113.0), p, "") == pytest.approx((12.0, "当日"))


def test_us_reaction_uses_official_close_and_handles_missing():
    p = date(2026, 10, 1)
    # 引け後に更新された確定終値(regularMarketPrice)があれば p の終値はそれ
    data = _chart(100.0, 110.0, 99.0, official=108.0)
    assert sc.us_reaction(data, p, "time-pre-market")[0] == pytest.approx(8.0)
    assert sc.us_reaction(data, p, "time-after-hours")[0] == pytest.approx((99 / 108 - 1) * 100)
    # 別の日の足しか無い・壊れたデータ → None
    assert sc.us_reaction(data, date(2026, 10, 2), "") is None
    assert sc.us_reaction({"chart": {"result": None}}, p, "") is None
    assert sc.us_reaction({}, p, "") is None
    # 実データ(Nike 10/1 引け後、1日分だけ): 当日は前営業日が無く計算できないので時間外だけ
    nke = json.loads(_fx("yahoo_chart_NKE_2026-10-01.json"))
    pct, label = sc.us_reaction(nke, p, "time-after-hours")
    assert label == "時間外" and pct == pytest.approx(-8.71, abs=0.01)
    assert sc.us_reaction(nke, p, "time-pre-market") is None


def test_us_surprises_threshold_sort_and_limit():
    rows = [{"cap": 10**11, "name": f"社{i}", "sym": f"S{i}", "time": ""} for i in range(14)]
    moves = {f"S{i}": ((i + 4.0) * (-1 if i % 2 else 1), "時間外" if i % 2 else "当日") for i in range(14)}
    moves["S0"] = (4.99, "当日")     # 5%未満
    moves["S3"] = None               # Yahoo が取れなかった銘柄は飛ばす
    out = sc.us_surprises(rows, lambda r: moves[r["sym"]])
    assert len(out) == sc.SURPRISE_MAX
    assert out[0]["name"] == "社13（S13）" and out[0]["move_pct"] == -17.0 and out[0]["move_label"] == "時間外"
    assert all(x["kind"] == "surprise" and x["country"] == "US" and x["headline"] == "" for x in out)
    assert "社3（S3）" not in [x["name"] for x in out] and "社0（S0）" not in [x["name"] for x in out]
    assert [abs(x["move_pct"]) for x in out] == sorted((abs(x["move_pct"]) for x in out), reverse=True)


# --- fetch まるごと(取得はすべて偽物) ---

def _pts_html(rows: list[tuple]) -> bytes:
    tr = "".join(
        f'<tr><td class="tac"><a href="/stock/?code={c}">{c}</a></td><th scope="row" class="tal">{n}</th>'
        f'<td class="tac">{m}</td>'
        f'<td class="w50"><span class="{"up" if v > 0 else "down"}">{v:+.2f}</span>%</td></tr>'
        for c, n, m, v in rows)
    return f"<table><tbody>{tr}</tbody></table>".encode()


def test_fetch_returns_surprises_and_notables(monkeypatch):
    seen: list[str] = []

    def get(url):
        seen.append(url)
        if "minkabu" in url:
            return _fx("minkabu_2026-09-30.html")
        if "federalreserve" in url:
            return _fx("frb_calendar.json")
        if "boj.or.jp" in url:
            return _fx("boj_mpmsche.html")
        if "nasdaq.com" in url:
            return _fx("nasdaq_earnings_2026-10-01.json")
        if "irbank.net" in url:
            return _fx("irbank_kessan_2026-10-01.html")
        if "pts_night_price_increase" in url and "capitalization=5" in url:
            return _pts_html([("7545", "西松屋チェーン", "東Ｐ", 12.34), ("3549", "クスリのアオキ", "東Ｐ", 2.0),
                              ("8951", "日本ビルファンド", "東Ｒ", 9.0)])
        if "pts_night_price_decrease" in url and "capitalization=4" in url:
            return _pts_html([("7447", "ナガイレーベン", "東Ｐ", -6.5), ("1306", "ＴＯＰＩＸ連動型", "東Ｅ", -8.0)])
        if "pts_night_price" in url:
            return _pts_html([])
        if "stock/news" in url:
            return _fx("kabutan_stock_news_7847.html")
        if url.rstrip("/") == "https://kabutan.jp":
            return _fx("kabutan_top_links.html")
        if "marketnews" in url:
            return _fx("kabutan_weekly_2026-09-28.html")
        raise AssertionError(url)

    monkeypatch.setattr(sc, "_get", get)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: json.loads(_fx("yahoo_chart_NKE_2026-10-01.json")))
    got = sc.fetch(datetime(2026, 10, 2, 7, 15))     # 金曜。前営業日は 10/1
    assert set(got) == {"schedule", "results", "surprises"}
    assert [(x["name"], x["move_pct"], x["move_label"]) for x in got["surprises"] if x["country"] == "JP"] == [
        ("西松屋チェーン（7545）", 12.34, "PTS"), ("ナガイレーベン（7447）", -6.5, "PTS")]
    assert all(x["headline"] == "グラファイト、上期経常を3.9倍上方修正、通期も増額"
               for x in got["surprises"] if x["country"] == "JP")
    # Yahoo は全銘柄に Nike の 10/1 の足を返す。寄り前の Accenture は前営業日の足が無く飛ばされ、
    # 引け後の Nike だけが時間外の下落(-8.7%)で残る
    assert [(x["name"], x["move_label"]) for x in got["surprises"] if x["country"] == "US"] == [
        ("Nike（NKE）", "時間外")]
    # 株探は直列(PTS 4ページ + 個別ニュース2件)で、前営業日(10/1)の一覧を引く
    assert sum("pts_night_price" in u for u in seen) == 4 and sum("stock/news" in u for u in seen) == 2
    assert any("nasdaq.com" in u and "date=2026-10-01" in u for u in seen)
    assert not any("irbank.net" in u and "y=2026-10-01" in u for u in seen)   # 日本のサプライズに IRBANK は使わない


def test_fetch_kabutan_failure_falls_back_to_irbank_top(monkeypatch, capsys):
    def get(url):
        if "kabutan.jp" in url:
            return None
        if "irbank.net" in url:
            return _fx("irbank_kessan_2026-10-01.html")
        return None

    monkeypatch.setattr(sc, "_get", get)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: None)
    got = sc.fetch(datetime(2026, 10, 1, 7, 15, tzinfo=J))
    names = [(x["name"], x.get("fallback")) for x in got["schedule"]]
    assert names == [("平和堂（8276）決算", True), ("クスリのアオキホールディングス（3549）決算", True),
                     ("西松屋チェーン（7545）決算", True)]   # 発表目安(13:30・15:00・15:30)の時刻順
    assert got["surprises"] == []
    assert "株探" in capsys.readouterr().err


def test_fetch_star_free_day_has_no_japan_earnings(monkeypatch):
    """株探の記事に d の見出しがあって★が0件の日は、IRBANK で代用せず日本の注目決算は0件。"""
    def get(url):
        if url.rstrip("/") == "https://kabutan.jp":
            return _fx("kabutan_top_links.html")
        if "marketnews" in url:
            return _fx("kabutan_weekly_2026-09-28.html")
        if "irbank.net" in url:
            return _fx("irbank_kessan_2026-10-01.html")
        return None

    monkeypatch.setattr(sc, "_get", get)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: None)
    got = sc.fetch(datetime(2026, 10, 1, 7, 15, tzinfo=J))
    assert [x for x in got["schedule"] if x["country"] == "JP"] == []


def test_kabutan_get_skips_after_deadline(monkeypatch):
    """締め切りを過ぎたら株探を読まない(遅い応答の積み上がりで collect の600秒打ち切りに達しないように)。"""
    def boom(url):
        raise AssertionError("締め切り後に読んだ")
    monkeypatch.setattr(sc, "_get", boom)
    monkeypatch.setattr(sc, "_kabutan_deadline", [0.0])
    assert sc._kabutan_get("https://kabutan.jp/") is None
