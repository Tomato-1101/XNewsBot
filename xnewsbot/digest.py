"""ダイジェストの永続化(ingest)と組み立て(assemble)。

新方式: 収集は scripts/pipeline.py、キュレーションは Claude Code が行う。
本モジュールは「キュレーション済みアイテム + 元ツイート」を DB に取り込み(ingest)、
配信時には DB の既存ダイジェストから組み立てる(assemble)だけ。LLM は呼ばない。

「日×ジャンル」で1ダイジェスト。複数購読者で再利用する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

from sqlmodel import Session, select

from . import xclient
from .curator import CuratedItem
from .genres import GENRE_KEYS
from .models import GenreDigest, MarketSnapshot, NewsItem, ScheduleSnapshot, XUsageSnapshot

_X_CREATED_FMT = "%a %b %d %H:%M:%S %z %Y"  # 例: 'Tue Sep 30 12:34:56 +0000 2026'


def _created_iso(raw) -> str:
    """候補の createdAt を ISO8601(UTC) 文字列にそろえる(表示の「N時間前」に使う)。

    X は 'Tue Sep 30 12:34:56 +0000 2026'、ニュースは ISO8601 や RFC2822 のことがあるので
    順に試す。どれでも解析できなければ ""(表示側で時刻を省く)。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    for parse in (lambda v: datetime.strptime(v, _X_CREATED_FMT),
                  datetime.fromisoformat, parsedate_to_datetime):
        try:
            dt = parse(s)
        except (TypeError, ValueError, IndexError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    return ""


def _source_entry(t: dict) -> dict:
    """候補1件を NewsItem.source_tweets の1要素にする(出典名・時刻・種別つき)。"""
    kind = "news" if t.get("source") == "news" or t.get("kind") == "news" else "x"
    author = (t.get("author") or {}).get("userName", "?")
    media = t.get("media") or ("" if author == "?" else (author if kind == "news" else f"@{author}"))
    return {
        "text": " ".join((t.get("text") or "").split()),
        "author": author,
        "url": t.get("url", ""),
        "views": xclient.views(t),
        "media": media,
        "created_at": _created_iso(t.get("createdAt")),
        "kind": kind,
    }


def _build_news_items(
    digest_id: int, genre: str, curated: list[CuratedItem], tweets: list[dict]
) -> list[NewsItem]:
    items: list[NewsItem] = []
    # 表示順は rank。プロンプトでも「big 先・score 降順」を指示しているが、モデルの並びは
    # 崩れることがある(試走で small の score が前後した)ので取り込み時に確定させる。同点は元の順を保つ。
    ordered = sorted(curated, key=lambda c: (c.importance != "big", -c.score))
    for rank, ci in enumerate(ordered):
        srcs = [tweets[i] for i in ci.source_idxs if 0 <= i < len(tweets)]
        source_tweets = [_source_entry(t) for t in srcs]
        source_urls = [s["url"] for s in source_tweets if s["url"]]
        top_views = max((s["views"] for s in source_tweets), default=0)
        # 表示用ジャンルタグ: Claude が付けた関連ジャンル(既知キーのみ)に主ジャンルを足し、
        # 表示順(GENRE_KEYS)で整列・重複排除。タグが無ければ主ジャンルのみ。
        tag_set = {g for g in ci.genres if g in GENRE_KEYS} | {genre}
        tags = [g for g in GENRE_KEYS if g in tag_set]
        items.append(
            NewsItem(
                genre_digest_id=digest_id,
                genre=genre,
                genres=tags,
                importance=ci.importance,
                rank=rank,
                title=ci.title,
                summary=ci.summary,
                detail=ci.detail,
                source_urls=source_urls,
                source_tweets=source_tweets,
                top_view_count=top_views,
                score=ci.score,
            )
        )
    return items


def get_genre_digest(
    session: Session, genre: str, local_date: date, slot: str
) -> GenreDigest | None:
    return session.exec(
        select(GenreDigest).where(
            GenreDigest.digest_date == local_date,
            GenreDigest.slot == slot,
            GenreDigest.genre == genre,
        )
    ).first()


def ingest_curated(
    session: Session,
    genre: str,
    local_date: date,
    slot: str,
    curated: list[CuratedItem],
    tweets: list[dict],
    commit: bool = True,
) -> GenreDigest:
    """キュレーション済みアイテムを DB に取り込む。
    当日・当スロット・当ジャンルの既存ダイジェストがあれば置き換える(再実行で冪等)。

    取り込む内容が空(curated が空 = 収集失敗やノイズのみ)で、かつ既存ダイジェストがある場合は
    置き換えない(既存をそのまま返す)。これは収集失敗の「今すぐ」配信が、定刻に作られた良い
    ダイジェストを空で上書き破壊するのを防ぐため。既存が無い場合は従来どおり空ダイジェストを作る
    (「キュレーション済み・該当ニュースなし」を表し、catch-up の揃い判定が完了とみなせる)。

    commit=False なら確定は呼び出し側に任せる(複数ジャンルを1トランザクションで書くため)。"""
    existing = get_genre_digest(session, genre, local_date, slot)
    if not curated and existing:
        return existing
    # 削除と作成は1トランザクションにまとめる(中間 commit を挟むと、その隙にプロセスが落ちた
    # ときに当日分が「消えたまま作られていない」状態で残り、catch-up の揃い判定が永久に揃わない)。
    if existing:
        for it in items_of_digest(session, existing.id):
            session.delete(it)
        session.delete(existing)
        session.flush()

    digest = GenreDigest(digest_date=local_date, slot=slot, genre=genre)
    session.add(digest)
    session.flush()  # commit せずに id だけ採番する(NewsItem の外部キーに要る)
    session.refresh(digest)

    for item in _build_news_items(digest.id, genre, curated, tweets):
        session.add(item)
    if commit:
        session.commit()
    return digest


def items_of_digest(session: Session, digest_id: int) -> list[NewsItem]:
    return list(
        session.exec(
            select(NewsItem).where(NewsItem.genre_digest_id == digest_id).order_by(NewsItem.rank)
        )
    )


def missing_genres(
    session: Session, genres: list[str], local_date: date, slot: str
) -> list[str]:
    """当日・当スロットのダイジェストがまだ無いジャンル(=キュレーション未実施)。"""
    return [g for g in genres if get_genre_digest(session, g, local_date, slot) is None]


def assemble_for_genres(
    session: Session, genres: list[str], local_date: date, slot: str
) -> dict[str, list[NewsItem]]:
    """購読ジャンルごとに当日・当スロットの NewsItem を返す(DB の既存ダイジェストのみ。無ければ空)。"""
    out: dict[str, list[NewsItem]] = {}
    for genre in genres:
        digest = get_genre_digest(session, genre, local_date, slot)
        out[genre] = items_of_digest(session, digest.id) if digest else []
    return out


def save_market(session: Session, d: date, slot: str, market: list[dict]) -> None:
    """当日・当スロットの市況を保存する(同日同スロットの既存は置き換える)。

    取得に失敗して空のときは既存を消さない(ダイジェストと同じく、失敗した再取得が
    良いデータを空で上書きしないようにする)。"""
    existing = session.exec(
        select(MarketSnapshot).where(MarketSnapshot.digest_date == d, MarketSnapshot.slot == slot)
    ).all()
    if not market and existing:
        return
    for snap in existing:
        session.delete(snap)
    session.add(MarketSnapshot(digest_date=d, slot=slot, data=list(market)))
    session.commit()


def get_market(session: Session, d: date, slot: str) -> list[dict]:
    """当日・当スロットの市況(無ければ空)。"""
    snap = session.exec(
        select(MarketSnapshot)
        .where(MarketSnapshot.digest_date == d, MarketSnapshot.slot == slot)
        .order_by(MarketSnapshot.id.desc())
    ).first()
    return list(snap.data) if snap else []


def save_schedule(session: Session, d: date, slot: str, schedule: list[dict]) -> None:
    """当日・当スロットの「今日の予定」を保存する(空なら既存を消さない=save_market と同じ)。

    既存とはマージする: 同じ予定(name と at が同じ)は新しい値で上書きし、新しい結果に無い既存の
    予定は残す。再収集で一部の取得元だけ失敗したときに、未到来の FOMC 等が消えないようにするため。
    並び順・件数の絞り込みは表示側(line_client._schedule_rows)が行う。"""
    existing = session.exec(
        select(ScheduleSnapshot).where(ScheduleSnapshot.digest_date == d, ScheduleSnapshot.slot == slot)
        .order_by(ScheduleSnapshot.id)
    ).all()
    if not schedule and existing:
        return
    key = lambda ev: (ev.get("name"), ev.get("at"))  # noqa: E731
    new_by_key = {key(ev): ev for ev in schedule}
    old = list(existing[-1].data) if existing else []  # get_schedule と同じく最新のスナップショット
    old_keys = {key(ev) for ev in old}
    merged = [new_by_key.get(key(ev), ev) for ev in old]
    merged += [ev for ev in schedule if key(ev) not in old_keys]
    for snap in existing:
        session.delete(snap)
    session.add(ScheduleSnapshot(digest_date=d, slot=slot, data=merged))
    session.commit()


def get_schedule(session: Session, d: date, slot: str) -> list[dict]:
    """当日・当スロットの「今日の予定」(無ければ空)。"""
    snap = session.exec(
        select(ScheduleSnapshot)
        .where(ScheduleSnapshot.digest_date == d, ScheduleSnapshot.slot == slot)
        .order_by(ScheduleSnapshot.id.desc())
    ).first()
    return list(snap.data) if snap else []


def save_x_usage(session: Session, d: date, slot: str, usage: dict | None) -> None:
    """当日・当スロットの twitterapi.io クレジット消費 {"used","remaining"} を保存する(置き換え)。

    取得に失敗して None のときは何もしない(既存を消さない=save_market と同じ)。"""
    if not usage:
        return
    for snap in session.exec(
        select(XUsageSnapshot).where(XUsageSnapshot.digest_date == d, XUsageSnapshot.slot == slot)
    ).all():
        session.delete(snap)
    session.add(XUsageSnapshot(digest_date=d, slot=slot, used=int(usage["used"]),
                               remaining=int(usage["remaining"])))
    session.commit()


def get_x_usage(session: Session, d: date, slot: str) -> dict | None:
    """当日・当スロットのクレジット消費 {"used","remaining"}(無ければ None)。"""
    snap = session.exec(
        select(XUsageSnapshot)
        .where(XUsageSnapshot.digest_date == d, XUsageSnapshot.slot == slot)
        .order_by(XUsageSnapshot.id.desc())
    ).first()
    return {"used": snap.used, "remaining": snap.remaining} if snap else None
