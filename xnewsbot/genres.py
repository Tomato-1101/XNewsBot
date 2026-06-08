"""配信ジャンルの定義とXの検索キーワード群。

キーワードは初期値(調整可)。collect 時に "(kw1 OR kw2 ...) lang:ja" の OR 検索になる。
"""

from __future__ import annotations

# 表示順を保つため辞書(Python3.7+は挿入順を保持)
GENRES: dict[str, dict] = {
    "AI": {
        "label": "AI",
        "keywords": [
            "AI", "生成AI", "ChatGPT", "Claude", "LLM", "OpenAI",
            "Gemini", "人工知能", "Anthropic", "AIエージェント",
        ],
    },
    "株": {
        "label": "株",
        "keywords": [
            "株価", "日経平均", "決算", "米株", "S&P500", "半導体株",
            "東証", "個別株", "新NISA", "グロース株",
        ],
    },
    "経済": {
        "label": "経済",
        "keywords": [
            "経済", "円安", "円高", "為替", "金利", "インフレ",
            "日銀", "FRB", "GDP", "景気",
        ],
    },
    "政治": {
        "label": "政治",
        "keywords": [
            "政治", "選挙", "国会", "法案", "首相", "内閣",
            "外交", "増税", "政策", "与党",
        ],
    },
}

GENRE_KEYS: list[str] = list(GENRES.keys())


def is_valid_genre(name: str) -> bool:
    return name in GENRES


def keywords(genre: str) -> list[str]:
    return GENRES[genre]["keywords"]
