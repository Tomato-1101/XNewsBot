"""ダイジェストの永続化(ingest)と組み立て(assemble)。

新方式: 収集は scripts/pipeline.py、キュレーションは Claude Code が行う。
本モジュールは「キュレーション済みアイテム + 元ツイート」を DB に取り込み(ingest)、
配信時には DB の既存ダイジェストから組み立てる(assemble)だけ。LLM は呼ばない。

「日×ジャンル」で1ダイジェスト。複数購読者で再利用する。
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from . import xclient
from .curator import CuratedItem
from .models import GenreDigest, NewsItem


def _build_news_items(
    digest_id: int, genre: str, curated: list[CuratedItem], tweets: list[dict]
) -> list[NewsItem]:
    items: list[NewsItem] = []
    for rank, ci in enumerate(curated):
        srcs = [tweets[i] for i in ci.source_idxs if 0 <= i < len(tweets)]
        source_tweets = [
            {
                "text": " ".join((t.get("text") or "").split()),
                "author": (t.get("author") or {}).get("userName", "?"),
                "url": t.get("url", ""),
                "views": xclient.views(t),
            }
            for t in srcs
        ]
        source_urls = [s["url"] for s in source_tweets if s["url"]]
        top_views = max((s["views"] for s in source_tweets), default=0)
        items.append(
            NewsItem(
                genre_digest_id=digest_id,
                genre=genre,
                importance=ci.importance,
                rank=rank,
                title=ci.title,
                summary=ci.summary,
                source_urls=source_urls,
                source_tweets=source_tweets,
                top_view_count=top_views,
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
) -> GenreDigest:
    """キュレーション済みアイテムを DB に取り込む。
    当日・当スロット・当ジャンルの既存ダイジェストがあれば置き換える(再実行で冪等)。"""
    existing = get_genre_digest(session, genre, local_date, slot)
    if existing:
        for it in items_of_digest(session, existing.id):
            session.delete(it)
        session.delete(existing)
        session.commit()

    digest = GenreDigest(digest_date=local_date, slot=slot, genre=genre)
    session.add(digest)
    session.commit()
    session.refresh(digest)

    for item in _build_news_items(digest.id, genre, curated, tweets):
        session.add(item)
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
