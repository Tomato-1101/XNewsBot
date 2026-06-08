"""ダイジェスト構築: 収集 → キュレーション → GenreDigest/NewsItem 永続化。

「日×ジャンル」で1回だけキュレーションし、複数購読者で再利用する(無駄な再生成を防ぐ)。
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Session, select

from . import xclient
from .config import Settings, get_settings
from .curator import CuratedItem, Curator
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


def build_genre_digest(
    session: Session,
    genre: str,
    local_date: date,
    *,
    curator: Curator,
    settings: Settings | None = None,
    key: str | None = None,
) -> GenreDigest:
    """ジャンルの当日ダイジェストを収集+キュレーションして保存する(常に新規作成)。"""
    settings = settings or get_settings()
    tweets = xclient.collect(genre, settings=settings, key=key)
    curated = curator.curate(genre, tweets)

    digest = GenreDigest(digest_date=local_date, genre=genre)
    session.add(digest)
    session.commit()
    session.refresh(digest)

    for item in _build_news_items(digest.id, genre, curated, tweets):
        session.add(item)
    session.commit()
    return digest


def get_or_build_genre_digest(
    session: Session,
    genre: str,
    local_date: date,
    *,
    curator: Curator,
    settings: Settings | None = None,
    key: str | None = None,
) -> GenreDigest:
    """当日・当ジャンルの GenreDigest があれば再利用、無ければ構築する。"""
    existing = session.exec(
        select(GenreDigest).where(
            GenreDigest.digest_date == local_date, GenreDigest.genre == genre
        )
    ).first()
    if existing:
        return existing
    return build_genre_digest(
        session, genre, local_date, curator=curator, settings=settings, key=key
    )


def items_of_digest(session: Session, digest_id: int) -> list[NewsItem]:
    return list(
        session.exec(
            select(NewsItem).where(NewsItem.genre_digest_id == digest_id).order_by(NewsItem.rank)
        )
    )


def assemble_for_genres(
    session: Session,
    genres: list[str],
    local_date: date,
    *,
    curator: Curator,
    settings: Settings | None = None,
    key: str | None = None,
) -> dict[str, list[NewsItem]]:
    """購読ジャンルごとに当日の NewsItem を返す(無ければ構築)。"""
    out: dict[str, list[NewsItem]] = {}
    for genre in genres:
        digest = get_or_build_genre_digest(
            session, genre, local_date, curator=curator, settings=settings, key=key
        )
        out[genre] = items_of_digest(session, digest.id)
    return out
