"""Python 実装の出力を golden.json に書き出す(TS 側 test/golden.test.ts が完全一致を確かめる)。

実行: リポジトリの venv の python で `python cloud/worker/golden/make_golden.py`。
spec 生成(xnewsbot/line_client.py)や文言を変えたら作り直してコミットする。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from xnewsbot import explain, feedback, onboarding  # noqa: E402
from xnewsbot import line_client as lc  # noqa: E402
from xnewsbot.genres import GENRE_KEYS, GENRES, SELECTABLE_KEYS  # noqa: E402
from xnewsbot.models import NewsItem, Subscriber  # noqa: E402

cases: list[dict] = []


def add(fn: str, args: dict, out) -> None:
    cases.append({"fn": fn, "args": args, "out": out})


QR = [{"label": "a", "data": "b"}]
for text, q in (("こんにちは", None), ("ボタンつき", QR), ("", [])):
    add("text_spec", {"text": text, "quick_reply": q}, lc.text_spec(text, q))

for label in ("短い", "あ" * 25, "😀" * 25, "a" * 20, "a" * 21):
    add("_qr", {"label": label, "data": "x:1"}, lc._qr(label, "x:1"))

for sel in ([], ["AI", "株"], ["株", "AI", "存在しない"], list(SELECTABLE_KEYS), ["暗号資産"]):
    add("genre_select_spec", {"selected": sel}, lc.genre_select_spec(sel))

for slot in ("morning", "evening", "other"):
    add("time_select_spec", {"slot": slot}, lc.time_select_spec(slot))

add("menu_quick_reply", {}, lc.menu_quick_reply())
add("menu_spec", {"text": "メニュー"}, lc.menu_spec())
add("menu_spec", {"text": "メニューです。操作を選んでください。"}, lc.menu_spec("メニューです。操作を選んでください。"))
add("_help_spec", {}, onboarding._help_spec())

SUBS = [
    {},
    {"enabled_genres": ["AI", "株"], "morning_hour": 7, "morning_minute": 5, "evening_hour": 0, "evening_minute": 0},
    {"enabled_genres": ["ロボティクス"], "morning_enabled": False},
    {"enabled_genres": ["AI"], "evening_enabled": False, "morning_hour": 23, "morning_minute": 59},
]
for s in SUBS:
    sub = Subscriber(line_user_id="U1", **s)
    full = {k: getattr(sub, k) for k in ("enabled_genres", "morning_hour", "morning_minute", "evening_hour",
                                         "evening_minute", "morning_enabled", "evening_enabled")}
    add("settings_summary_text", {"sub": full}, lc.settings_summary_text(sub))

SOURCES = [
    {"media": "架空新聞", "author": "x", "url": "https://example.com/a", "kind": "news"},
    {"author": "someone", "url": "https://x.com/someone/status/1", "kind": "x"},
    {"author": "架空通信", "url": "https://example.com/b", "kind": "news"},
    {"author": "?", "url": "https://example.com/c"},
    {"author": "", "url": ""},
    {"author": "old", "text": "本文"},
    {"media": 123},
    {},
]
for s in SOURCES:
    add("_source_name", {"s": s}, lc._source_name(s))

ITEMS = [
    {"id": 1, "genre": "AI", "genres": [], "title": "見出しA", "summary": "要約A", "detail": "", "source_urls": [],
     "source_tweets": []},
    {"id": 2, "genre": "暗号資産", "genres": ["暗号資産", "株", "廃止ジャンル"], "title": "見出しB", "summary": "要約B",
     "detail": "長めの解説B\n2行目", "source_urls": ["https://example.com/u1"], "source_tweets": SOURCES[:5]},
    {"id": 3, "genre": "特大", "genres": [], "title": "見出しC", "summary": "", "detail": "",
     "source_urls": ["https://example.com/1", "", "https://example.com/3", "https://example.com/4"],
     "source_tweets": []},
    {"id": 4, "genre": "ロボティクス", "genres": ["ロボティクス"], "title": "長い" * 50, "summary": "あ" * 6000, "detail": "",
     "source_urls": [], "source_tweets": SOURCES[3:8]},
    {"id": 5, "genre": "株", "genres": ["株"], "title": "絵文字😀の記事", "summary": "", "detail": "😀" * 4900,
     "source_urls": [], "source_tweets": []},
    # 切り詰め境界: ちょうど上限(4800字)に収まる本文と、1字だけ超える本文
    {"id": 6, "genre": "AI", "genres": [], "title": "T", "summary": "", "detail": "あ" * (4800 - len("【AI】T\n\n")),
     "source_urls": [], "source_tweets": []},
    {"id": 7, "genre": "AI", "genres": [], "title": "T", "summary": "", "detail": "あ" * (4801 - len("【AI】T\n\n")),
     "source_urls": [], "source_tweets": []},
]
PAYLOADS = [None, "123", "20261119:morning:AI:3", " 20261119:morning:暗号資産:0", "a:b:c"]
for it in ITEMS:
    item = NewsItem(genre_digest_id=1, **it)
    add("_tag_labels", {"item": it}, lc._tag_labels(item))
    add("_tag_text", {"item": it}, lc._tag_text(item))
    for p in PAYLOADS:
        add("detail_spec", {"item": it, "payload": p}, lc.detail_spec(item, p))

add("feedback_quick_reply", {"key": "20261119:morning:AI:abcd1234"}, lc.feedback_quick_reply("20261119:morning:AI:abcd1234"))
for title in ("見出し", "長" * 500, "😀" * 450):
    add("feedback_spec", {"title": title, "key": "20261119:morning:AI:abcd1234"},
        lc.feedback_spec(title, "20261119:morning:AI:abcd1234"))

for title in ("", "見出しA", "絵文字😀の記事", "a b\nc"):
    add("title_hash", {"title": title}, feedback.title_hash(title))

add("line_text", {"title": "見出し", "text": "【何が起きたか】\n本文"}, explain.line_text("見出し", "【何が起きたか】\n本文"))

for text in ("7:30", "7：30", "7時30分", "7時", "0730", "７：３０", " 23:59 ", "24:00", "7:60", "730", "abc", "",
             "12時5分", "9 : 05", "0000", "2400", "1:2:3", "7時30", "07301"):
    t = onboarding.parse_time(text)
    add("parse_time", {"text": text}, list(t) if t else None)

# 送信時の変換(spec → LINE のメッセージ)は SDK のモデルを通した結果と比べる
for spec in (lc.text_spec("素のテキスト"), lc.menu_spec(), lc.genre_select_spec(["AI"]),
             lc.feedback_spec("見出し", "20261119:morning:AI:abcd1234")):
    add("to_message", {"spec": spec}, lc._spec_to_message(spec).to_dict())

out = {
    "genres": {"keys": GENRE_KEYS, "labels": {k: GENRES[k]["label"] for k in GENRE_KEYS},
               "selectable": SELECTABLE_KEYS},
    "cases": cases,
}
Path(__file__).with_name("golden.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"{len(cases)} 件を書き出しました")
