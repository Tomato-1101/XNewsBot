"""決算予定(日本: IRBANK・株探の★・売買代金、米国: Nasdaq 500億ドル以上)・当日の説明会と決算サプライズ(前営業日の決算への市場の反応)の単体テスト。

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


# --- 日本の決算予定(IRBANK の全社 → 場中/引け後で条件を変えて絞る) ---

def _rows():
    return sc._irbank_rows(_fx("irbank_kessan_2026-10-01.html"), date(2026, 10, 1))


def test_irbank_rows_read_past_times():
    by = {r["code"]: r for r in _rows()}
    assert by["7545"]["hm"] == (15, 30) and by["7545"]["past"] == [(14, 30)]
    assert by["8276"]["past"] == []
    # 見出しの日付が違う一覧(休日に翌営業日が出る等)は使わない
    assert sc._irbank_rows(_fx("irbank_kessan_2026-10-01.html"), date(2026, 10, 2)) == []


def test_jp_session_by_forecast_then_past_time():
    assert sc.jp_session({"hm": (13, 30), "past": []}) == ("intraday", (13, 30), "")
    assert sc.jp_session({"hm": (15, 30), "past": [(16, 0), (14, 30)]}) == ("intraday", (14, 30), "前回14:30")
    assert sc.jp_session({"hm": (15, 30), "past": [(15, 30)]}) == ("after", (15, 30), "")   # 大引けちょうどは引け後
    assert sc.jp_session({"hm": None, "past": []}) == ("unknown", None, "")


def test_jp_notable_items_intraday_wide_after_narrow():
    d = date(2026, 10, 1)
    out = sc.jp_notable_items([], _rows(), d, {"2493": 150})
    got = {it["name"]: (it["session"], it["label"], it["at"]) for it in out}
    # 場中(15:30 前): 時価総額500億円以上(アオキ 3523・西松屋 1364・平和堂 1332・ナガイレーベン 501)は入り、
    # 日本フイルコン(152億・順位なし)は入らない。西松屋は目安15:30だが前回14:30なので場中
    assert got == {
        "クスリのアオキホールディングス（3549）決算": ("intraday", "", _at(d, 15)),
        "西松屋チェーン（7545）決算": ("intraday", "前回14:30", _at(d, 14, 30)),
        "平和堂（8276）決算": ("intraday", "", _at(d, 13, 30)),
        "ナガイレーベン（7447）決算": ("intraday", "", _at(d, 15)),
    }
    # 引け後は売買代金上位100位まで(イーサポートリンク 42億・150位は入らない、100位なら入る)
    out = sc.jp_notable_items([], _rows(), d, {"2493": 100, "5942": 300})
    names = [it["name"] for it in out]
    assert "イーサポートリンク（2493）決算" in names and "日本フイルコン（5942）決算" in names   # 場中は300位まで
    assert all(it["kind"] == "earnings" and it["country"] == "JP" and it["notable"] for it in out)


def test_jp_notable_items_stars_always_and_order():
    d = date(2026, 10, 1)
    weekly = [{"code": "2493", "name": "イーサポ"}, {"code": "9999", "name": "IRBANKに無い"}]
    out = sc.jp_notable_items(weekly, _rows(), d)
    by = {it["name"]: it for it in out}
    # ★は条件に関係なく出す(会社名は IRBANK の正式名)。IRBANK に無い★は株探の名前・時刻未定
    assert by["イーサポートリンク（2493）決算"]["star"] and by["イーサポートリンク（2493）決算"]["session"] == "after"
    assert by["IRBANKに無い（9999）決算"]["at"] is None and by["IRBANKに無い（9999）決算"]["session"] == "unknown"
    assert sc._out(by["IRBANKに無い（9999）決算"], d)["time_label"] == "未定"
    # 並びは★ → 時価総額の大きい順(時刻順への並べ替えは select が安定ソートで行う)
    assert [it["name"] for it in out][:3] == ["イーサポートリンク（2493）決算", "IRBANKに無い（9999）決算",
                                              "クスリのアオキホールディングス（3549）決算"]
    assert not sc._out(by["平和堂（8276）決算"], d).get("star")
    assert sc._out(by["平和堂（8276）決算"], d)["session"] == "intraday"


def test_parse_yahoo_trading_value():
    out = sc.parse_yahoo_tv(_fx("yahoo_trading_value_2026-11-11.html"))
    assert out == [{"rank": 1, "code": "285A", "name": "キオクシアホールディングス(株)"},
                   {"rank": 2, "code": "6857", "name": "(株)アドバンテスト"},
                   {"rank": 3, "code": "9984", "name": "ソフトバンクグループ(株)"}]
    assert sc.parse_yahoo_tv(b"<html></html>") == []


def test_yahoo_tv_reads_pages_until_empty(monkeypatch):
    page = _fx("yahoo_trading_value_2026-11-11.html")
    seen = []
    monkeypatch.setattr(sc, "YAHOO_TV_GAP", 0)
    monkeypatch.setattr(sc, "_get", lambda url: seen.append(url) or (page if "page=1" in url or "page=2" in url
                                                                      else None))
    assert len(sc._yahoo_tv()) == 6 and len(seen) == 3        # 3ページ目が空なら止める
    monkeypatch.setattr(sc, "_get", lambda url: None)
    with pytest.raises(sc._FetchError):
        sc._yahoo_tv()


_TV = [{"rank": 44, "code": "3905", "name": "データセクション(株)"},
       {"rank": 5, "code": "9412", "name": "スカパーJSATホールディングス(株)"},
       {"rank": 3, "code": "9984", "name": "ソフトバンクグループ(株)"}]


def test_ir_events_today_only_for_ranked_names():
    titles = [
        "データセクション－7日ぶり大幅反発 10日にAIデータセンターの稼働状況などに関する説明会開催へ(トレーダーズ・ウェブ)",
        "2026年11月10日（火）【野村證券主催】「スカパーＪＳＡＴ株式会社 個人投資家様向け会社説明会」開催",
        "ソフトバンクグループ株式会社が決算説明会を開催。11月10日16時30分よりライブ配信",   # 決算説明会は除く
        "ＪＴ、個人投資家向け説明会を見合わせ 10日に予定",                                   # 見合わせは除く
        "アクシス[4012]：個人投資家向け会社説明会開催のお知らせ 2026年11月10日(適時開示)",      # 開示日・圏外
        "多賀城跡の現地説明会を11月10日に開催",                                              # 銘柄ではない
    ]
    d = date(2026, 11, 10)
    out = sc.ir_events(titles, d, _TV)
    assert [(it["name"], it["kind"], it["at"], it["session"]) for it in out] == [
        ("データセクション（3905）説明会", "ir", None, "unknown"),
        ("スカパーJSATホールディングス（9412）説明会", "ir", None, "unknown")]
    assert sc.ir_events(titles, date(2026, 11, 7), _TV) == []      # 「7日ぶり」は日付ではない
    timed = sc.ir_events(["データセクション、11/10 14時から説明会を開催"], d, _TV)
    assert timed[0]["at"] == _at(d, 14) and timed[0]["session"] == "intraday"
    assert sc._out(timed[0], d)["time_label"] == "14:00" and sc._out(timed[0], d)["notable"]


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


def test_select_outputs_notable_session_and_star():
    d = date(2026, 10, 1)
    out = sc.select(sc.jp_notable_items([{"code": "8276", "name": "平和堂"}], _rows(), d), _at(d, 7, 15))
    assert out and all(x["notable"] is True and x["session"] == "intraday" for x in out)
    assert [x["name"] for x in out if x.get("star")] == ["平和堂（8276）決算"]
    assert [x["time_label"] for x in out] == ["13:30", "前回14:30", "15:00", "15:00"]   # 時刻順


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


def test_fetch_kabutan_and_yahoo_failure_still_lists_irbank(monkeypatch, capsys):
    """株探・Yahoo・Google ニュースが取れなくても、IRBANK の時価総額の条件だけで日本の決算予定は出る。"""
    def get(url):
        if "irbank.net" in url:
            return _fx("irbank_kessan_2026-10-01.html")
        return None

    monkeypatch.setattr(sc, "_get", get)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: None)
    got = sc.fetch(datetime(2026, 10, 1, 7, 15, tzinfo=J))
    assert [x["name"] for x in got["schedule"]] == [
        "平和堂（8276）決算", "西松屋チェーン（7545）決算", "クスリのアオキホールディングス（3549）決算",
        "ナガイレーベン（7447）決算"]   # 13:30・前回14:30・15:00・15:00(同時刻は時価総額順)
    assert got["surprises"] == []
    err = capsys.readouterr().err
    assert "株探" in err and "Yahoo(売買代金)" in err


def test_fetch_star_free_day_uses_conditions_only(monkeypatch):
    """株探の記事に d の見出しがあって★が0件の日も、条件(時価総額・売買代金)に当たる決算は出す。"""
    def get(url):
        if url.rstrip("/") == "https://kabutan.jp":
            return _fx("kabutan_top_links.html")
        if "marketnews" in url:
            return _fx("kabutan_weekly_2026-09-28.html")
        if "irbank.net" in url:
            return _fx("irbank_kessan_2026-10-01.html")
        if "tradingValueHigh" in url and "page=1" in url:
            return _fx("yahoo_trading_value_2026-11-11.html")
        return None

    monkeypatch.setattr(sc, "_get", get)
    monkeypatch.setattr(sc, "_yahoo_chart", lambda sym: None)
    got = sc.fetch(datetime(2026, 10, 1, 7, 15, tzinfo=J))
    jp = [x for x in got["schedule"] if x["country"] == "JP"]
    assert len(jp) == 4 and not any(x.get("star") for x in jp)


def test_kabutan_get_skips_after_deadline(monkeypatch):
    """締め切りを過ぎたら株探を読まない(遅い応答の積み上がりで collect の600秒打ち切りに達しないように)。"""
    def boom(url):
        raise AssertionError("締め切り後に読んだ")
    monkeypatch.setattr(sc, "_get", boom)
    monkeypatch.setattr(sc, "_kabutan_deadline", [0.0])
    assert sc._kabutan_get("https://kabutan.jp/") is None
