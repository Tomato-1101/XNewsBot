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
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from xnewsbot import digest, newsfeeds, xclient  # noqa: E402
from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot.config import get_settings  # noqa: E402
from xnewsbot.curator import CURATE_INPUT_LIMIT, parse_curated  # noqa: E402
from xnewsbot.db import get_session, init_db  # noqa: E402
from xnewsbot.genres import ALWAYS_KEYS, GENRE_KEYS, is_valid_genre, keywords, lang  # noqa: E402
from xnewsbot.models import SLOTS, Subscriber  # noqa: E402
from xnewsbot.scheduler import deliver_to_subscriber, slot_for_now  # noqa: E402


# collect の「対象ジャンルなし(購読者未登録)」を本物の失敗と区別するための exit code。
# ops/deliver.sh がこの値のときだけ正常スキップ扱いにする(契約。変えたら deliver.sh も合わせる)。
EXIT_NO_TARGET = 64
# 対象ジャンルはあったが全ジャンルが収集0件(API全滅)。空ダイジェストを「成功」配信しないため、
# deliver.sh はこの値を失敗(配信中止)として扱う(契約。変えたら deliver.sh も合わせる)。
EXIT_ALL_FAILED = 65


def _today(settings) -> date:
    return datetime.now(ZoneInfo(settings.default_tz)).date()


def _trim(t: dict) -> dict:
    """キュレーション + 出典マッピングに必要な項目だけに絞る(raw JSON を小さく読みやすく)。"""
    a = t.get("author") or {}
    return {
        "text": " ".join((t.get("text") or "").split()),
        "viewCount": t.get("viewCount") or 0,
        "likeCount": t.get("likeCount") or 0,
        "url": t.get("url", ""),
        "createdAt": t.get("createdAt", ""),
        "author": {"userName": a.get("userName", "?"), "followers": a.get("followers", 0)},
    }


def _newsfeed_candidates(genre: str, settings) -> list[dict]:
    """無料ニュース(Google ニュースRSS)をジャンルの候補に足す(質向上・コスト度外視)。

    X(twitterapi.io)由来の候補に、大手報道の記事見出しを加えて Claude の選択肢を厚くする。
    lang="any" のジャンルは英語ロケールも引いて世界の一次ニュースも拾う。失敗時は空(=Xのみ・無害)。
    """
    if not settings.collect_use_newsfeeds:
        return []
    kws = keywords(genre)
    if not kws:
        return []
    terms = kws[:6]
    query = "(" + " OR ".join(terms) + ")" if len(terms) > 1 else terms[0]
    items = newsfeeds.google_news(query, within_hours=settings.collect_hours)
    if lang(genre) == "any":
        items += newsfeeds.google_news(query, lang="en", region="US",
                                       within_hours=settings.collect_hours)
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        if it.url in seen:
            continue
        seen.add(it.url)
        out.append(newsfeeds.as_candidate(it))
        if len(out) >= settings.collect_newsfeeds_per_genre:
            break
    return out


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

    out = {"date": _today(settings).isoformat(), "tz": settings.default_tz,
           "slot": args.slot, "genres": {g: [] for g in genres}}

    # 鍵は不変。ジャンルごとに load_keys()→Keychain サブプロセスを叩くのは無駄かつ並列で多重に
    # security を起動するので、ここで1度だけ取得して各収集に渡す(優先度順・フォールバック用)。
    keys = xclient.load_keys(settings)

    # ジャンル収集は I/O 待ち(twitterapi.io)。直列だと数分かかるので並列化するが、同一APIキーへ
    # 多並列(以前は6)だと混雑→同時多発タイムアウトを招くため 3 に抑える(xclient 側で再試行もする)。
    def _one(g: str) -> tuple[str, list[dict], int, int]:
        # 1ジャンルの失敗(再試行しても回復しないタイムアウト/恒久エラー)で収集全体を落とさない。
        # 取れたジャンルだけで配信を続ける(空になったジャンルはキュレーションで空配列扱い)。
        # X(有料)と 無料ニュース(RSS)の両方を集めて候補プールにする(質向上)。片方が空でも続ける。
        try:
            tweets = xclient.collect(g, settings=settings, keys=keys)[:CURATE_INPUT_LIMIT]
        except Exception as e:
            print(f"  {g}: X収集失敗のためスキップ ({type(e).__name__}: {e})", file=sys.stderr)
            tweets = []
        news = _newsfeed_candidates(g, settings)
        return g, [_trim(t) for t in tweets] + news, len(tweets), len(news)

    with ThreadPoolExecutor(max_workers=min(3, len(genres))) as pool:
        for g, cands, n_x, n_news in pool.map(_one, genres):  # 入力順を保つ
            out["genres"][g] = cands
            print(f"  {g}: {len(cands)} 件 収集 (X {n_x} + ニュース {n_news})", file=sys.stderr)

    text = json.dumps(out, ensure_ascii=False, indent=2)
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

    init_db()
    with get_session() as session:
        for genre, tweets in raw["genres"].items():
            items = parse_curated(cur_genres.get(genre, []))
            d = digest.ingest_curated(session, genre, local_date, slot, items, tweets)
            stored = digest.items_of_digest(session, d.id)
            n_big = sum(1 for it in stored if it.importance == "big")
            if not items and stored:
                note = " (収集/抽出0件 → 既存を保持・上書きしない)"  # 失敗の空で良い結果を壊さない
            elif not items:
                note = " (該当ニュースなし)"
            else:
                note = ""
            print(f"  {genre}: {len(stored)} 件 (大{n_big}){note}", file=sys.stderr)
    print(f"ingest 完了 ({local_date} / {slot})", file=sys.stderr)


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

    args = p.parse_args()
    {
        "collect": cmd_collect,
        "ingest": cmd_ingest,
        "push": cmd_push,
        "pending": cmd_pending,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
