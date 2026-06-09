"""レイアウト確認用のモック(サンプル)ニュース。

実際の収集/キュレーション(twitterapi.io / Claude)を走らせずに配信レイアウトを確認する
ためのダミーデータ。LINEで「テスト」等を送ると、現在の配信レイアウトでこのサンプルが
返る(冒頭に「架空」警告つき)。API も時間も使わずレイアウト調整を回せる。

- 内容はすべて架空(事実ではない)。配信時は WARNING を必ず先頭に付ける。
- DB にはセンチネル日付 MOCK_DATE で格納し、実配信(当日分)と絶対に衝突させない。
"""

from __future__ import annotations

from datetime import date

from sqlmodel import Session

from . import digest
from .genres import display_genres
from .models import GenreDigest, NewsItem

# 実配信の当日分と衝突しないセンチネル(過去の固定日)
MOCK_DATE = date(2000, 1, 1)
MOCK_SLOT = "morning"

WARNING = (
    "⚠️ これはレイアウト確認用のサンプルです。\n"
    "実際のニュースではありません(内容はすべて架空)。"
)

# (genre, importance, title, summary) — 大ニュースは要約あり、小ニュースは見出しのみ
_MOCK: list[tuple[str, str, str, str]] = [
    ("特大", "big", "首都圏で大規模停電、約200万世帯に影響 復旧を急ぐ",
     "送電設備のトラブルが原因とみられ、鉄道や信号にも影響が出ている。電力会社は数時間以内の復旧を見込むと発表した。"),
    ("AI", "big", "OpenAIが新モデル「GPT-X」を発表 推論速度が従来比2倍に",
     "長文処理とコスト効率を大幅に改善したとされる。開発者向けAPIを今週から段階的に提供する。"),
    ("AI", "small", "国産LLM、医療分野での実証実験を開始", ""),
    ("AI", "small", "EUのAI規制法、最終調整の段階に", ""),
    ("株", "big", "日経平均、史上最高値を更新し4万円台後半に",
     "半導体関連と輸出株が指数を牽引した。海外勢の買いが続いているとの見方が強い。"),
    ("株", "small", "半導体株が軒並み上昇、関連ETFにも資金流入", ""),
    ("株", "small", "新NISA、口座開設数が前年比1.5倍に", ""),
    ("世界情勢", "big", "G7首脳会議、レアアース供給網の強化で合意",
     "特定国への依存度を下げるため、調達先の多角化と備蓄の協調を進める方針を確認した。"),
    ("世界情勢", "small", "中東情勢、停戦交渉が再開の見通し", ""),
    ("経済", "small", "円相場、一時1ドル=150円台に", ""),
    ("経済", "small", "日銀、政策金利を据え置き 物価動向を注視", ""),
    ("暗号資産", "small", "ビットコイン、最高値圏で推移", ""),
]


def _mock_genres() -> list[str]:
    """モックに含まれるジャンルを表示順(display_genres)で返す。"""
    seen = list(dict.fromkeys(g for g, *_ in _MOCK))  # 出現順で重複排除
    return display_genres(seen)


def seed(session: Session) -> int:
    """モックを DB に投入する(再実行で冪等: 同センチネルの既存を置き換える)。投入件数を返す。"""
    by_genre: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in _MOCK:
        by_genre.setdefault(row[0], []).append(row)

    n = 0
    for genre, rows in by_genre.items():
        existing = digest.get_genre_digest(session, genre, MOCK_DATE, MOCK_SLOT)
        if existing:
            for it in digest.items_of_digest(session, existing.id):
                session.delete(it)
            session.delete(existing)
            session.commit()
        gd = GenreDigest(digest_date=MOCK_DATE, slot=MOCK_SLOT, genre=genre)
        session.add(gd)
        session.commit()
        session.refresh(gd)
        for rank, (_g, importance, title, summary) in enumerate(rows):
            url = f"https://x.com/i/web/status/90000000{n:02d}"
            views = 90000 - n * 500
            session.add(NewsItem(
                genre_digest_id=gd.id, genre=genre, importance=importance, rank=rank,
                title=title, summary=summary, source_urls=[url],
                source_tweets=[{"text": title, "author": "sample_news", "url": url, "views": views}],
                top_view_count=views,
            ))
            n += 1
        session.commit()
    return n


def assemble(session: Session) -> dict[str, list[NewsItem]]:
    """モックを表示順で組み立てて返す(無ければ空)。"""
    return digest.assemble_for_genres(session, _mock_genres(), MOCK_DATE, MOCK_SLOT)


def get_or_seed(session: Session) -> dict[str, list[NewsItem]]:
    """モックを組み立てる。まだ無ければ自動投入してから返す(初回でも必ず出る)。"""
    grouped = assemble(session)
    if not any(grouped.values()):
        seed(session)
        grouped = assemble(session)
    return grouped
