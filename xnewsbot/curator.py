"""ニュースキュレーションの「契約」(指示文 + 入出力スキーマ + パース)。

方針変更: キュレーション(束ね・重複排除・重要度判定・見出し/要約)は **Claude Code(サブスク)
の定期実行** が行う。本モジュールは Anthropic API を呼ばない。代わりに:
- curation_instructions(): Claude Code が従う指示文(プロンプト)
- format_tweets_for_curation(): 収集ツイートを読みやすい入力に整形
- parse_curated(): Claude Code が出力した JSON/リストを CuratedItem に変換(重要度の上限・並び替え込み)
を提供する。これらは scripts/pipeline.py から使う。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# Claude に渡す(=ソースとして採番する)ツイート数の上限。source_idxs はこの範囲。
# 候補プールを広げ Claude がより多くの話題から選べるよう 40→60(2026-06-11 本人要望)。
CURATE_INPUT_LIMIT = 60
# 1ジャンルあたり「大ニュース」の最大件数
MAX_BIG_PER_GENRE = 3
# 本文の上限(安全網)。指示どおりの出力(summary 2〜3文 / detail 4〜8文)には当たらない値にし、
# 指示を外れた長文が来たときだけ切る。上限が無いと Flex がサイズ超過し push が丸ごと失敗する。
SUMMARY_MAX_CHARS = 500
DETAIL_MAX_CHARS = 2000


@dataclass
class CuratedItem:
    title: str
    summary: str
    importance: str          # "big" | "small"
    score: int               # 0-100 (重要度の目安)
    detail: str = ""         # 「詳細を見る」用の長め解説(背景・経緯。空なら表示時 summary で代替)
    genres: list[str] = field(default_factory=list)  # 該当ジャンルタグ(横断話題。例 ["経済","株"])
    source_idxs: list[int] = field(default_factory=list)


def curation_instructions(genre: str) -> str:
    """Claude Code がキュレーション時に従う指示文。"""
    return (
        f"あなたは日本語のニュース編集者です。X(旧Twitter)の「{genre}」ジャンルの投稿群から、"
        "その日のニュースを抽出して整理してください。\n"
        "ルール:\n"
        "- 同じ話題(近い内容)は1件にまとめ、重複を排除する。\n"
        "- 各ニュースに importance を付ける。'big'=広く影響が大きい/速報級でインプレッションも高いもの。"
        f"それ以外は 'small'。**'big' は最大 {MAX_BIG_PER_GENRE} 件まで**。\n"
        "- title は日本語の短い見出し(40字以内)。summary は2〜3文の簡潔な要約(一覧表示用)。\n"
        "- detail は「詳細を見る」で表示する長めの解説(4〜8文)。"
        "背景・経緯・なぜ重要か・関連する数字や反応など、投稿群から読み取れる内容を厚く書く。"
        "summary の言い換えで終わらせず、必ず一段詳しくする。\n"
        "- 投稿に書かれていない事実を創作しない。誇張しない。広告/個人の宣伝は除外する。\n"
        "- score は重要度の目安(0-100の整数)。\n"
        "- source_idxs は、そのニュースの根拠となった入力ツイートの番号(複数可)。\n"
        "出力は**JSON配列のみ**。\n"
        '形式: [{"title":"...","summary":"...","detail":"...","importance":"big|small","score":0,"source_idxs":[0,2]}]'
    )


def format_tweets_for_curation(tweets: list[dict]) -> str:
    """収集ツイートを採番付きの読みやすいテキストに整形する(source_idxs と対応)。"""
    lines = []
    for i, t in enumerate(tweets[:CURATE_INPUT_LIMIT]):
        a = (t.get("author") or {}).get("userName", "?")
        text = " ".join((t.get("text") or "").split())
        if len(text) > 220:
            text = text[:220] + "…"
        v = t.get("viewCount") or 0
        like = t.get("likeCount") or 0
        lines.append(f"[{i}] (views={v} likes={like} @{a}) {text}")
    return "\n".join(lines)


def _extract_json_array(raw: str) -> list:
    """文字列から JSON 配列を取り出す。コードフェンスや前後ノイズを許容。"""
    s = raw.strip()
    if s.startswith("```"):
        parts = s.split("```", 2)
        s = parts[1] if len(parts) >= 2 else raw
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("JSON配列が見つかりません")
    return json.loads(s[start : end + 1])


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _coerce_items(data: list) -> list[CuratedItem]:
    items: list[CuratedItem] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        title = str(d.get("title") or "").strip()
        if not title:
            continue
        importance = "big" if str(d.get("importance") or "").strip().lower() == "big" else "small"
        try:
            score = int(d.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        # source_idxs: bool は int だが添字として無効なので除外し、順序を保って重複も除く
        # (同じ元ツイートが詳細の「元ポスト」に二重表示されるのを防ぐ)。
        idxs: list[int] = []
        seen: set[int] = set()
        for x in (d.get("source_idxs") or []):
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                continue
            xi = int(x)
            if xi not in seen:
                seen.add(xi)
                idxs.append(xi)
        # 該当ジャンルタグ(横断重複排除で1件にまとめた話題の関連ジャンル)。文字列のみ・重複除去。
        genres: list[str] = []
        for g in (d.get("genres") or []):
            gs = str(g).strip()
            if gs and gs not in genres:
                genres.append(gs)
        items.append(
            CuratedItem(
                title=title[:80],
                summary=_clip(str(d.get("summary") or "").strip(), SUMMARY_MAX_CHARS),
                detail=_clip(str(d.get("detail") or "").strip(), DETAIL_MAX_CHARS),
                importance=importance,
                score=score,
                genres=genres,
                source_idxs=idxs,
            )
        )
    return items


def _enforce_big_cap(items: list[CuratedItem]) -> list[CuratedItem]:
    """'big' が上限を超えたら score の低いものから 'small' に降格。"""
    bigs = sorted([i for i in items if i.importance == "big"], key=lambda x: x.score, reverse=True)
    for extra in bigs[MAX_BIG_PER_GENRE:]:
        extra.importance = "small"
    return items


def parse_curated(data) -> list[CuratedItem]:
    """Claude Code の出力(JSON文字列 or 既にパースされたlist)を CuratedItem に変換。
    大ニュース上限を強制し、大→小・score降順で並べる。"""
    if isinstance(data, str):
        data = _extract_json_array(data)
    if not isinstance(data, list):
        raise ValueError("キュレーション結果は配列である必要があります")
    items = _enforce_big_cap(_coerce_items(data))
    items.sort(key=lambda i: (i.importance != "big", -i.score))
    return items
