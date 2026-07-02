#!/usr/bin/env python3
"""速報リアルタイム監視 → LINEグループへ即 push(無料・Google ニュースRSS 主力 + GDELT 補助)。

launchd(com.tomato.xnewsbot-breaking)が15分毎に起動し1回実行する。X(twitterapi.io)は使わず
完全無料。定時ダイジェスト(ops/deliver.sh)とは独立した「速報だけ」の常時チャンネル。

流れ:
  1) 収集: 購読ジャンル + 常時(特大)のキーワードで Google ニュースRSS を直近1時間検索(無料)。
  2) 検出: 直近 lookback 分に公開 かつ 見出しに速報/注目マーカーを含むものを速報候補に(積極度=level)。
  3) 重複排除: 既送(SQLite breaking_sent)と照合し未送のみ。実行内の同一話題もまとめる。
  4) レート制限: 1日 max_per_day 件まで(グループを荒らさない/LINE無料枠200通/月を守る)。
  5) 配信: 「今のグループ」(DB subscriber.push_to のグループ)へ text で push。1件=1通。

安全策:
  --dry-run     送信せず検出結果だけ表示(既送記録もしない)。
  --once        既定(1回実行して終了。launchd がスケジュールする)。
  乱造防止 = 重複排除 + 日次上限 + 鮮度窓 の3重。API大量消費・大量送信は構造的に起きない。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import select  # noqa: E402

from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot import newsfeeds as nf  # noqa: E402
from xnewsbot.config import get_settings  # noqa: E402
from xnewsbot.db import get_session, init_db  # noqa: E402
from xnewsbot.genres import ALWAYS_KEYS, GENRE_KEYS, keywords  # noqa: E402
from xnewsbot.models import Subscriber  # noqa: E402

# 見出しに含まれれば「速報級」とみなす強マーカー(strict でも通す)。
STRONG_MARKERS = [
    "速報", "緊急", "号外", "地震", "震度", "津波", "噴火", "火災", "爆発", "テロ",
    "墜落", "衝突", "崩落", "死去", "訃報", "停電", "リコール", "為替介入", "避難指示",
]
# medium で追加する「大きめニュース」マーカー(影響の大きい決定・相場急変・重大人事など)。
# ※ 発表/就任/受賞/リリース/開始 等の汎用語は PR・スポーツ・芸能の軽い話題を大量に拾うため入れない。
NOTABLE_MARKERS = [
    "決定", "合意", "可決", "成立", "過去最高", "過去最大", "最高値", "最安値",
    "急騰", "急落", "暴落", "買収", "辞任", "解任", "逮捕", "起訴", "撤退",
    "破綻", "値上げ", "利上げ", "利下げ", "経営破綻", "全面戦争", "空爆", "制裁",
]

MAX_QUERY_TERMS = 6  # 1ジャンルの検索クエリに使うキーワード数(OR)。長すぎると焦点がぼける。


def norm_key(title: str) -> str:
    """見出しを正規化して重複判定キー(sha1)にする。記号・空白を除いた本文で同一話題をまとめる。"""
    t = re.sub(r"[\s\W_]+", "", title.lower())
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def markers_for(level: str) -> list[str] | None:
    """積極度に応じた採用マーカー。broad は None(マーカー不問=鮮度のみで採用)。"""
    if level == "broad":
        return None
    if level == "strict":
        return STRONG_MARKERS
    return STRONG_MARKERS + NOTABLE_MARKERS  # medium(既定)


def is_breaking(title: str, markers: list[str] | None) -> bool:
    if markers is None:
        return True
    return any(m in title for m in markers)


def select_breaking(candidates, *, level, lookback_min, now, seen_keys):
    """(genre, FeedItem) の候補列から、鮮度・マーカー・重複排除を通ったものを新しい順で返す。

    ネットワーク非依存の純関数(テスト対象)。seen_keys は既送 + 実行内で採用済みのキー集合。
    """
    markers = markers_for(level)
    cutoff = now - timedelta(minutes=lookback_min)
    picked = []
    local_seen = set(seen_keys)
    for genre, item in candidates:
        if item.published is None or item.published < cutoff:
            continue
        if not is_breaking(item.title, markers):
            continue
        key = norm_key(item.title)
        if key in local_seen:
            continue
        local_seen.add(key)
        picked.append((genre, item, key))
    picked.sort(key=lambda gi: gi[1].published, reverse=True)
    return picked


# ---- 既送ストア(xnewsbot.db 内の breaking_sent テーブルを raw sqlite で自己管理) ----

def _store(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE IF NOT EXISTS breaking_sent("
        "key TEXT PRIMARY KEY, title TEXT, url TEXT, genre TEXT, sent_at TEXT)")
    con.commit()
    return con


def _seen_keys(con: sqlite3.Connection, since: datetime) -> set[str]:
    """直近(送信済みの重複判定に十分な期間)の既送キー集合。"""
    rows = con.execute("SELECT key FROM breaking_sent WHERE sent_at >= ?",
                       (since.isoformat(),)).fetchall()
    return {r[0] for r in rows}


def _sent_today(con: sqlite3.Connection, day_start: datetime) -> int:
    return con.execute("SELECT COUNT(*) FROM breaking_sent WHERE sent_at >= ?",
                      (day_start.isoformat(),)).fetchone()[0]


def _record(con: sqlite3.Connection, key, item, genre, when) -> None:
    con.execute("INSERT OR REPLACE INTO breaking_sent(key,title,url,genre,sent_at) VALUES(?,?,?,?,?)",
               (key, item.title, item.url, genre, when.isoformat()))
    con.commit()


# ---- 配信先(今のグループ)の解決 ----

def resolve_group(settings) -> str | None:
    """速報の配信先グループIDを解決。設定 > DBの push_to(グループ=今のグループ) の順。"""
    if settings.breaking_group_id:
        return settings.breaking_group_id
    init_db()
    with get_session() as session:
        subs = session.exec(select(Subscriber)).all()
        for s in subs:
            pt = s.push_to or ""
            if pt.startswith(("C", "R")):  # C=グループ, R=複数人トーク
                return pt
    return None


def target_genres(settings) -> list[str]:
    """監視対象ジャンル = オンボード済み購読者の有効ジャンルの和集合(表示順)。

    「特大」など常時ジャンルの汎用キーワード(速報/ニュース/発表/会見)は何でも拾い
    ノイズ源になるため、監視ではトピックの明確な選択ジャンルだけを対象にする。
    購読者未登録時は全選択ジャンルにフォールバック(常時ジャンルは除く)。
    """
    init_db()
    seen: set[str] = set()
    with get_session() as session:
        subs = session.exec(
            select(Subscriber).where(Subscriber.is_onboarded == True)).all()  # noqa: E712
        for s in subs:
            seen.update(s.enabled_genres)
    seen -= set(ALWAYS_KEYS)
    ordered = [g for g in GENRE_KEYS if g in seen]
    return ordered or [g for g in GENRE_KEYS if g not in ALWAYS_KEYS]


def collect_candidates(genres: list[str]) -> list[tuple[str, "nf.FeedItem"]]:
    """各ジャンルのキーワードで Google ニュースRSS(直近1h)を検索し (genre, FeedItem) 列にする。

    Google ニュースは関連の薄い記事も返すため、見出しにそのジャンルのキーワードを
    実際に含むものだけ残す(トピック整合=誤爆・ノイズを減らす)。
    """
    out: list[tuple[str, nf.FeedItem]] = []
    for g in genres:
        kws = keywords(g)
        query_terms = kws[:MAX_QUERY_TERMS]
        query = "(" + " OR ".join(query_terms) + ")" if len(query_terms) > 1 else query_terms[0]
        for item in nf.google_news(query, within_hours=1):
            if any(k in item.title for k in kws):  # トピック整合(見出しにジャンル語を含む)
                out.append((g, item))
    return out


def _age(published: datetime, now: datetime) -> str:
    mins = int((now - published).total_seconds() // 60)
    if mins < 1:
        return "たった今"
    if mins < 60:
        return f"{mins}分前"
    return f"{mins // 60}時間前"


def _push_spec(genre: str, item, now) -> dict:
    body = f"🚨 速報・{genre}\n{item.title}\n{item.source}・{_age(item.published, now)}\n{item.url}"
    return lc.text_spec(body)


def run(dry_run: bool = False) -> int:
    settings = get_settings()
    if not settings.breaking_enabled:
        print("[breaking] 無効化されています(breaking_enabled=False)。", file=sys.stderr)
        return 0
    now = datetime.now(timezone.utc)
    tz = ZoneInfo(settings.default_tz)
    day_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    group = resolve_group(settings)
    if group is None and not dry_run:
        print("[breaking] 配信先グループが未解決(push_to にグループ未登録)。dry-run 相当で終了。",
              file=sys.stderr)
        dry_run = True

    genres = target_genres(settings)
    candidates = collect_candidates(genres)
    print(f"[breaking] 収集: {len(genres)}ジャンル / 候補 {len(candidates)} 件", file=sys.stderr)

    con = _store(settings.db_path)
    seen = _seen_keys(con, now - timedelta(hours=48))
    sent_today = _sent_today(con, day_start)
    remaining = max(0, settings.breaking_max_per_day - sent_today)

    picked = select_breaking(candidates, level=settings.breaking_level,
                             lookback_min=settings.breaking_lookback_min, now=now, seen_keys=seen)
    print(f"[breaking] 速報候補 {len(picked)} 件 / 本日送信済 {sent_today} / 残り枠 {remaining}",
          file=sys.stderr)

    if not picked:
        return 0

    messenger = None
    if not dry_run:
        if not settings.line_channel_access_token:
            print("[breaking] LINE トークン未設定。送信不可。", file=sys.stderr)
            return 1
        messenger = lc.LineMessenger(settings.line_channel_access_token)

    sent = 0
    for genre, item, key in picked:
        if sent >= remaining:
            print(f"[breaking] 日次上限({settings.breaking_max_per_day})に達したため打ち切り。", file=sys.stderr)
            break
        line = f"[{genre}] {item.title[:50]} ({item.source})"
        if dry_run:
            print(f"[breaking][DRY] would push: {line}", file=sys.stderr)
            continue
        try:
            messenger.push(group, [_push_spec(genre, item, now)])
        except Exception as e:  # 1件の失敗で全体を止めない
            print(f"[breaking] push 失敗: {e} ({line})", file=sys.stderr)
            continue
        _record(con, key, item, genre, now)
        sent += 1
        print(f"[breaking] push: {line}", file=sys.stderr)

    con.close()
    print(f"[breaking] 完了: {sent} 件送信 (dry_run={dry_run})", file=sys.stderr)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="XNewsBot 速報監視→LINEグループへ即push")
    ap.add_argument("--dry-run", action="store_true", help="送信せず検出結果だけ表示(記録もしない)")
    ap.add_argument("--once", action="store_true", help="1回実行して終了(既定・launchd用)")
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
