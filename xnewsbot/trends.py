"""「話題」ジャンル用: 急上昇ワード・人気エントリーを無料の公開ページから集めて候補にする。APIキー不要。

X や世の中で大きく話題になっていることを拾うため(本人要望 2026-10-01)。候補の形は
newsfeeds.as_candidate と同じ(pipeline.merge_news でニュース候補とそのまま混ぜる)で、
どれだけ話題かを `trend` キーに添える(キュレーションが話題の大きさを判断する材料):
  {"source": "yahoo_realtime", "word", "posts", "related": [関連語], "headlines": [見出し]}
  {"source": "google_trends", "word", "traffic", "headlines": [見出し]}
  {"source": "hatena", "bookmarks"}

取得元(どれも鍵なし。2026-10-01 に実ページを1回ずつ取得して構造を確認):
- Yahoo!リアルタイム検索のトレンド(HTML 内 __NEXT_DATA__ の pageData.buzzTrend.items):
  X 由来の語と投稿数。語だけでは何が起きたか分からないので、上位の語ごとに Google ニュースの
  見出しを引いて候補にする(見出しが無い語は候補にしない)。
- Google トレンド急上昇 RSS(関連ニュースつき): 関連ニュースの先頭を候補にする。
- はてなブックマーク人気エントリー RSS(RDF・ブクマ数つき): ブクマ数の多い順。
- 新しい AI モデルの公開(AI ジャンル用。2026-10-02 追加。マイナーなものまで拾うため):
  OpenRouter の新規掲載モデル(API・鍵なし)と、Hugging Face の組織ウォッチ+急上昇の新規モデル(API・鍵なし)。
  HF の「新規」は createdAt(リポジトリ作成時刻)ではなく、このプログラムが初めて見た時刻で判定する
  (非公開で作ってから公開したモデルは createdAt が古いため。状態は DB と同じ場所の seen_models.json)。
  候補は {"source": "openrouter_models", "id", "output", "context_length"} /
  {"source": "hf_models", "id", "pipeline_tag", "likes", "downloads"[, "trending"]} を `trend` に添える。
取得元ごとに失敗しても空を返す(stderr に1行)。配信は止めない。
"""

from __future__ import annotations

import gzip
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import newsfeeds
from .config import get_settings
from .newsfeeds import FeedItem

YAHOO_RT_URL = "https://search.yahoo.co.jp/realtime/search/trend"
GOOGLE_TRENDS_URL = "https://trends.google.com/trending/rss?geo=JP"
HATENA_URL = "https://b.hatena.ne.jp/hotentry.rss"

YAHOO_WORDS = 10        # Yahoo のトレンド語は上位この数まで(1語ごとに Google ニュースを1回引く)
HEADLINES = 2           # Yahoo の1語あたりの見出し数
GOOGLE_TRENDS_MAX = 15
GT_HEADLINES = 3        # Google トレンドの1語あたりに添える見出し数
HATENA_MAX = 15

OPENROUTER_URL = "https://openrouter.ai/api/v1/models?output_modalities=all"
OPENROUTER_TIMEOUT = 40   # 応答は約1MB。通常は1秒未満だが、遅い時のために長めに取る
OPENROUTER_MAX = 30
OPENROUTER_FLOOR_H = 48   # 配信は24h間隔・掲載時刻のずれと取りこぼし対策で、収集窓が短くても最低これだけ遡る
HF_API = "https://huggingface.co/api/models"
HF_ORG_LIMIT = 20         # 組織ごとに新しい順にこの件数まで見る
HF_ORG_FLOOR_H = 48
HF_TREND_LIMIT = 500      # 急上昇の上位この数から、新しくて伸びているものを拾う
HF_TREND_WINDOW_H = 72    # 公開から時間が経っても伸びるのは数日かかるので、組織ウォッチより長く見る
HF_TREND_MIN_SCORE = 20
HF_SEEN_MAX_AGE_D = 30    # createdAt がこれより古いものは「初めて見た」でも新規扱いしない(緩い上限)
HF_SEEN_KEEP_D = 60       # 初めて見た時刻の記録をこの日数で捨てる(HF_SEEN_MAX_AGE_D より長く持つ)
HF_NO_DL_AGE_D = 3        # ダウンロード0のまま作成からこの日数を超えたものは水増し・放置とみなして出さない
HF_README_CHARS = 350     # 説明文(README 冒頭)の長さ
HF_README_MAX_BYTES = 30000
HF_README_BUDGET_S = 10   # README の取得全体をこの秒数で打ち切る(取れた分だけ説明を付ける)
HF_TIMEOUT = 8            # 1リクエストの上限
HF_BUDGET_S = 10          # 全組織の取得をこの秒数で打ち切る(取れた分だけで続ける)
HF_WORKERS = 6
HF_MAX = 30
HF_TREND_EXPAND = "&".join(f"expand[]={f}" for f in
                           ("createdAt", "likes", "downloads", "trendingScore", "pipeline_tag"))

# 監視する Hugging Face の組織(中国・欧米の主要ラボ、音声・画像・動画、日本の国内モデル)
HF_ORGS = [
    "Qwen", "deepseek-ai", "moonshotai", "zai-org", "MiniMaxAI", "stepfun-ai", "baidu", "tencent",
    "ByteDance-Seed", "XiaomiMiMo", "inclusionAI", "meituan-longcat", "openai", "google", "microsoft",
    "facebook", "mistralai", "nvidia", "apple", "amazon", "ibm-granite", "allenai", "black-forest-labs",
    "stabilityai", "Lightricks", "Wan-AI", "kyutai", "sesame", "cartesia", "fishaudio", "ResembleAI",
    "nari-labs", "FunAudioLLM", "SakanaAI", "pfnet", "elyza", "sbintuitions", "llm-jp", "cyberagent",
    "tokyotech-llm", "LGAI-EXAONE", "openbmb", "CohereLabs",
]
# 量子化・変換の再配布をするだけの投稿者(新モデルの公開ではない)
HF_REDISTRIBUTORS = {a.lower() for a in (
    "unsloth", "bartowski", "mlx-community", "lmstudio-community", "TheBloke", "QuantFactory",
    "mradermacher", "MaziyarPanahi", "second-state", "ggml-org", "DavidAU")}
# モデル名にこの語(英数字の区切りで分けた1語)が入っていたら量子化・LoRA の派生とみなす
_HF_DERIVED_WORDS = {"gguf", "awq", "gptq", "mlx", "exl2", "fp8", "nvfp4", "int4", "int8", "bnb", "lora"}
_HF_BITS = re.compile(r"^\d+bit$")

# 同じ UA・タイムアウト・失敗時 None(newsfeeds と同じ取り方)
_get = newsfeeds._get

_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)
_HT = "{https://trends.google.com/trending/rss}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_HATENA = "{http://www.hatena.ne.jp/info/xmlns#}"


def _need(url: str) -> bytes:
    body = _get(url)
    if not body:
        raise ConnectionError(f"取得できませんでした: {url}")
    return body


def _cutoff(hours: float | None) -> datetime | None:
    return datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None


def _candidate(item: FeedItem, trend: dict) -> dict:
    c = newsfeeds.as_candidate(item)
    c["trend"] = trend
    return c


# --- Yahoo!リアルタイム検索 ---

def parse_yahoo_realtime(body: bytes) -> list[dict]:
    """トレンドページ → [{"word", "posts", "related"}](ページの順=トレンドの順)。"""
    m = _NEXT_DATA.search(body.decode("utf-8", "replace"))
    if not m:
        return []
    data = json.loads(m.group(1))
    items = (((data.get("props") or {}).get("pageProps") or {}).get("pageData") or {}) \
        .get("buzzTrend", {}).get("items") or []
    out: list[dict] = []
    for it in items:
        word = " ".join(str(it.get("query") or "").split())
        if word:
            out.append({"word": word, "posts": int(it.get("tweetCount") or 0),
                        "related": [str(x) for x in it.get("childBuzz") or []]})
    return out


def yahoo_realtime(hours: float | None = 24) -> list[dict]:
    words = parse_yahoo_realtime(_need(YAHOO_RT_URL))[:YAHOO_WORDS]
    with ThreadPoolExecutor(max_workers=5) as pool:
        heads = list(pool.map(
            lambda w: newsfeeds.google_news(w["word"], within_hours=hours)[:HEADLINES], words))
    return [_candidate(hs[0], {"source": "yahoo_realtime", "word": w["word"], "posts": w["posts"],
                               "related": w["related"], "headlines": [h.title for h in hs]})
            for w, hs in zip(words, heads) if hs]


# --- Google トレンド ---

def parse_google_trends(body: bytes) -> list[dict]:
    """急上昇 RSS → [{"word", "traffic", "published", "news": [FeedItem]}]。"""
    root = ET.fromstring(body)
    out: list[dict] = []
    for item in root.iter("item"):
        word = (item.findtext("title") or "").strip()
        pub = newsfeeds._parse_date_any(item.findtext("pubDate"))
        news = []
        for n in item.findall(f"{_HT}news_item"):
            title = newsfeeds.strip_html(n.findtext(f"{_HT}news_item_title"), limit=500)
            url = (n.findtext(f"{_HT}news_item_url") or "").strip()
            if title and url:
                news.append(FeedItem(title=title, url=url, published=pub, origin="feed",
                                     source=(n.findtext(f"{_HT}news_item_source") or "").strip()
                                     or "Google トレンド"))
        if word:
            out.append({"word": word, "traffic": (item.findtext(f"{_HT}approx_traffic") or "").strip(),
                        "published": pub, "news": news})
    return out


def google_trends(hours: float | None = 24) -> list[dict]:
    cut = _cutoff(hours)
    out: list[dict] = []
    for t in parse_google_trends(_need(GOOGLE_TRENDS_URL)):
        if not t["news"] or (cut and t["published"] and t["published"] < cut):
            continue
        out.append(_candidate(t["news"][0], {
            "source": "google_trends", "word": t["word"], "traffic": t["traffic"],
            "headlines": [n.title for n in t["news"][:GT_HEADLINES]]}))
    return out[:GOOGLE_TRENDS_MAX]


# --- はてなブックマーク ---

def parse_hatena(body: bytes) -> list[tuple[FeedItem, int]]:
    """人気エントリー RSS(RDF) → [(FeedItem, ブクマ数)]。媒体名は記事のドメイン。"""
    root = ET.fromstring(body)
    out: list[tuple[FeedItem, int]] = []
    for item in root.iter(f"{_RSS1}item"):
        # 見出しは実体参照が二重のことがある(&amp;#39; 等)。strip_html が unescape する
        title = newsfeeds.strip_html(item.findtext(f"{_RSS1}title"), limit=500)
        url = (item.findtext(f"{_RSS1}link") or "").strip()
        if not title or not url:
            continue
        host = urllib.parse.urlsplit(url).hostname or "はてなブックマーク"
        try:
            n = int(item.findtext(f"{_HATENA}bookmarkcount") or 0)
        except ValueError:
            n = 0
        out.append((FeedItem(title=title, url=url, source=host.removeprefix("www."),
                             published=newsfeeds._parse_date_any(item.findtext(f"{_DC}date")),
                             origin="feed", summary=newsfeeds.strip_html(item.findtext(f"{_RSS1}description"))),
                    n))
    return out


def hatena(hours: float | None = 24) -> list[dict]:
    cut = _cutoff(hours)
    rows = [(it, n) for it, n in parse_hatena(_need(HATENA_URL))
            if not cut or (it.published is not None and it.published >= cut)]
    rows.sort(key=lambda r: -r[1])
    return [_candidate(it, {"source": "hatena", "bookmarks": n}) for it, n in rows[:HATENA_MAX]]


# --- 新しい AI モデルの公開(OpenRouter / Hugging Face) ---

def _get_json(url: str, timeout: float):
    """JSON API を GET する(newsfeeds._get は20秒固定なので、取得元ごとの上限を持てるよう別に持つ)。"""
    req = urllib.request.Request(url, headers={"User-Agent": newsfeeds._UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data)


def _get_text(url: str, timeout: float, max_bytes: int) -> str:
    """テキストを先頭 max_bytes だけ GET する(README 用。巨大ファイルを全部読まない)。"""
    req = urllib.request.Request(url, headers={"User-Agent": newsfeeds._UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes).decode("utf-8", "replace")


def _fetch_all(fn, items: list, workers: int, budget_s: float) -> list:
    """fn(item) を並列に呼び、結果を入力順の [("ok", 値) | ("err", 例外) | None(時間切れ)] で返す。

    daemon スレッドで動かす(articles.enrich_bodies と同じ): ThreadPoolExecutor のワーカーは Python 終了時に
    join されるため、budget で戻っても取得中の1件が終わるまでプロセスが終わらず、配信が遅れる。
    budget を過ぎたら残りは待たず、まだ始めていない分は始めない。"""
    todo: queue.Queue[int] = queue.Queue()
    for i in range(len(items)):
        todo.put(i)
    results: queue.Queue[tuple[int, tuple]] = queue.Queue()
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                i = todo.get_nowait()
            except queue.Empty:
                return
            try:
                r = ("ok", fn(items[i]))
            except Exception as e:
                r = ("err", e)
            results.put((i, r))

    for _ in range(min(workers, len(items))):
        threading.Thread(target=worker, daemon=True).start()
    out: list = [None] * len(items)
    deadline = time.monotonic() + budget_s
    got = 0
    while got < len(items):
        try:
            i, r = results.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        out[i] = r
        got += 1
    stop.set()
    return out


def _ts(unix: float | int | None) -> datetime | None:
    return datetime.fromtimestamp(unix, tz=timezone.utc) if unix else None


def _iso(raw: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_openrouter(data: dict, hours: float) -> list[dict]:
    """OpenRouter のモデル一覧 → 直近 hours 時間に掲載された新モデル(新しい順)。

    id に ":"(:free / :batch 等)が付くのは既存モデルの派生なので落とす。ただし本体の id が一覧に
    無いとき(無料版だけで公開された新モデル)は、派生を本体の1件として残す(取りこぼし防止)。
    """
    models = data.get("data") or []
    ids = {m.get("id") for m in models}
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    seen: set[str] = set()
    out: list[dict] = []
    for m in sorted(models, key=lambda m: -(m.get("created") or 0)):
        mid = str(m.get("id") or "")
        created = _ts(m.get("created"))
        if not mid or created is None or created < cut:
            continue
        base = mid.split(":", 1)[0]
        if ":" in mid and base in ids:
            continue
        if base in seen:
            continue
        seen.add(base)
        name = re.sub(r"\s*\((free|beta|extended|batch)\)\s*$", "", str(m.get("name") or base), flags=re.I)
        arch = m.get("architecture") or {}
        out.append({"id": base, "name": name, "created": created,
                    "description": newsfeeds.strip_html(m.get("description"), limit=200),
                    "output": [str(x) for x in arch.get("output_modalities") or []],
                    "context_length": int(m.get("context_length") or 0)})
    return out


def openrouter_models(hours: float | None = 24) -> list[dict]:
    window = max(hours or 0, OPENROUTER_FLOOR_H)
    rows = parse_openrouter(_get_json(OPENROUTER_URL, OPENROUTER_TIMEOUT), window)[:OPENROUTER_MAX]
    out = []
    for r in rows:
        parts = [f"OpenRouter に新規掲載({r['created']:%Y-%m-%d %H:%M} UTC)"]
        if r["output"]:
            parts.append("出力: " + "・".join(r["output"]))
        if r["context_length"]:
            parts.append(f"コンテキスト長 {r['context_length']:,}")
        summary = "。".join(parts) + "。" + r["description"]
        item = FeedItem(title=r["name"], url=f"https://openrouter.ai/{r['id']}",
                        source=r["id"].split("/", 1)[0], published=r["created"], origin="feed",
                        summary=summary)
        out.append(_candidate(item, {"source": "openrouter_models", "id": r["id"],
                                     "output": r["output"], "context_length": r["context_length"]}))
    return out


def _is_derived_hf(model_id: str) -> bool:
    """量子化・変換の再配布や LoRA など、新モデルの公開ではないもの。"""
    author, _, name = model_id.partition("/")
    if author.lower() in HF_REDISTRIBUTORS:
        return True
    words = re.split(r"[^a-z0-9]+", name.lower())
    return any(w in _HF_DERIVED_WORDS or _HF_BITS.match(w) for w in words)


def parse_hf_models(rows: list[dict], hours: float, min_trending: float | None = None) -> list[dict]:
    """HF のモデル一覧 → createdAt が直近 hours 時間以内のモデル(緩い上限。「新規」の判定は hf_models が
    初めて見た時刻で行う)。min_trending を渡すと trendingScore がそれ以上のものだけ。量子化・LoRA の派生は落とす。"""
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for m in rows if isinstance(rows, list) else []:
        mid = str(m.get("id") or "")
        created = _iso(m.get("createdAt"))
        if not mid or created is None or created < cut or _is_derived_hf(mid):
            continue
        score = float(m.get("trendingScore") or 0)
        if min_trending is not None and score < min_trending:
            continue
        out.append({"id": mid, "created": created, "pipeline_tag": str(m.get("pipeline_tag") or ""),
                    "likes": int(m.get("likes") or 0), "downloads": int(m.get("downloads") or 0),
                    "trending": score})
    return out


def _seen_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(get_settings().db_path)), "seen_models.json")


def _load_seen() -> tuple[dict[str, datetime], bool]:
    """初めて見た時刻の記録 {model_id: first_seen} と、状態ファイルが既にあったか。
    読めない・壊れているときは stderr に1行出して「状態なし」で続ける(収集は止めない)。"""
    try:
        path = _seen_path()
        if not os.path.exists(path):
            return {}, False
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        seen = {str(k): dt for k, v in raw.items() if (dt := _iso(v)) is not None}
        return seen, True
    except Exception as e:
        print(f"  話題: seen_models.json を読めず状態なしで続行 ({type(e).__name__}: {e})", file=sys.stderr)
        return {}, False


def _save_seen(seen: dict[str, datetime]) -> None:
    """HF_SEEN_KEEP_D より古い記録を捨てて一時ファイル→os.replace で保存。失敗しても収集は止めない。"""
    tmp = None
    try:
        path = _seen_path()
        cut = datetime.now(timezone.utc) - timedelta(days=HF_SEEN_KEEP_D)
        data = {k: v.strftime("%Y-%m-%dT%H:%M:%SZ") for k, v in sorted(seen.items()) if v >= cut}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".seen_models-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
        os.replace(tmp, path)
    except Exception as e:
        print(f"  話題: seen_models.json を保存できず続行 ({type(e).__name__}: {e})", file=sys.stderr)
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


_README_FENCE = re.compile(r"(```|~~~).*?(\1|\Z)", re.S)
_README_IMAGE = re.compile(r"!\[[^\]]*\](\([^)]*\)|\[[^\]]*\])")
_README_LINK = re.compile(r"\[([^\]]*)\](\([^)]*\)|\[[^\]]*\])")
_README_REFDEF = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M)
_README_TABLE_RULE = re.compile(r"^[\s|:\-]+$", re.M)
_README_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_README_QUOTE = re.compile(r"^\s*>+\s?", re.M)


def clean_readme(text: str, limit: int = HF_README_CHARS) -> str:
    """HF の README.md → 説明として読める本文の先頭 limit 字。YAML front-matter・HTML(コメント・タグ)・
    コードブロック・画像/バッジ・リンク記法(文字だけ残す)・見出し記号・表の罫線・強調記号を除く。"""
    s = (text or "").lstrip("\ufeff")
    if s.startswith("---"):
        end = re.search(r"^---\s*$", s[3:], re.M)
        s = s[3 + end.end():] if end else ""
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = _README_FENCE.sub(" ", s)
    s = _README_IMAGE.sub(" ", s)
    s = _README_LINK.sub(r"\1", s)
    s = _README_REFDEF.sub(" ", s)
    s = _README_TABLE_RULE.sub(" ", s)
    s = _README_HEADING.sub("", s)
    s = _README_QUOTE.sub("", s)
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\*\*|__|`", "", s)
    s = s.replace("|", " ")
    return newsfeeds.strip_html(s, limit=limit)


def _hf_candidates(rows: list[dict]) -> list[dict]:
    """上限 HF_MAX 件に絞り、README の冒頭を `body` に入れた候補にする(README が取れなければ body は空)。"""
    rows = sorted(rows, key=lambda r: (-r["likes"], -r["downloads"], -r["created"].timestamp()))[:HF_MAX]
    out = []
    for r in rows:
        first = r["first_seen"]
        # 「公開」とは書かない: first_seen は createdAt ではなく、このプログラムが初めて見た日
        when = f"初出 {first:%Y-%m-%d}"
        if r["created"].date() != first.date():
            when += f"・リポジトリ作成 {r['created']:%Y-%m-%d}"
        parts = [f"Hugging Face の新規モデル({when} UTC)"]
        if r["pipeline_tag"]:
            parts.append(f"種別: {r['pipeline_tag']}")
        parts.append(f"いいね {r['likes']:,}・ダウンロード {r['downloads']:,}")
        trend = {"source": "hf_models", "id": r["id"], "pipeline_tag": r["pipeline_tag"],
                 "likes": r["likes"], "downloads": r["downloads"]}
        if r["trending"]:
            trend["trending"] = r["trending"]
        item = FeedItem(title=r["id"], url=f"https://huggingface.co/{r['id']}",
                        source=f"Hugging Face / {r['id'].split('/', 1)[0]}", published=first,
                        origin="feed", summary="。".join(parts) + "。")
        out.append(_candidate(item, trend))
    # 説明文: huggingface.co は articles の本文取得の対象外(JS 描画)なので、README の冒頭をここで入れる
    readmes = _fetch_all(
        lambda mid: _get_text(f"https://huggingface.co/{mid}/raw/main/README.md", HF_TIMEOUT, HF_README_MAX_BYTES),
        [c["trend"]["id"] for c in out], HF_WORKERS, HF_README_BUDGET_S)
    for c, r in zip(out, readmes):
        if r and r[0] == "ok":
            c["body"] = clean_readme(r[1])
    return out


def hf_models(hours: float | None = 24) -> list[dict]:
    org_window = max(hours or 0, HF_ORG_FLOOR_H)
    org_urls = [f"{HF_API}?author={urllib.parse.quote(o)}&sort=createdAt&direction=-1&limit={HF_ORG_LIMIT}"
                for o in HF_ORGS]
    trend_url = (f"{HF_API}?sort=trendingScore&direction=-1&limit={HF_TREND_LIMIT}&{HF_TREND_EXPAND}")
    urls = [trend_url] + org_urls
    results = _fetch_all(lambda u: _get_json(u, HF_TIMEOUT), urls, HF_WORKERS, HF_BUDGET_S)   # 時間切れの取得は待たない
    max_age_h = HF_SEEN_MAX_AGE_D * 24
    found: dict[str, dict] = {}
    failed = 0
    for u, res in zip(urls, results):
        if res is None or res[0] == "err":
            failed += 1
            continue
        for r in parse_hf_models(res[1], max_age_h):
            prev = found.get(r["id"])
            if prev is None:
                found[r["id"]] = prev = {**r, "org": False}
            elif r["trending"] > prev["trending"]:   # 重複 id は1件(急上昇の値がある方を残す)
                prev.update(trending=r["trending"])
            if u != trend_url:
                prev["org"] = True
    if failed == len(urls):
        raise ConnectionError("Hugging Face の取得がすべて失敗しました")
    if failed:
        print(f"  話題: Hugging Face {failed}/{len(urls)} 件の取得に失敗(取れた分で続行)", file=sys.stderr)

    # 「新規」= 初めて見た時刻から窓以内。createdAt は非公開→公開で変わらないので使わない。
    # 状態ファイルが無い初回は createdAt を初出とみなし(古い急上昇モデルが一斉に新規扱いになるのを防ぐ)、
    # 2回目以降に初めて現れた id は今を初出とする。
    seen, existed = _load_seen()
    now = datetime.now(timezone.utc)
    fresh = []
    for r in found.values():
        first = seen.setdefault(r["id"], now if existed else r["created"])
        r["first_seen"] = first
        if r["downloads"] == 0 and now - r["created"] > timedelta(days=HF_NO_DL_AGE_D):
            continue   # 水増し・放置(ダウンロードが付かないまま日が経った)
        in_org = r["org"] and now - first <= timedelta(hours=org_window)
        in_trend = r["trending"] >= HF_TREND_MIN_SCORE and now - first <= timedelta(hours=HF_TREND_WINDOW_H)
        if in_org or in_trend:
            fresh.append(r)
    _save_seen(seen)
    return _hf_candidates(fresh)


def _safe(name: str, fn):
    def run(hours: float | None = 24) -> list[dict]:
        try:
            return fn(hours)
        except Exception as e:
            print(f"  話題: {name} 取得失敗のため省略 ({type(e).__name__}: {e})", file=sys.stderr)
            return []
    return run


# genres.toml の trend_sources に書く名前 → 取得関数(hours) -> 候補。失敗は [](stderr に1行)。
SOURCES = {
    "yahoo_realtime": _safe("Yahoo!リアルタイム検索", yahoo_realtime),
    "google_trends": _safe("Google トレンド", google_trends),
    "hatena": _safe("はてなブックマーク", hatena),
    "openrouter_models": _safe("OpenRouter 新規モデル", openrouter_models),
    "hf_models": _safe("Hugging Face 新規モデル", hf_models),
}
# 候補の上限(news_max)より先に全件入れる取得元。新モデルの登録一覧は1日数件〜十数件で、深い順位も落としたくない。
PINNED = {"openrouter_models", "hf_models"}
