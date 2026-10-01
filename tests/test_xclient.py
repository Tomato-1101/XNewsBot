"""収集クエリ(日本語/英語/公式の3クエリ)と採用条件(いいね OR 表示回数)・重複除去、
複数キーのフォールバックの回帰テスト。"""

from __future__ import annotations

import pytest

from xnewsbot import xclient


class _Settings:
    collect_hours = 24
    collect_max_tweets = 60
    collect_min_faves = 200
    collect_min_views_floor = 20000


def _fake_genre(monkeypatch, *, kws=("生成AI", "GitHub Copilot"), kws_en=("OpenAI", "Claude Code"),
                accounts=("OpenAI", "AnthropicAI"), ex=("銘柄",), deny=(), min_faves=None,
                min_faves_en=None, xq=()):
    """genres.toml の現在値に依存しないよう、xclient が参照するジャンル定義を差し替える。"""
    monkeypatch.setattr(xclient, "keywords", lambda g: list(kws))
    monkeypatch.setattr(xclient, "keywords_en", lambda g: list(kws_en))
    monkeypatch.setattr(xclient, "accounts", lambda g: list(accounts))
    monkeypatch.setattr(xclient, "excludes", lambda g: list(ex))
    monkeypatch.setattr(xclient, "exclude_accounts", lambda g: list(deny))
    monkeypatch.setattr(xclient, "min_faves", lambda g: min_faves)
    monkeypatch.setattr(xclient, "min_faves_en", lambda g: min_faves_en)
    monkeypatch.setattr(xclient, "x_queries", lambda g: [dict(q) for q in xq])


def _route(ja=(), en=(), official=(), calls=None):
    """クエリの種類(日本語/英語/公式)ごとに返すツイートを変える fetch_with_retry の偽物。"""
    def fake(q, qt, mx, keys):
        if calls is not None:
            calls.append((q, mx))
        if "from:" in q:
            return [dict(t) for t in official]
        if "lang:en" in q:
            return [dict(t) for t in en]
        return [dict(t) for t in ja]
    return fake


def test_collect_builds_three_queries(monkeypatch):
    """日本語(lang:ja)・英語(lang:en)・公式(from:)の3クエリを、上限 60/40/20 で投げる。"""
    _fake_genre(monkeypatch)
    calls = []
    monkeypatch.setattr(xclient, "fetch_with_retry", _route(calls=calls))
    xclient.collect("AI", settings=_Settings(), keys=["k"])
    assert len(calls) == 3
    (q_ja, mx_ja), (q_en, mx_en), (q_off, mx_off) = calls
    assert q_ja == '(生成AI OR "GitHub Copilot") lang:ja within_time:24h -銘柄'
    assert q_en == '(OpenAI OR "Claude Code") lang:en within_time:24h -銘柄'
    assert q_off == "(from:OpenAI OR from:AnthropicAI) within_time:24h"
    assert (mx_ja, mx_en, mx_off) == (60, 40, 20)


def test_collect_skips_queries_without_terms(monkeypatch):
    """keywords_en / accounts が無いジャンルは日本語クエリだけ(無駄なリクエストを投げない)。"""
    _fake_genre(monkeypatch, kws_en=(), accounts=())
    calls = []
    monkeypatch.setattr(xclient, "fetch_with_retry", _route(calls=calls))
    xclient.collect("健康", settings=_Settings(), keys=["k"])
    assert [q for q, _ in calls] == ["(生成AI OR \"GitHub Copilot\") lang:ja within_time:24h -銘柄"]


def test_collect_keeps_high_view_even_if_low_likes(monkeypatch):
    """いいねが下限未満でも、表示回数が多い速報は採用する(いいね OR 表示回数)。"""
    _fake_genre(monkeypatch, min_faves=300, kws_en=(), accounts=())
    ja = [
        {"id": "1", "viewCount": 50000, "likeCount": 1, "text": "速報A", "createdAt": ""},  # 高view低like
        {"id": "2", "viewCount": 10, "likeCount": 1, "text": "雑談B", "createdAt": ""},      # 両方低い → 除外
        {"id": "3", "viewCount": 100, "likeCount": 500, "text": "人気C", "createdAt": ""},   # 高like → 採用
    ]
    monkeypatch.setattr(xclient, "fetch_with_retry", _route(ja=ja))
    texts = {t["text"] for t in xclient.collect("特大", settings=_Settings(), keys=["k"])}
    assert texts == {"速報A", "人気C"}


def test_collect_english_has_higher_floor(monkeypatch):
    """英語は日本語より下限が高い: 同じ 300いいね・5万表示でも日本語は採用・英語は除外。"""
    _fake_genre(monkeypatch, accounts=())
    mid = {"viewCount": 50000, "likeCount": 300, "createdAt": ""}
    big = {"viewCount": 10, "likeCount": 800, "createdAt": ""}
    monkeypatch.setattr(xclient, "fetch_with_retry", _route(
        ja=[{**mid, "id": "j1", "text": "日本語の話題"}],
        en=[{**mid, "id": "e1", "text": "english mid"}, {**big, "id": "e2", "text": "english big"}]))
    texts = {t["text"] for t in xclient.collect("AI", settings=_Settings(), keys=["k"])}
    assert texts == {"日本語の話題", "english big"}


def test_collect_official_first_without_like_floor(monkeypatch):
    """公式投稿はいいね下限なしで残り、_official=True で先頭に並ぶ(返信は除外)。残りは viewCount 降順。"""
    _fake_genre(monkeypatch)
    off = [{"id": "o1", "viewCount": 5, "likeCount": 0, "text": "公式の発表", "createdAt": ""},
           {"id": "o2", "viewCount": 9, "likeCount": 0, "text": "公式の返信", "isReply": True, "createdAt": ""}]
    ja = [{"id": "j1", "viewCount": 900000, "likeCount": 5000, "text": "大人気", "createdAt": ""},
          {"id": "j2", "viewCount": 30000, "likeCount": 9000, "text": "中くらい", "createdAt": ""}]
    monkeypatch.setattr(xclient, "fetch_with_retry", _route(ja=ja, official=off))
    out = xclient.collect("AI", settings=_Settings(), keys=["k"])
    assert [t["text"] for t in out] == ["公式の発表", "大人気", "中くらい"]
    assert out[0]["_official"] is True and not out[1].get("_official")


def test_collect_partial_failure_keeps_other_queries(monkeypatch):
    """1クエリが失敗しても他のクエリの結果は返す。全クエリ失敗のときだけ例外。"""
    _fake_genre(monkeypatch)
    ok = {"id": "o1", "viewCount": 5, "likeCount": 0, "text": "公式", "createdAt": ""}

    def partly(q, qt, mx, keys):
        if "from:" in q:
            return [dict(ok)]
        raise xclient.XClientError("dead", status=402, detail="Credits is not enough")

    monkeypatch.setattr(xclient, "fetch_with_retry", partly)
    assert [t["text"] for t in xclient.collect("AI", settings=_Settings(), keys=["k"])] == ["公式"]

    def all_fail(q, qt, mx, keys):
        raise xclient.XClientError("dead")

    monkeypatch.setattr(xclient, "fetch_with_retry", all_fail)
    with pytest.raises(xclient.XClientError):
        xclient.collect("AI", settings=_Settings(), keys=["k"])


def test_collect_raw_query_without_keywords(monkeypatch):
    """キーワードの無いジャンルでも x_queries の生クエリだけで集める。窓を足し、上限は max と管理UIの小さい方。
    いいね下限はクライアント側で確定的に効かせ(表示回数では救済しない)、返信・除外語も落とす。"""
    _fake_genre(monkeypatch, kws=(), kws_en=(), accounts=(), ex=("プレゼント企画",), xq=[
        {"query": "lang:ja min_faves:20000 -filter:replies", "max": 80, "min_faves": 20000}])
    calls = []
    ja = [
        {"id": "1", "viewCount": 10, "likeCount": 25000, "text": "バズった話", "createdAt": ""},
        {"id": "2", "viewCount": 9_000_000, "likeCount": 19999, "text": "惜しい", "createdAt": ""},
        {"id": "3", "viewCount": 10, "likeCount": 30000, "text": "返信", "isReply": True, "createdAt": ""},
        {"id": "4", "viewCount": 10, "likeCount": 90000, "text": "プレゼント企画です", "createdAt": ""},
    ]
    monkeypatch.setattr(xclient, "fetch_with_retry", _route(ja=ja, calls=calls))
    out = xclient.collect("話題", settings=_Settings(), keys=["k"])
    assert calls == [("lang:ja min_faves:20000 -filter:replies within_time:24h", 60)]
    assert [t["text"] for t in out] == ["バズった話"]


def test_merge_tweets_dedup_rt_and_denylist():
    """RT・除外アカウントを落とし、ID と正規化本文(URL・空白違い)で重複を1件にする。同文は表示回数の多い方が残る。"""
    def tw(i, text, views, user="u", **kw):
        return {"id": i, "text": text, "viewCount": views, "author": {"userName": user}, **kw}

    official = [tw("1", "OpenAI が新モデルを発表 https://t.co/a", 100, "OpenAI", _official=True)]
    others = [
        tw("1", "OpenAI が新モデルを発表 https://t.co/a", 100, "OpenAI"),   # 公式と同じID → 落ちる
        tw("2", "RT @OpenAI: OpenAI が新モデルを発表", 99999, "fan"),        # RT → 落ちる
        tw("3", "使い回し速報", 50000, "WhaleNewsDaily"),                     # 除外アカウント → 落ちる
        tw("4", "同じ  本文です https://t.co/x", 10, "a"),                   # 同文(表示少) → 落ちる
        tw("5", "同じ本文です\nhttps://t.co/y", 20, "b"),                   # 同文(表示多) → 残る
        tw("6", "別の話題", 5, "c"),
    ]
    out = xclient.merge_tweets(official, others, ["whalenewsdaily"])
    assert [t["id"] for t in out] == ["1", "5", "6"]
    assert out[0].get("_official") is True


def test_fallback_log_includes_reason_without_key(monkeypatch, capsys):
    """キー切替ログに失敗理由(HTTP ステータスとメッセージ)を出す。鍵の値は出さない。"""
    def fake_fetch(q, qt, mx, key):
        if key == "SECRETKEY0":
            raise xclient.XClientError("twitterapi.io HTTP 402: ...", status=402,
                                       detail="Credits is not enough.Please recharge SECRETKEY0")
        return [{"viewCount": 1, "text": "ok"}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    xclient.fetch_with_retry("q", "Top", 10, ["SECRETKEY0", "k2"])
    err = capsys.readouterr().err
    assert "キー#0 失敗 → 次キーへ" in err
    assert "HTTP 402: Credits is not enough.Please recharge" in err
    assert "SECRETKEY0" not in err


def test_error_detail_from_json_body():
    assert xclient._error_detail('{"error":"Unauthorized","message":"Credits is not enough"}') \
        == "Credits is not enough"
    assert xclient._error_detail("plain text") == "plain text"


# --- 複数キーの優先度フォールバック ---

def test_failover_to_next_key_on_permanent_error(monkeypatch):
    """上位キーが恒久エラー(4xx)なら次の優先度キーへフォールバックして成功する。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        if key == "k1":
            raise xclient.XClientError("k1 dead (4xx)")
        return [{"viewCount": 1, "text": "ok", "likeCount": 0, "createdAt": ""}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    out = xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"])
    assert [t["text"] for t in out] == ["ok"]
    assert calls == ["k1", "k2"]  # k1 で失敗 → k2 へ


def test_failover_on_retryable_exhausted(monkeypatch):
    """上位キーが一時エラーで再試行を尽くしたら次キーへフォールバックする。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        if key == "k1":
            raise xclient.XClientRetryable("k1 timeout")
        return [{"viewCount": 1, "text": "ok", "likeCount": 0, "createdAt": ""}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    out = xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"], retries=1)
    assert [t["text"] for t in out] == ["ok"]
    assert calls == ["k1", "k2"]


def test_no_fallback_when_top_key_succeeds(monkeypatch):
    """最優先キーで成功したら下位キーは呼ばない(無駄な消費をしない)。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        return [{"viewCount": 1, "text": "ok"}]

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2", "k3"])
    assert calls == ["k1"]


def test_empty_result_does_not_fallback(monkeypatch):
    """空結果(該当ツイート無し)は確定。下位キーへフォールバックしない。"""
    calls = []

    def fake_fetch(q, qt, mx, key):
        calls.append(key)
        return []

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    out = xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"], retries=1)
    assert out == []
    assert calls == ["k1"]


def test_all_keys_fail_raises(monkeypatch):
    """全キーが失敗したら最後の例外を送出する。"""
    def fake_fetch(q, qt, mx, key):
        raise xclient.XClientError(f"{key} dead")

    monkeypatch.setattr(xclient, "fetch", fake_fetch)
    with pytest.raises(xclient.XClientError):
        xclient.fetch_with_retry("q", "Top", 10, ["k1", "k2"])


# --- load_keys / _split_keys ---

def test_split_keys_comma_and_newline():
    assert xclient._split_keys("a, b\nc") == ["a", "b", "c"]
    assert xclient._split_keys("  ") == []
    assert xclient._split_keys(None) == []


def test_load_keys_single_key_backcompat(monkeypatch):
    """単一キーのみのとき1要素リストを返す(後方互換)。"""
    class S:
        twitterapi_io_key = "solo"

    monkeypatch.delenv("TWITTERAPI_IO_KEY", raising=False)
    monkeypatch.setattr(xclient, "_keychain_get", lambda: "")
    monkeypatch.setattr(xclient, "KEY_FILE", xclient.Path("/nonexistent/.key"))
    assert xclient.load_keys(S()) == ["solo"]


def test_load_keys_dedup_and_order(monkeypatch):
    """複数ソースから優先度順(.env→環境変数→Keychain)で集め、重複を順序保持で除去する。"""
    class S:
        twitterapi_io_key = "a,b"

    monkeypatch.setenv("TWITTERAPI_IO_KEY", "b,c")
    monkeypatch.setattr(xclient, "_keychain_get", lambda: "c")
    monkeypatch.setattr(xclient, "KEY_FILE", xclient.Path("/nonexistent/.key"))
    assert xclient.load_keys(S()) == ["a", "b", "c"]


# --- fetch_balance(残高 API。ネットワークは使わない) ---

class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_fetch_balance_parses_recharge_credits(monkeypatch):
    """recharge_credits を int で返し、鍵はヘッダにだけ載せる(URL に出さない)・timeout 15秒。"""
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"], seen["key"], seen["timeout"] = req.full_url, req.get_header("X-api-key"), timeout
        return _Resp(b'{"recharge_credits": 3040677, "total_bonus_credits": 0}')

    monkeypatch.setattr(xclient.urllib.request, "urlopen", fake_urlopen)
    assert xclient.fetch_balance("secret-key") == 3040677
    assert seen["url"] == "https://api.twitterapi.io/oapi/my/info"
    assert seen["key"] == "secret-key" and "secret-key" not in seen["url"]
    assert seen["timeout"] == 15


def test_fetch_balance_negative_balance_is_returned_as_is(monkeypatch):
    monkeypatch.setattr(xclient.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(b'{"recharge_credits": -120}'))
    assert xclient.fetch_balance("k") == -120


@pytest.mark.parametrize("body", [b"not json", b'{"other": 1}', b'{"recharge_credits": "x"}',
                                  b'{"recharge_credits": null}', b"[]"])
def test_fetch_balance_bad_body_is_none(monkeypatch, body):
    monkeypatch.setattr(xclient.urllib.request, "urlopen", lambda req, timeout=None: _Resp(body))
    assert xclient.fetch_balance("k") is None


def test_fetch_balance_network_error_is_none(monkeypatch):
    def boom(req, timeout=None):
        raise xclient.urllib.error.URLError("down")

    monkeypatch.setattr(xclient.urllib.request, "urlopen", boom)
    assert xclient.fetch_balance("k") is None
