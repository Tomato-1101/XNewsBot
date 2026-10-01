#!/usr/bin/env python3
"""XNewsBot パイプライン CLI。Claude Code の定期実行から呼ぶ。

1日2回(朝=morning / 夜=evening スロット)の流れ。各スロットで最新を集め直す:
  1) collect : Xから収集して raw JSON を書き出す(--slot で朝/夜を指定)
       python scripts/pipeline.py collect --due --slot morning --out /tmp/xnews_raw.json
  2) (Claude Code がキュレーション) raw JSON を読み、各ジャンルを
     [{"title","summary","importance","score","source_idxs"}] にして curated JSON を書く
  3) ingest  : キュレーション結果を DB に取り込む(slot は raw から自動・--slot で上書き可)
       python scripts/pipeline.py ingest --raw /tmp/xnews_raw.json --curated /tmp/xnews_curated.json
  4) push    : LINE へ送信。定刻配信(全購読者)は --due、今すぐ配信(個人)は --user。
       python scripts/pipeline.py push --due  --slot morning   # 定刻(配信済みにする)
       python scripts/pipeline.py push --user Uxxxx            # 今すぐ(配信済みにしない)

これら1〜4を配信時刻ちょうどに通しで実行するのが ops/deliver.sh(launchd / 今すぐ配信)。
収集と送信はこのプログラム、記事の選別・見出し・要約の生成はヘッドレス Claude Code が担う。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from xnewsbot import digest, newsfeeds, xclient  # noqa: E402
from xnewsbot import articles, market, schedule, trends, watch  # noqa: E402
from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot.config import get_settings  # noqa: E402
from xnewsbot.curator import parse_curated  # noqa: E402
from xnewsbot.db import get_session, init_db  # noqa: E402
from xnewsbot.genres import ALWAYS_KEYS, GENRE_KEYS, is_valid_genre, is_watch, keywords  # noqa: E402
from xnewsbot.genres import feeds, keywords_en, news_max, trend_sources, x_queries  # noqa: E402
from xnewsbot.models import SLOTS, Subscriber  # noqa: E402
from xnewsbot.models import GenreDigest, NewsItem  # noqa: E402
from xnewsbot.scheduler import deliver_to_subscriber, slot_for_now  # noqa: E402


# collect の「対象ジャンルなし(購読者未登録)」を本物の失敗と区別するための exit code。
# ops/deliver.sh がこの値のときだけ正常スキップ扱いにする(契約。変えたら deliver.sh も合わせる)。
EXIT_NO_TARGET = 64
# 対象ジャンルはあったが全ジャンルが収集0件(API全滅)。空ダイジェストを「成功」配信しないため、
# deliver.sh はこの値を失敗(配信中止)として扱う(契約。変えたら deliver.sh も合わせる)。
EXIT_ALL_FAILED = 65


def _today(settings) -> date:
    return datetime.now(ZoneInfo(settings.default_tz)).date()


X_TEXT_MAX = 1000


def _trim(t: dict) -> dict:
    """キュレーション + 出典マッピングに必要な項目だけに絞る(raw JSON を小さく読みやすく)。"""
    a = t.get("author") or {}
    user = a.get("userName", "?")
    return {
        # 長文ポストは打ち切る(raw は1行1候補で、Read ツールは2000字を超える行を切り捨てるため)
        "text": " ".join((t.get("text") or "").split())[:X_TEXT_MAX],
        "viewCount": t.get("viewCount") or 0,
        "likeCount": t.get("likeCount") or 0,
        "url": t.get("url", ""),
        "createdAt": t.get("createdAt", ""),
        "author": {"userName": user, "followers": a.get("followers", 0)},
        "media": f"@{user}",
        "kind": "x",
        # 公式・一次情報アカウントの投稿(xclient の from: クエリ)。キュレーションの信頼度判定に使う。
        "official": bool(t.get("_official")),
    }


# X 候補の上限。公式(accounts 由来)は全部残し、それ以外を viewCount 降順で合計この数まで。
X_PER_GENRE = 50
# x_queries の from: に並べた監視アカウントは公式扱いだが無制限にはしない(AI は約30アカウントで、
# 全部残すと日本語・英語キーワードの一般投稿が枠から消えるため)。viewCount の高い順にこの数まで。
X_WATCHED_MAX = 15
# 一般投稿(キーワード検索)に最低限残す数。公式・監視で X_PER_GENRE を使い切っても、これだけは確保する。
X_MIN_GENERAL = 30
# ニュース候補の上限(Google ニュース日本語+英語+直取り RSS を重複除去した後)。
# genres.toml の news_max があればそちらを使う(媒体の多い AI・話題は 60)。
NEWS_PER_GENRE = 40
GN_JA_MAX = 10      # Google ニュース日本語の上限(以前は ja を先に詰めて英語が押し出されていた)
GN_EN_MAX = 8       # Google ニュース英語の上限(日本語とは別枠)
FEED_MAX = 8        # 直取り RSS 1本あたりの上限(1媒体で枠を埋めない)
GN_QUERY_TERMS = 6  # Google ニュース検索に使う先頭キーワード数(長すぎるクエリはヒットが痩せる)
# Yahoo!ファイナンスの銘柄ページ(「トヨタ(株)【7203】：株価・株式情報」)はニュースではない。
# 株ジャンルの Google ニュース枠の大半(15件中10件)を占めていたので見出しで落とす。
# 指数ページ(「日経平均株価の指数情報・推移」)も同じく中身が無い。
QUOTE_PAGE_MARKS = ("株価・株式情報", "指数情報・推移")
# 再掲を避けるためキュレーションに渡す「直近の配信見出し」の範囲(raw の date の前日〜N日前)。
RECENT_DAYS = 3
# 新モデルの登録一覧(trends.PINNED)をラウンドロビンの外で先に入れる件数の上限。
# 上限が無いと、新モデルが多い日に RSS・Google ニュースが 1 本あたり約 1 件まで細る。
PINNED_MAX = 30


def _mark_watched(genre: str, tweets: list[dict]) -> list[dict]:
    """x_queries の from: に並べた当事者アカウント(AI の新興ラボ・音声系など)の投稿に _official と _watched を付ける。
    公式扱いにしないと _cap_x で再生数の多い一般投稿に押し出され、マイナーなモデル公開を取りこぼすため。
    accounts 由来で既に _official の投稿は、監視枠(上限あり)に落とさずそのまま公式として扱う。
    `-from:spam` のような除外指定は監視対象ではない(先頭が - や英数字の from: は拾わない)。"""
    watched = {u.lower() for q in x_queries(genre) for u in re.findall(r"(?<![-\w])from:(\w+)", q["query"])}
    if watched:
        for t in tweets:
            if not t.get("_official") and ((t.get("author") or {}).get("userName") or "").lower() in watched:
                t["_official"] = True
                t["_watched"] = True
    return tweets


def _cap_x(tweets: list[dict], limit: int = X_PER_GENRE, min_general: int = X_MIN_GENERAL) -> list[dict]:
    """公式は全部・監視アカウントは viewCount 降順で X_WATCHED_MAX 件まで・一般投稿は残り枠(最低 min_general 件)。
    出力順は 公式→監視→一般。"""
    off = [t for t in tweets if t.get("_official") and not t.get("_watched")]
    watched = sorted((t for t in tweets if t.get("_watched")), key=xclient.views, reverse=True)[:X_WATCHED_MAX]
    rest = sorted((t for t in tweets if not t.get("_official")), key=xclient.views, reverse=True)
    return off + watched + rest[: max(min_general, limit - len(off) - len(watched))]


def _gn_query(terms: list[str]) -> str:
    """Google ニュース検索の OR クエリ。空白を含む語はフレーズにする。"""
    ts = [f'"{t}"' if " " in t else t for t in terms[:GN_QUERY_TERMS]]
    return "(" + " OR ".join(ts) + ")" if len(ts) > 1 else ts[0]


def _norm_title(s: str) -> str:
    """重複判定用の正規化見出し(全角半角・大小文字・記号・空白の差を無視)。"""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s or "").lower())


def _not_quote_page(items: list) -> list:
    return [it for it in items if not any(m in it.title for m in QUOTE_PAGE_MARKS)]


def _mentions(item, terms: list[str]) -> bool:
    """見出し・概要がジャンルのキーワードのどれかを含むか(総合媒体の feed を絞る filter=true 用)。"""
    hay = f"{item.title} {item.summary}".lower()
    return any(t.lower() in hay for t in terms)


def _feed_items(feed: dict, terms: list[str], hours) -> list:
    items = newsfeeds.fetch_feed(feed["url"], feed["name"], within_hours=hours)
    if feed.get("filter"):
        items = [it for it in items if _mentions(it, terms)]
    items = _not_quote_page(items)
    items.sort(key=lambda it: it.published.timestamp() if it.published else 0, reverse=True)
    return items[:FEED_MAX]


def merge_news(sources: list[list], limit: int = NEWS_PER_GENRE, pinned: int = 0) -> list[dict]:
    """ソースごとの記事列を1件ずつ順番に取り出し(ラウンドロビン)、URL と正規化見出しで重複を除いて
    limit 件までの候補にする。1つのソースが枠を埋めて他の媒体・英語が押し出されないように。
    先頭 pinned 本のソースは先に入れる(新モデルの登録一覧など、深い順位も落としたくないもの)。
    pinned 全体で PINNED_MAX 件まで(1つの一覧が枠を使い切らないよう、pinned 同士もラウンドロビン)。
    要素は FeedItem か、候補の形の dict(trends の候補。trend キー付き)のどちらでもよい。"""
    seen_url: set[str] = set()
    seen_title: set[str] = set()
    out: list[dict] = []

    def add(it) -> None:
        c = it if isinstance(it, dict) else newsfeeds.as_candidate(it)
        nt = _norm_title(c["text"])
        if c["url"] in seen_url or (nt and nt in seen_title):
            return
        seen_url.add(c["url"])
        if nt:
            seen_title.add(nt)
        out.append(c)

    pin_srcs = sources[:pinned]
    for i in range(max((len(s) for s in pin_srcs), default=0)):
        for src in pin_srcs:
            if i < len(src) and len(out) < min(limit, PINNED_MAX):
                add(src[i])
    rest = sources[pinned:]
    depth = max((len(s) for s in rest), default=0)
    for i in range(depth):
        for src in rest:
            if i < len(src) and len(out) < limit:
                add(src[i])
    return out


def _safe_source(fn) -> list:
    """無料ソース1つの想定外の失敗(壊れた XML 等)で収集全体を落とさない。"""
    try:
        return fn()
    except Exception as e:
        print(f"  ニュース取得の一部が失敗(スキップ): {type(e).__name__}: {e}", file=sys.stderr)
        return []


def _newsfeed_candidates(genre: str, settings) -> list[dict]:
    """無料ニュースをジャンルの候補に足す: Google ニュース 日本語(keywords)・英語(keywords_en)
    ＋ 直取り RSS(genres.toml の feeds) ＋ 話題の取得元(trend_sources。xnewsbot/trends.py)。
    失敗したソースは空(=残りで続ける・無害)。
    本文(body)はここでは取らず、cmd_collect が全ジャンル分まとめて articles.enrich_bodies で埋める。
    """
    if not settings.collect_use_newsfeeds:
        return []
    hours = settings.collect_hours
    kws, kws_en = keywords(genre), keywords_en(genre)
    jobs = []
    # 話題の候補を先に並べる(同じ記事が RSS にもあれば、話題の大きさ(trend)付きの方を残す)
    names = []
    for name in trend_sources(genre):
        if name in trends.SOURCES:
            names.append(name)
        else:
            print(f"  {genre}: 未知の trend_sources をスキップ: {name}", file=sys.stderr)
    # 新モデルの登録一覧は件数が少なく取りこぼしたくないので、ラウンドロビンの外で全件先に入れる
    names.sort(key=lambda n: n not in trends.PINNED)
    pinned = sum(1 for n in names if n in trends.PINNED)
    for name in names:
        jobs.append(lambda fn=trends.SOURCES[name]: fn(hours))
    if kws:
        jobs.append(lambda: _not_quote_page(
            newsfeeds.google_news(_gn_query(kws), within_hours=hours))[:GN_JA_MAX])
    if kws_en:
        jobs.append(lambda: _not_quote_page(
            newsfeeds.google_news(_gn_query(kws_en), lang="en", region="US", hl="en-US",
                                  within_hours=hours))[:GN_EN_MAX])
    for f in feeds(genre):
        jobs.append(lambda f=f: _feed_items(f, kws + kws_en, hours))
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        sources = list(pool.map(_safe_source, jobs))
    return merge_news(sources, limit=news_max(genre) or NEWS_PER_GENRE, pinned=pinned)


def _recent_titles(genres: list[str], day: date) -> dict[str, list[str]]:
    """直近に配信した見出し(day の前日〜RECENT_DAYS 日前・ジャンル別)。DB は読むだけ。

    日をまたいだ再掲(14日で約8%、同じ話題が6日連続も)を避けるため、キュレーションに渡して
    「新しい進展がある続報だけ」にさせる。読めなくても収集は続ける(再掲判定なしになるだけ)。
    """
    out: dict[str, list[str]] = {g: [] for g in genres}
    try:
        init_db()
        with get_session() as session:
            rows = session.exec(
                select(GenreDigest.genre, NewsItem.title)
                .join(NewsItem, NewsItem.genre_digest_id == GenreDigest.id)
                .where(GenreDigest.digest_date >= day - timedelta(days=RECENT_DAYS))
                .where(GenreDigest.digest_date <= day - timedelta(days=1))
                .where(GenreDigest.genre.in_(genres))
                .order_by(GenreDigest.digest_date.desc(), NewsItem.rank)
            ).all()
    except Exception as e:
        print(f"  直近の見出しを読めませんでした(再掲判定なしで続行): {type(e).__name__}: {e}",
              file=sys.stderr)
        return out
    for g, title in rows:
        if title and title not in out[g]:
            out[g].append(title)
    return out


def _total_balance(keys: list[str]) -> int | None:
    """全鍵の残クレジットのうち「残高が正の鍵」の合計。1つでも取得に失敗したら None。

    残高マイナスの鍵(常に 402 で使われない)は合計に入れない。失敗を飛ばして足すと
    収集の前後で足す鍵の集合が変わり、差(使用量)が狂うので None にして x_usage ごと出さない。"""
    total = 0
    for k in keys:
        b = xclient.fetch_balance(k)
        if b is None:
            return None
        if b > 0:
            total += b
    return total


def compute_x_usage(before: int | None, after: int | None) -> dict | None:
    """収集前後の残高から {"used","remaining"}。どちらか取れていなければ None。"""
    if before is None or after is None:
        return None
    return {"used": max(before - after, 0), "remaining": after}


def _dump_raw(out: dict) -> str:
    """raw を「1行1候補」の JSON で書く。各候補の先頭に、そのジャンル内の番号 `i` を付ける。

    indent=2 だと1候補が十数行になり、キュレーションが 200 行ずつ 25 回読んで約5分かかった
    (2026-10-01 試走)。1行1候補ならインデント分のトークンが消え、読む回数も減る。
    `i` は source_idxs に書く番号そのもの(数え間違いを防ぐ)。取り込みは配列の位置で引くので値は一致させる。
    """
    def d(v) -> str:
        return json.dumps(v, ensure_ascii=False)

    def block(items: list, last: bool) -> list[str]:
        return [d(x) + ("," if j < len(items) - 1 else "") for j, x in enumerate(items)] + \
            ["]" + ("" if last else ",")]

    lines = ["{"] + [f"{d(k)}: {d(out[k])}," for k in ("date", "tz", "slot")]
    lines.append(f'"x_usage": {d(out.get("x_usage"))},')
    lines.append(f'"watch": {d(out.get("watch"))},')
    lines += ['"market": ['] + block(out["market"], last=False)
    lines += ['"schedule": ['] + block(out["schedule"], last=False)
    lines += ['"indicator_results": ['] + block(out["indicator_results"], last=False)
    lines.append('"recent_titles": {')
    rt = list(out["recent_titles"].items())
    for n, (g, titles) in enumerate(rt):
        lines += [f"{d(g)}: ["] + block(titles, last=n == len(rt) - 1)
    lines.append("},")
    lines.append('"genres": {')
    gs = list(out["genres"].items())
    for n, (g, cands) in enumerate(gs):
        lines += [f"{d(g)}: ["] + block([{"i": j, **c} for j, c in enumerate(cands)],
                                         last=n == len(gs) - 1)
    lines.append("}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _due_genres(settings) -> list[str]:
    """オンボーディング済み購読者の有効ジャンルの和集合(表示順)。"""
    init_db()
    seen: set[str] = set()
    with get_session() as session:
        subs = session.exec(select(Subscriber).where(Subscriber.is_onboarded == True)).all()  # noqa: E712
        for s in subs:
            seen.update(s.enabled_genres)
    return [g for g in GENRE_KEYS if g in seen]


def _user_genres(settings, line_user_id: str) -> list[str]:
    """指定ユーザーの有効ジャンル(表示順)。"""
    init_db()
    with get_session() as session:
        sub = session.exec(
            select(Subscriber).where(Subscriber.line_user_id == line_user_id)
        ).first()
        seen = set(sub.enabled_genres) if sub else set()
    return [g for g in GENRE_KEYS if g in seen]


def _with_always(genres: list[str]) -> list[str]:
    """常時ジャンル(特大など)を必ず含めた表示順のリストにする。"""
    chosen = set(genres) | set(ALWAYS_KEYS)
    return [g for g in GENRE_KEYS if g in chosen]


def cmd_collect(args) -> None:
    settings = get_settings()
    if args.user:
        genres = _with_always(_user_genres(settings, args.user))
    elif args.due:
        genres = _with_always(_due_genres(settings))
    else:
        genres = [g.strip() for g in args.genres.split(",") if g.strip()]
    bad = [g for g in genres if not is_valid_genre(g)]
    if bad:
        sys.exit(f"未知のジャンル: {bad}  有効: {GENRE_KEYS}")
    if not genres:
        print("対象ジャンルがありません(--due/--user なら購読者が未登録の可能性)。", file=sys.stderr)
        sys.exit(EXIT_NO_TARGET)

    day = _today(settings)
    cands_by_genre: dict[str, list[dict]] = {g: [] for g in genres}

    # 鍵は不変。ジャンルごとに load_keys()→Keychain サブプロセスを叩くのは無駄かつ並列で多重に
    # security を起動するので、ここで1度だけ取得して各収集に渡す(優先度順・フォールバック用)。
    keys = xclient.load_keys(settings)
    balance_before = _total_balance(keys)  # X クレジット消費の算出用(残高 API は課金されない)

    # 監視アカウントのジャンル(watch=true)は、前回の取り込みから今回までの全投稿を集める(xnewsbot/watch.py)
    watch_since = watch_until = None
    watch_handles: list[str] = []
    watch_ok: dict[str, bool] = {}
    if any(is_watch(g) for g in genres):
        watch_since, watch_until = watch.window(day, datetime.now(timezone.utc), watch.load_state())
        init_db()
        with get_session() as session:
            watch_handles = watch.enabled_handles(session)

    # ジャンル収集は I/O 待ち(twitterapi.io)。直列だと数分かかるので並列化するが、同一APIキーへ
    # 多並列(以前は6)だと混雑→同時多発タイムアウトを招くため 3 に抑える(xclient 側で再試行もする)。
    def _one(g: str) -> tuple[str, list[dict], int, int, list[dict]]:
        # 1ジャンルの失敗(再試行しても回復しないタイムアウト/恒久エラー)で収集全体を落とさない。
        # 取れたジャンルだけで配信を続ける(空になったジャンルはキュレーションで空配列扱い)。
        # X(有料)と 無料ニュース(RSS)の両方を集めて候補プールにする(質向上)。片方が空でも続ける。
        if is_watch(g):
            # 全投稿が要件なので _cap_x の件数制限はかけない。ニュース RSS も使わない。
            tweets, watch_ok[g] = watch.collect(watch_handles, watch_since, watch_until, keys)
            return g, [_trim(t) for t in tweets], len(tweets), len(tweets), []
        try:
            tweets = _cap_x(_mark_watched(g, xclient.collect(g, settings=settings, keys=keys)))
        except Exception as e:
            print(f"  {g}: X収集失敗のためスキップ ({type(e).__name__}: {e})", file=sys.stderr)
            tweets = []
        news = _newsfeed_candidates(g, settings)
        n_off = sum(1 for t in tweets if t.get("_official"))
        return g, [_trim(t) for t in tweets] + news, len(tweets), n_off, news

    stats: dict[str, tuple[int, int, list[dict]]] = {}
    with ThreadPoolExecutor(max_workers=min(3, len(genres))) as pool:
        for g, cands, n_x, n_off, news in pool.map(_one, genres):  # 入力順を保つ
            cands_by_genre[g] = cands
            stats[g] = (n_x, n_off, news)

    x_usage = compute_x_usage(balance_before, _total_balance(keys))
    if x_usage:
        print(f"  X クレジット: 今回 {x_usage['used']:,} 使用・残り {x_usage['remaining']:,}",
              file=sys.stderr)
    else:
        print("  X クレジット: 残高を取得できず表示を省略", file=sys.stderr)

    # 本文は全ジャンル分まとめて1回(同じ記事が複数ジャンルにあっても取得1回・時間上限も1つ)。
    all_cands = [c for cs in cands_by_genre.values() for c in cs]
    n_target = len({c["url"] for c in all_cands if articles.is_target(c)})
    t0 = time.monotonic()
    articles.enrich_bodies(all_cands)
    n_bodied = len({c["url"] for c in all_cands if c.get("body")})
    print(f"  本文取得: {n_bodied}/{n_target} 記事 ({time.monotonic() - t0:.1f} 秒)", file=sys.stderr)
    for g in genres:
        n_x, n_off, news = stats[g]
        n_body = sum(1 for c in news if c.get("body"))
        print(f"  {g}: {len(cands_by_genre[g])} 件 収集 (X {n_x}[公式 {n_off}] + "
              f"ニュース {len(news)}[本文 {n_body}])", file=sys.stderr)

    mkt = market.fetch_market()
    print(f"  市況: {len(mkt)} 件", file=sys.stderr)
    # 今日の予定(次の配信まで)と直近24時間の指標の結果。取れなくても配信は続ける(空で渡す)。
    try:
        sched = schedule.fetch()
    except Exception as e:
        print(f"  今日の予定: 取得失敗のため省略 ({type(e).__name__}: {e})", file=sys.stderr)
        sched = {"schedule": [], "results": []}
    print(f"  今日の予定: {len(sched['schedule'])} 件 (直近の指標結果 {len(sched['results'])} 件)",
          file=sys.stderr)
    # 監視アカウントの取得期間。全アカウントの取得に成功したときだけ載せ、ingest がこれで状態を進める
    # (失敗した日は載せない=状態が進まず、次回に同じ範囲を取り直す)。
    watch_window = None
    if watch_until is not None:
        if all(watch_ok.values()):
            watch_window = {"since": watch.fmt_utc(watch_since), "until": watch.fmt_utc(watch_until)}
        else:
            print("  監視アカウント: 取得に失敗したアカウントがあるため、次回も同じ範囲から取り直します",
                  file=sys.stderr)
    out = {"date": day.isoformat(), "tz": settings.default_tz, "slot": args.slot,
           "x_usage": x_usage, "watch": watch_window,
           "market": mkt, "schedule": sched["schedule"], "indicator_results": sched["results"],
           "recent_titles": _recent_titles(genres, day),
           "genres": cands_by_genre}

    text = _dump_raw(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"raw を書き出し: {args.out}", file=sys.stderr)
    else:
        print(text)

    # 対象ジャンルはあったのに全ジャンルが0件 = API全滅。空ダイジェストを配信しないよう、
    # 「対象なし(64)」とは別の失敗コードで知らせる(deliver.sh が配信を中止する)。
    if not any(out["genres"].values()):
        print("全ジャンルの収集が0件でした(API一時障害の可能性)。配信を中止します。", file=sys.stderr)
        sys.exit(EXIT_ALL_FAILED)


def cmd_ingest(args) -> None:
    settings = get_settings()
    raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    curated = json.loads(Path(args.curated).read_text(encoding="utf-8"))
    # curated は {"genres": {genre: [items]}} か {genre: [items]} の両方を許容
    cur_genres = curated.get("genres", curated)
    local_date = date.fromisoformat(args.date) if args.date else date.fromisoformat(raw["date"])
    slot = args.slot or raw.get("slot", "morning")
    if slot not in SLOTS:
        sys.exit(f"未知のスロット: {slot}  有効: {list(SLOTS)}")

    # 全ジャンルの解析を DB 更新の前に済ませる(途中のジャンルで落ちて、前のジャンルだけ書き換わるのを防ぐ)
    parsed = {genre: parse_curated(cur_genres.get(genre, [])) for genre in raw["genres"]}

    init_db()
    with get_session() as session:
        for genre, tweets in raw["genres"].items():
            items = parsed[genre]
            d = digest.ingest_curated(session, genre, local_date, slot, items, tweets, commit=False)
            stored = digest.items_of_digest(session, d.id)
            n_big = sum(1 for it in stored if it.importance == "big")
            if not items and stored:
                note = " (収集/抽出0件 → 既存を保持・上書きしない)"  # 失敗の空で良い結果を壊さない
            elif not items:
                note = " (該当ニュースなし)"
            else:
                note = ""
            print(f"  {genre}: {len(stored)} 件 (大{n_big}){note}", file=sys.stderr)
        session.commit()  # 全ジャンルを1トランザクションで確定する
        digest.save_market(session, local_date, slot, raw.get("market") or [])
        digest.save_x_usage(session, local_date, slot, raw.get("x_usage"))
        digest.save_schedule(session, local_date, slot, raw.get("schedule") or [])
    _advance_watch_state(raw, cur_genres, local_date)
    print(f"ingest 完了 ({local_date} / {slot}, 市況 {len(raw.get('market') or [])} 件, "
          f"予定 {len(raw.get('schedule') or [])} 件)", file=sys.stderr)


def _advance_watch_state(raw: dict, cur_genres: dict, local_date: date) -> None:
    """取り込みに成功した監視アカウントの取得期間を記録する(次回はこの続きから集める)。

    - キュレーション結果に監視ジャンルのキー自体が無いときは進めない(取り込めなかった投稿を次回も取り直す)。
      キーがあって空配列なのは「中身の無い投稿だけだった」正常な結果なので進める。
    - 同じ配信日の2回目以降(今すぐ更新・リカバリ・1:1 の今すぐ)では進めない。進めると、その日の朝の配信以降の
      投稿が翌朝の窓から外れ、グループの LINE に一度も届かなくなるため(重複は recent_titles で抑える)。"""
    w = raw.get("watch")
    wg = [g for g in raw["genres"] if is_watch(g)]
    if not w or not wg:
        return
    if not all(g in cur_genres or not raw["genres"][g] for g in wg):
        print("  監視アカウント: キュレーション結果に無いため、次回も同じ範囲から取り直します", file=sys.stderr)
        return
    if watch.load_state().get("date") == local_date.isoformat():
        return
    try:
        watch.save_state(local_date, w["since"], w["until"])
    except Exception as e:  # 記録できなくても配信は続ける(次回に同じ範囲を取り直すだけ)
        print(f"  監視アカウント: watch_state.json を保存できず続行 ({type(e).__name__}: {e})", file=sys.stderr)
        return
    print(f"  監視アカウント: 取得期間を記録 ({w['since']} 〜 {w['until']})", file=sys.stderr)


# キュレーションを並列に分けるときのジャンルの組。同じ出来事が重なりやすいジャンルを同じ組に入れる
# 組をまたぐ同じ出来事は1件にまとめられない。試走5で重複した5件中4件がテクノロジーと話題の間だったので同じ組にする。
# 監視アカウントのジャンル(watch=true)はここに入れない。登録アカウントが増えても他のジャンルの
# キュレーションを圧迫しないよう、候補があれば raw の大きさに関わらず必ず独立した組にする(split_raw)。
CURATE_GROUPS = [["特大", "AI", "株"], ["暗号資産", "テクノロジー", "話題"]]
# raw がこれ未満なら分割しない。1セッションで読み切れる大きさなら、組をまたぐ重複を避けられる
# 1セッションの方がよい(288KB では問題なく、489KB で文脈があふれ自動圧縮が走った。2026-10-02 実測)。
SPLIT_MIN_BYTES = 300_000
_SPLIT_KEEP_KEYS = ("date", "tz", "slot", "market", "schedule", "indicator_results")


def split_raw(raw: dict, whole: bool = False) -> list[dict]:
    """raw をジャンルの組ごとの raw に分ける。候補の配列(と `i`)は変えない。0ジャンルの組は出さない。

    監視アカウントのジャンルは、候補があれば独立した組(最後)にし、候補が0件なら組に入れない
    (空の組で claude を起動しない。curated に無いジャンルは ingest が空として扱う)。
    whole=True なら監視アカウント以外は分けずに1組にする(raw が小さいとき)。"""
    genres = raw["genres"]
    normal = [g for g in genres if not is_watch(g)]
    watch_grp = [g for g in genres if is_watch(g) and genres[g]]
    if whole:
        groups = [normal]
    else:
        size = {g: len(json.dumps(genres[g], ensure_ascii=False).encode()) for g in normal}
        groups = [[g for g in grp if g in normal] for grp in CURATE_GROUPS]
        known = {g for grp in CURATE_GROUPS for g in grp}
        for g in normal:
            if g not in known:  # 組に無いジャンルは、その時点で小さい方の組へ(偏りを抑える)
                min(groups, key=lambda grp: sum(size[x] for x in grp)).append(g)
    groups.append(watch_grp)
    parts = []
    for grp in groups:
        if not grp:
            continue
        part = {k: raw[k] for k in _SPLIT_KEEP_KEYS}
        part["recent_titles"] = {g: t for g, t in raw.get("recent_titles", {}).items() if g in grp}
        part["genres"] = {g: c for g, c in genres.items() if g in grp}
        parts.append(part)
    return parts


def cmd_split(args) -> None:
    src = Path(args.raw)
    raw = json.loads(src.read_text(encoding="utf-8"))
    small = src.stat().st_size < SPLIT_MIN_BYTES
    # 小さく、監視アカウントの候補も無ければ分けない(=従来どおり1セッション)
    if small and not any(is_watch(g) and c for g, c in raw["genres"].items()):
        print(src)
        return
    base = str(src)[:-len(".json")] if src.name.endswith(".json") else str(src)
    for n, part in enumerate(split_raw(raw, whole=small)):
        dst = Path(f"{base}.p{n}.json")
        dst.write_text(_dump_raw(part), encoding="utf-8")
        print(dst)


def cmd_merge(args) -> None:
    merged: dict = {}
    for p in args.parts:
        try:
            g = json.loads(Path(p).read_text(encoding="utf-8")).get("genres")
        except (OSError, ValueError, AttributeError) as e:
            sys.exit(f"merge: {p} を読めません: {e}")
        if not isinstance(g, dict):
            sys.exit(f"merge: {p} の genres が dict ではありません")
        # 中身の型もここで弾く(通すと ingest が途中のジャンルで落ち、DB が部分更新になる)
        bad = [k for k, v in g.items() if not isinstance(v, list) or not all(isinstance(x, dict) for x in v)]
        if bad:
            sys.exit(f"merge: {p} のジャンルの値が「dict の配列」ではありません: {bad}")
        dup = merged.keys() & g.keys()
        if dup:
            sys.exit(f"merge: ジャンルが重複しています: {sorted(dup)} ({p})")
        merged.update(g)
    Path(args.out).write_text(json.dumps({"genres": merged}, ensure_ascii=False, indent=2),
                              encoding="utf-8")


def cmd_push(args) -> None:
    settings = get_settings()
    if not settings.line_channel_access_token:
        sys.exit("LINE_CHANNEL_ACCESS_TOKEN が未設定です。")
    messenger = lc.LineMessenger(settings.line_channel_access_token)
    default_slot = args.slot or slot_for_now(datetime.now(ZoneInfo(settings.default_tz)))
    # 収集〜送信が深夜0時を跨ぐと現在日にはダイジェストが無く空配信になるため、
    # deliver.sh は raw の日付を --date で渡してくる(省略時は従来どおり現在日)。
    digest_date = date.fromisoformat(args.date) if args.date else None
    init_db()
    with get_session() as session:
        if args.user:
            # 今すぐ配信: 指定ユーザーへ。配信済みフラグは立てない(定刻枠を消費しない)。
            sub = session.exec(
                select(Subscriber).where(Subscriber.line_user_id == args.user)
            ).first()
            if not sub:
                sys.exit(f"購読者が見つかりません: {args.user}")
            specs = deliver_to_subscriber(
                session, sub, default_slot, messenger=messenger, mark_delivered=False,
                digest_date=digest_date,
            )
            print(f"push 完了 → {args.user} slot={default_slot} ({len(specs)} メッセージ)", file=sys.stderr)
        elif args.due:
            # 定刻配信(deliver.sh から): 当該スロットが有効で当日未配信の全購読者へ。送信後に配信済み記録。
            subs = session.exec(
                select(Subscriber).where(Subscriber.is_onboarded == True)  # noqa: E712
            ).all()
            sent = 0
            for sub in subs:
                now_local = datetime.now(ZoneInfo(sub.tz))
                if not sub.enabled_genres or not sub.slot_enabled(default_slot):
                    continue
                if sub.last_on(default_slot) == now_local.date():
                    continue
                try:
                    specs = deliver_to_subscriber(
                        session, sub, default_slot, messenger=messenger,
                        now_local=now_local, mark_delivered=True, digest_date=digest_date,
                        skip_if_empty=True,  # 空(全ジャンル0件)は送らず配信済みにもしない=次回に委ねる
                    )
                    if not specs:
                        print(f"  skip(空) {sub.line_user_id} slot={default_slot}", file=sys.stderr)
                        continue
                    sent += 1
                    print(f"  push → {sub.line_user_id} slot={default_slot}", file=sys.stderr)
                except Exception as e:  # 1人の失敗で全体を止めない
                    print(f"  push 失敗 {sub.line_user_id}: {e}", file=sys.stderr)
            print(f"push(due) 完了 slot={default_slot} ({sent} 名)", file=sys.stderr)
        else:
            sys.exit("--user または --due を指定してください。")


def cmd_pending(args) -> None:
    """当該スロットの当日分がまだ届いていない購読者がいるかを返す。

    exit 0 = 未配信あり(配信すべき) / EXIT_NO_TARGET = 全員配信済み(何もしなくてよい)。
    リカバリ実行(deliver.sh --recover)が、収集やヘッドレス Claude を呼ぶ前に
    空振りかどうかを判定するために使う(成功済みの日に何度起動されても無害にする)。
    """
    settings = get_settings()
    slot = args.slot or slot_for_now(datetime.now(ZoneInfo(settings.default_tz)))
    init_db()
    with get_session() as session:
        subs = session.exec(
            select(Subscriber).where(Subscriber.is_onboarded == True)  # noqa: E712
        ).all()
        pending = [
            sub.line_user_id
            for sub in subs
            if sub.enabled_genres
            and sub.slot_enabled(slot)
            and sub.last_on(slot) != datetime.now(ZoneInfo(sub.tz)).date()
        ]
    if not pending:
        print(f"未配信なし slot={slot}", file=sys.stderr)
        sys.exit(EXIT_NO_TARGET)
    print(f"未配信 {len(pending)} 名 slot={slot}", file=sys.stderr)


def cmd_alert(args) -> None:
    """運用アラートを購読者本人へ LINE で1通だけ送る(当日中の復旧に失敗したとき)。

    宛先はグループ(push_to)ではなく本人(line_user_id)。グループ宛 push は
    グループ内の友だち人数分が課金される(実測3通)ため、無料枠200通/月を無駄にしない。
    """
    settings = get_settings()
    if not settings.line_channel_access_token:
        sys.exit("LINE_CHANNEL_ACCESS_TOKEN が未設定です。")
    messenger = lc.LineMessenger(settings.line_channel_access_token)
    init_db()
    with get_session() as session:
        subs = session.exec(
            select(Subscriber).where(Subscriber.is_onboarded == True)  # noqa: E712
        ).all()
        targets = [sub.line_user_id for sub in subs if sub.enabled_genres]
    if not targets:
        print("アラート対象なし", file=sys.stderr)
        sys.exit(EXIT_NO_TARGET)
    for to in targets:
        try:
            messenger.push(to, [lc.text_spec(args.text)])
            print(f"アラート送信 → {to}", file=sys.stderr)
        except Exception as e:  # 通知が落ちても呼び出し元(リカバリ)の後始末は続ける
            print(f"アラート送信 失敗 {to}: {e}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description="XNewsBot パイプライン")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("collect", help="Xから収集して raw JSON を出力")
    pc.add_argument("--genres", default="", help="カンマ区切り(例 AI,株)")
    pc.add_argument("--due", action="store_true", help="購読者の有効ジャンルの和集合+常時ジャンルを対象に")
    pc.add_argument("--user", help="指定ユーザーの有効ジャンル+常時ジャンルを対象に(今すぐ配信)")
    pc.add_argument("--slot", choices=SLOTS, default="morning", help="朝=morning / 夜=evening")
    pc.add_argument("--out", help="出力先ファイル(省略時は標準出力)")

    pi = sub.add_parser("ingest", help="キュレーション結果(curated)を DB に取り込む")
    pi.add_argument("--raw", required=True, help="collect が出した raw JSON")
    pi.add_argument("--curated", required=True, help="Claude Code が書いた curated JSON")
    pi.add_argument("--date", help="YYYY-MM-DD(省略時は raw の date)")
    pi.add_argument("--slot", choices=SLOTS, help="省略時は raw の slot")

    pp = sub.add_parser("push", help="当日ダイジェストを LINE へ push")
    pp.add_argument("--user", help="指定ユーザーへ送る(今すぐ配信。配信済みにしない)")
    pp.add_argument("--due", action="store_true", help="当該スロットが有効で未配信の全購読者へ送る(定刻配信。配信済みにする)")
    pp.add_argument("--slot", choices=SLOTS, help="省略時は現在時刻から推定")
    pp.add_argument("--date", help="配信するダイジェストの日付 YYYY-MM-DD(省略時は現在日。0時跨ぎ対策)")

    pn = sub.add_parser("pending", help="当日未配信の購読者がいるか(exit 0=いる / 64=いない)")
    pn.add_argument("--slot", choices=SLOTS, help="省略時は現在時刻から推定")

    pa = sub.add_parser("alert", help="運用アラートを購読者本人へ LINE で1通送る")
    pa.add_argument("--text", required=True, help="送信する本文")

    ps = sub.add_parser("split", help="raw をジャンルの組ごとに分け、書いたパスを1行ずつ出す(小さければ元のパスだけ)")
    ps.add_argument("--raw", required=True, help="collect が出した raw JSON")

    pm = sub.add_parser("merge", help="組ごとの curated JSON を1つにまとめる")
    pm.add_argument("--out", required=True, help="まとめた curated JSON の出力先")
    pm.add_argument("parts", nargs="+", help="組ごとの curated JSON")

    args = p.parse_args()
    {
        "collect": cmd_collect,
        "ingest": cmd_ingest,
        "split": cmd_split,
        "merge": cmd_merge,
        "push": cmd_push,
        "pending": cmd_pending,
        "alert": cmd_alert,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
