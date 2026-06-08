"""ニュースキュレーション(Claude)。

生ツイート群を Claude が束ねて重複排除し、トピック化・重要度判定・見出し/要約を生成する。
流用元: XAgent/xagent/formatter.py の `_anthropic_complete` / complete 注入パターン。
LLM呼び出しは complete(system, user)->str に抽象化し、テストはフェイクを注入する。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings, get_settings

CompleteFn = Callable[[str, str], str]

_MAX_TOKENS = 2500
# Claude に渡すツイート数の上限(トークン/コスト対策。collect は viewCount 降順済み)
_CURATE_INPUT_LIMIT = 40
# 1ジャンルあたり「大ニュース」の最大件数
MAX_BIG_PER_GENRE = 3


@dataclass
class CuratedItem:
    title: str
    summary: str
    importance: str          # "big" | "small"
    score: int               # 0-100 (重要度の目安)
    source_idxs: list[int] = field(default_factory=list)


def _anthropic_complete(settings: Settings, system: str, user: str) -> str:
    import anthropic

    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY が未設定です。.env を設定してください。")
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    msg = client.messages.create(
        model=settings.claude_model,
        max_tokens=_MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(
        getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text"
    ).strip()


def _system_prompt(genre: str) -> str:
    return (
        f"あなたは日本語のニュース編集者です。X(旧Twitter)の「{genre}」ジャンルの投稿群から、"
        "その日のニュースを抽出して整理します。\n"
        "ルール:\n"
        "- 同じ話題(近い内容)は1件にまとめ、重複を排除する。\n"
        "- 各ニュースに importance を付ける。'big'=広く影響が大きい/速報級でインプレッションも高いもの。"
        f"それ以外は 'small'。**'big' は最大 {MAX_BIG_PER_GENRE} 件まで**。\n"
        "- title は日本語の短い見出し(40字以内)。summary は2〜3文の要約。\n"
        "- 投稿に書かれていない事実を創作しない。誇張しない。広告/個人の宣伝は除外する。\n"
        "- score は重要度の目安(0-100の整数)。\n"
        "- source_idxs は、そのニュースの根拠となった入力ツイートの番号(複数可)。\n"
        "出力は**JSON配列のみ**。前後に説明文やコードフェンスを付けない。\n"
        '形式: [{"title":"...","summary":"...","importance":"big|small","score":0,"source_idxs":[0,2]}]'
    )


def _format_tweets(tweets: list[dict]) -> str:
    lines = []
    for i, t in enumerate(tweets[:_CURATE_INPUT_LIMIT]):
        a = (t.get("author") or {}).get("userName", "?")
        text = " ".join((t.get("text") or "").split())
        if len(text) > 220:
            text = text[:220] + "…"
        v = t.get("viewCount") or 0
        like = t.get("likeCount") or 0
        lines.append(f"[{i}] (views={v} likes={like} @{a}) {text}")
    return "\n".join(lines)


def _extract_json_array(raw: str) -> list:
    """モデル出力から JSON 配列を取り出す。コードフェンスや前後ノイズを許容。"""
    s = raw.strip()
    if s.startswith("```"):
        # ```json ... ``` を剥がす
        s = s.split("```", 2)
        s = s[1] if len(s) >= 2 else raw
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError("JSON配列が見つかりません")
    return json.loads(s[start : end + 1])


def _coerce_items(data: list) -> list[CuratedItem]:
    items: list[CuratedItem] = []
    for d in data:
        if not isinstance(d, dict):
            continue
        title = str(d.get("title") or "").strip()
        if not title:
            continue
        importance = "big" if str(d.get("importance")).lower() == "big" else "small"
        try:
            score = int(d.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        idxs = [int(x) for x in (d.get("source_idxs") or []) if isinstance(x, (int, float))]
        items.append(
            CuratedItem(
                title=title[:80],
                summary=str(d.get("summary") or "").strip(),
                importance=importance,
                score=score,
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


class Curator:
    def __init__(self, settings: Settings | None = None, complete: CompleteFn | None = None) -> None:
        self.settings = settings or get_settings()
        self._complete = complete or (lambda s, u: _anthropic_complete(self.settings, s, u))

    def curate(self, genre: str, tweets: list[dict]) -> list[CuratedItem]:
        if not tweets:
            return []
        system = _system_prompt(genre)
        user = "次の投稿群を整理してください:\n" + _format_tweets(tweets)

        raw = self._complete(system, user)
        try:
            data = _extract_json_array(raw)
        except (ValueError, json.JSONDecodeError):
            # 1回だけ「JSONのみ」を強めて再試行
            raw = self._complete(
                system, user + "\n\n注意: JSON配列のみを出力。説明文やコードフェンスは禁止。"
            )
            try:
                data = _extract_json_array(raw)
            except (ValueError, json.JSONDecodeError):
                # 最終フォールバック: 機械的に小ニュース化(配信を止めない)
                return self._mechanical_fallback(tweets)

        items = _enforce_big_cap(_coerce_items(data))
        # 大ニュースを上に、同区分内は score 降順
        items.sort(key=lambda i: (i.importance != "big", -i.score))
        return items

    def _mechanical_fallback(self, tweets: list[dict]) -> list[CuratedItem]:
        out: list[CuratedItem] = []
        for i, t in enumerate(tweets[:10]):
            text = " ".join((t.get("text") or "").split())
            out.append(
                CuratedItem(
                    title=(text[:40] or "(本文なし)"),
                    summary=text[:160],
                    importance="big" if i == 0 else "small",
                    score=max(0, 100 - i * 5),
                    source_idxs=[i],
                )
            )
        return out
