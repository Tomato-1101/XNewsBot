"""ニュースの評価(興味あり/興味なし)の記録と、キュレーションに渡す「好み」の集計。

評価はグループ全員分をまとめて「僕たちの好み」として扱う(人ごとに分けない)。
LINE(onboarding)と管理画面(admin/news.py)の両方から記録し、収集(scripts/pipeline.py)が
raw の `preferences` に入れる(形式は ops/curate_prompt.md が読む形。変えない)。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from . import mockdata
from .models import GenreDigest, NewsFeedback, NewsItem

VERDICT_LABELS = {"good": "興味あり", "bad": "興味なし"}
PREF_DAYS = 90   # raw に渡す評価の期間
PREF_MAX = 40    # good / bad それぞれの最大件数(raw を大きくしすぎない)


def title_hash(title: str) -> str:
    """評価キーの記事の目印(見出しの sha1 先頭8桁)。

    詳細タップの rank キーは、同じ日に取り込み直すと同じ rank が別の記事を指すので、評価には使わない。
    見出しで引けば、取り込み直しで記事が入れ替わっても別の記事に評価が付かない。"""
    return hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:8]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _find(session: Session, item_id: int, title: str, user_id: str) -> NewsFeedback | None:
    # NewsItem の id は取り込み直しで別の記事に使い回されうるので、見出しも一致したときだけ同じ記事とみなす
    return session.exec(select(NewsFeedback).where(
        NewsFeedback.newsitem_id == item_id, NewsFeedback.user_id == user_id,
        NewsFeedback.title == title)).first()


def record(session: Session, item: NewsItem, user_id: str, verdict: str) -> NewsFeedback:
    """item への user_id の評価を記録する(同じ人の同じ記事は上書き)。"""
    if verdict not in VERDICT_LABELS:
        raise ValueError(f"verdict が不正です: {verdict}")
    row = _find(session, item.id, item.title, user_id)
    now = _now()
    if row is None:
        gd = session.get(GenreDigest, item.genre_digest_id)
        row = NewsFeedback(newsitem_id=item.id, user_id=user_id, genre=item.genre, title=item.title,
                           digest_date=gd.digest_date if gd else None, created_at=now)
    row.verdict = verdict
    row.updated_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def verdicts_of(session: Session, items: dict[int, str], user_id: str) -> dict[int, str]:
    """{記事 id: 見出し} のうち user_id が評価済みのものの {id: verdict}(管理画面で押した状態を出す)。"""
    if not items:
        return {}
    rows = session.exec(select(NewsFeedback).where(
        NewsFeedback.user_id == user_id, NewsFeedback.newsitem_id.in_(list(items)))).all()
    return {r.newsitem_id: r.verdict for r in rows if items.get(r.newsitem_id) == r.title}


def preferences(session: Session, now: datetime | None = None, days: int = PREF_DAYS,
                limit: int = PREF_MAX) -> dict[str, list[dict]]:
    """raw の `preferences`: {"good": [{"genre","title"}], "bad": [...]}。

    直近 days 日に評価された記事を、記事(ジャンル+見出し)ごとに1件へまとめる。
    同じ記事に複数人の評価があれば多い方(同数なら一番新しい評価の方)。1人が複数回押した記録が
    残っていても、その人の一番新しい評価だけを数える。並びは新しい順、それぞれ最大 limit 件。"""
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
    rows = session.exec(select(NewsFeedback).where(
        NewsFeedback.updated_at >= now - timedelta(days=days)
    ).order_by(NewsFeedback.updated_at.desc(), NewsFeedback.id.desc())).all()

    # (genre, title) -> {user_id: (verdict, updated_at)}  ※新しい順に見るので最初の1件がその人の最新
    by_item: dict[tuple[str, str], dict[str, tuple[str, datetime]]] = {}
    for r in rows:
        if not r.title or r.digest_date == mockdata.MOCK_DATE:  # レイアウト確認用の架空記事は好みに入れない
            continue
        by_item.setdefault((r.genre, r.title), {}).setdefault(r.user_id, (r.verdict, r.updated_at))

    out: dict[str, list[dict]] = {"good": [], "bad": []}
    # dict は挿入順 = 各記事の一番新しい評価の新しい順
    for (genre, title), votes in by_item.items():
        good = sum(1 for v, _t in votes.values() if v == "good")
        bad = sum(1 for v, _t in votes.values() if v == "bad")
        if good != bad:
            verdict = "good" if good > bad else "bad"
        else:
            verdict = max(votes.values(), key=lambda vt: vt[1])[0]
        if verdict in out and len(out[verdict]) < limit:
            out[verdict].append({"genre": genre, "title": title})
    return out
