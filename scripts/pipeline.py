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

from xnewsbot import digest, xclient  # noqa: E402
from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot.config import get_settings  # noqa: E402
from xnewsbot.curator import CURATE_INPUT_LIMIT, parse_curated  # noqa: E402
from xnewsbot.db import get_session, init_db  # noqa: E402
from xnewsbot.genres import ALWAYS_KEYS, GENRE_KEYS, is_valid_genre  # noqa: E402
from xnewsbot.models import SLOTS, Subscriber  # noqa: E402
from xnewsbot.scheduler import deliver_to_subscriber, slot_for_now  # noqa: E402


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
        sys.exit("対象ジャンルがありません(--due/--user なら購読者が未登録の可能性)。")

    out = {"date": _today(settings).isoformat(), "tz": settings.default_tz,
           "slot": args.slot, "genres": {g: [] for g in genres}}

    # ジャンル収集は I/O 待ち(twitterapi.io)。直列だと7ジャンルで数分かかるので並列化する。
    def _one(g: str) -> tuple[str, list[dict]]:
        return g, xclient.collect(g, settings=settings)[:CURATE_INPUT_LIMIT]

    with ThreadPoolExecutor(max_workers=min(6, len(genres))) as pool:
        for g, tweets in pool.map(_one, genres):  # 入力順を保つ
            out["genres"][g] = [_trim(t) for t in tweets]
            print(f"  {g}: {len(tweets)} 件 収集", file=sys.stderr)

    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"raw を書き出し: {args.out}", file=sys.stderr)
    else:
        print(text)


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
            n_big = sum(1 for it in digest.items_of_digest(session, d.id) if it.importance == "big")
            print(f"  {genre}: {len(items)} 件取り込み (大{n_big})", file=sys.stderr)
    print(f"ingest 完了 ({local_date} / {slot})", file=sys.stderr)


def cmd_push(args) -> None:
    settings = get_settings()
    if not settings.line_channel_access_token:
        sys.exit("LINE_CHANNEL_ACCESS_TOKEN が未設定です。")
    messenger = lc.LineMessenger(settings.line_channel_access_token)
    default_slot = args.slot or slot_for_now(datetime.now(ZoneInfo(settings.default_tz)))
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
                session, sub, default_slot, messenger=messenger, mark_delivered=False
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
                    deliver_to_subscriber(
                        session, sub, default_slot, messenger=messenger,
                        now_local=now_local, mark_delivered=True,
                    )
                    sent += 1
                    print(f"  push → {sub.line_user_id} slot={default_slot}", file=sys.stderr)
                except Exception as e:  # 1人の失敗で全体を止めない
                    print(f"  push 失敗 {sub.line_user_id}: {e}", file=sys.stderr)
            print(f"push(due) 完了 slot={default_slot} ({sent} 名)", file=sys.stderr)
        else:
            sys.exit("--user または --due を指定してください。")


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

    args = p.parse_args()
    {"collect": cmd_collect, "ingest": cmd_ingest, "push": cmd_push}[args.cmd](args)


if __name__ == "__main__":
    main()
