#!/usr/bin/env python3
"""配信前に Codex が全ジャンルを通して見て、重複の統合と不要記事の除外を決める（curated をその場で書き換える）。

Claude は組ごとに並列でキュレーションするため、組をまたぐ同じ出来事を1件にまとめられない（merge は結合するだけ）。
ここで全体を一度に見せて「外す」「まとめる」だけを決めさせ、適用はこのスクリプトが機械的に行う
（Codex に curated を直接書かせると source_idxs 等の構造を壊しうるため）。
失敗・異常時は curated を元のまま残す（fail-open）。レビューは二次チェックで、配信を止める理由にはしない。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from curate_fallback import validate  # noqa: E402  (ingest 前と同じ検証を適用後にもかける)

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT = 900          # 15分。deliver.sh の LOCK_STALE_SEC はこの分を見込んでいる
MAX_DROP_RATIO = 0.6   # これを超えて外そうとしたら Codex の故障とみなす
SMALL_SUMMARY_MAX = 100  # curate_prompt.md「書き方」: small の summary は100字以内

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["drop", "merge"],
    "properties": {
        "drop": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "reason"],
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}}}},
        "merge": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["into", "from", "summary", "detail"],
            "properties": {"into": {"type": "string"}, "from": {"type": "array", "items": {"type": "string"}},
                           "summary": {"type": "string"}, "detail": {"type": "string"}}}},
    },
}


def build_input(raw: dict, curated: dict) -> dict:
    items = []
    for genre, arr in curated["genres"].items():
        for i, it in enumerate(arr):
            row = {"id": f"{genre}/{i}", "title": it.get("title", ""), "summary": it.get("summary", ""),
                   "importance": it.get("importance"), "score": it.get("score"), "genres": it.get("genres", [])}
            if it.get("importance") == "big" and it.get("detail"):
                # 統合で detail を書き直させるので全文を渡す(冒頭だけだと元の詳細の事実が落ちる)
                row["detail"] = it["detail"]
            items.append(row)
    prefs = raw.get("preferences") if isinstance(raw.get("preferences"), dict) else {}
    return {"items": items, "recent_titles": raw.get("recent_titles") or {},
            "preferences": {"good": prefs.get("good") or [], "bad": prefs.get("bad") or []}}


def build_prompt(raw: dict, curated: dict) -> str:
    # Codex はシェルを無効にして動かす（記事は外部テキスト由来。仕込まれた指示で .env 等を読ませない）ため、
    # 選定基準はパスで読ませず本文をそのまま渡す（基準の正本は curate_prompt.md の1か所のまま）。
    return ((ROOT / "ops/review_prompt.md").read_text()
            + "\n\n# 選定基準（ops/curate_prompt.md）\n\n" + (ROOT / "ops/curate_prompt.md").read_text()
            + "\n\n# 入力 JSON\n\n" + json.dumps(build_input(raw, curated), ensure_ascii=False))


def parse_answer(text: str) -> dict:
    try:
        ans = json.loads(text)
    except ValueError:
        # --output-schema が効かず説明文やコードブロックが付いた場合に備え、最初の { から最後の } を取り出す
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("Codex の回答に JSON がない")
        ans = json.loads(m.group(0))
    if not isinstance(ans, dict) or not isinstance(ans.get("drop", []), list) \
            or not isinstance(ans.get("merge", []), list):
        raise ValueError("Codex の回答の形式が不正")
    return {"drop": ans.get("drop", []), "merge": ans.get("merge", [])}


def run_codex(prompt: str, timeout: int = TIMEOUT) -> dict:
    env_cmd = os.environ.get("XNEWS_REVIEW_CMD")
    if env_cmd:
        base = shlex.split(env_cmd)
    else:
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("Codex CLIが見つからない")
        base = [codex]
    # 作法は curate_fallback.codex_curate に揃える（ユーザー設定・ルールを読まない、空の作業ディレクトリ、ツール無効）
    with tempfile.TemporaryDirectory(prefix="xnews-review-") as cwd:
        out = Path(cwd) / "answer.json"
        schema = Path(cwd) / "schema.json"
        schema.write_text(json.dumps(SCHEMA))
        command = base + ["exec", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"',
                          "--ignore-user-config", "--ignore-rules", "--ephemeral",
                          "--skip-git-repo-check", "-C", cwd, "-s", "read-only",
                          "--disable", "shell_tool", "--disable", "code_mode_host", "--disable", "apps",
                          "--disable", "multi_agent", "-c", 'web_search="disabled"',
                          "--output-schema", str(schema), "-o", str(out), "-"]
        try:
            run = subprocess.run(command, input=prompt, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Codex が{timeout}秒で終わらずタイムアウト") from None
        if run.returncode:
            # CLI診断は資格情報や入力全文を含むことがあるため出力しない。
            raise RuntimeError(f"Codex CLI終了コード{run.returncode}")
        if not out.exists():
            raise RuntimeError("Codex の回答ファイルがない")
        return parse_answer(out.read_text())


def _ref(curated: dict, id_: object):
    if not isinstance(id_, str) or "/" not in id_:
        return None
    genre, _, n = id_.rpartition("/")
    arr = curated["genres"].get(genre)
    if arr is None or not n.isdigit() or int(n) >= len(arr):
        return None
    return genre, int(n)


def _item(curated: dict, id_: str) -> dict:
    g, n = _ref(curated, id_)
    return curated["genres"][g][n]


def _merge_error(curated: dict, m: dict) -> str | None:
    """統合後の summary・detail が書き方の決まりに合わなければ理由。合わない merge は丸ごと無視する
    (from だけ消えて、統合した記事の事実が親に入らないまま落ちるのを防ぐ)。"""
    small = _item(curated, m["into"]).get("importance") != "big"
    summary, detail = m.get("summary"), m.get("detail")
    if small and isinstance(summary, str) and len(summary.strip()) > SMALL_SUMMARY_MAX:
        return f"small の要約が{SMALL_SUMMARY_MAX}字超"
    if small and isinstance(detail, str) and detail.strip():
        return "small に detail を書いた（詳細は big だけ）"
    return None


def plan(curated: dict, answer: dict) -> tuple[list, list, list]:
    """有効な drop / merge と、無視した操作（理由つき）を返す。"""
    ops = [("drop", d) for d in answer["drop"]] + [("merge", m) for m in answer["merge"]]

    def ids_of(kind, op):
        if not isinstance(op, dict):
            return None
        if kind == "drop":
            return [op.get("id")]
        frm = op.get("from")
        return [op.get("into")] + (frm if isinstance(frm, list) else [None])

    # 同じ記事が複数の操作（または1つの merge の中で2回）に出たら、どちらが正しいか決められないので
    # 関わる操作をすべて無視する（迷ったら残す側に倒す）
    seen: dict = {}
    for kind, op in ops:
        for i in ids_of(kind, op) or []:
            if isinstance(i, str):
                seen[i] = seen.get(i, 0) + 1
    drops, merges, ignored = [], [], []
    for kind, op in ops:
        ids = ids_of(kind, op)
        reason = None
        if ids is None:
            reason = "操作の形式が不正"
        elif kind == "merge" and not op.get("from"):
            reason = "from が空"
        elif any(_ref(curated, i) is None for i in ids):
            reason = "存在しない id"
        elif any(seen.get(i, 0) > 1 for i in ids):
            reason = "同じ id が複数の操作に出ている（into と from が同じ場合を含む）"
        elif kind == "drop" and _item(curated, op["id"]).get("importance") == "big":
            # big はそのジャンルで今日いちばん大事な記事。重複なら merge で残す側に寄せる
            reason = "big の記事は除外しない（重複なら統合する）"
        if not reason and kind == "merge" and _item(curated, op["into"]).get("importance") != "big" \
                and any(_item(curated, f).get("importance") == "big" for f in op["from"]):
            # big の記事が small の親に吸収されると、big の詳細と枠が黙って消える
            reason = "big を small へ統合しない（into も big にする）"
        if not reason and kind == "merge":
            reason = _merge_error(curated, op)
        if reason:
            ignored.append({"op": kind, "detail": op, "reason": reason})
        elif kind == "drop":
            drops.append(op)
        else:
            merges.append(op)
    return drops, merges, ignored


def apply(curated: dict, drops: list, merges: list, ignored: list) -> tuple[dict, list, list]:
    genres = {g: [dict(it) for it in arr] for g, arr in curated["genres"].items()}
    removed: set = set()
    dropped, merged = [], []
    for d in drops:
        g, n = _ref(curated, d["id"])
        removed.add((g, n))
        dropped.append({"genre": g, "title": genres[g][n].get("title"), "reason": d.get("reason", "")})
    for m in merges:
        g, n = _ref(curated, m["into"])
        into = genres[g][n]
        absorbed = []
        for f in m["from"]:
            fg, fn = _ref(curated, f)
            src = genres[fg][fn]
            removed.add((fg, fn))
            absorbed.append({"genre": fg, "title": src.get("title")})
            into["genres"] = list(dict.fromkeys(list(into.get("genres", [])) + list(src.get("genres", []))))
            # source_idxs は「置いたジャンルの配列」の番号なので、同じジャンル内の統合だけ根拠を引き継げる
            if fg == g:
                into["source_idxs"] = list(dict.fromkeys(list(into.get("source_idxs", []))
                                                         + list(src.get("source_idxs", []))))
        # 書き方の決まりに合わない merge は plan で丸ごと無視済み(_merge_error)
        summary, detail = m.get("summary"), m.get("detail")
        if isinstance(summary, str) and summary.strip():
            into["summary"] = summary.strip()
        if isinstance(detail, str) and detail.strip():
            into["detail"] = detail.strip()
        merged.append({"into": {"genre": g, "title": into.get("title")}, "from": absorbed})
    out = {}
    for g, arr in genres.items():
        kept = [it for n, it in enumerate(arr) if (g, n) not in removed]
        kept.sort(key=lambda it: (it.get("importance") != "big", -(it.get("score") or 0)))
        bigs = [it for it in kept if it.get("importance") == "big"]
        for it in bigs[3:]:  # big 先頭・score 降順なので4件目以降が score の低い big
            it["importance"], it["detail"] = "small", ""
        if len(bigs) > 3:
            kept.sort(key=lambda it: (it.get("importance") != "big", -(it.get("score") or 0)))
        out[g] = kept
    result = {**curated, "genres": out}
    # レビューで空にしたジャンル。ingest は空の curated を「収集失敗」とみなして既存を残すので、
    # これは「空で確定」と伝えて前の回の記事を置き換えさせる(scripts/pipeline.py cmd_ingest)
    emptied = [g for g, arr in curated["genres"].items() if arr and not out[g]]
    if emptied:
        result["review_emptied"] = emptied
    return result, dropped, merged


def count(curated: dict) -> int:
    return sum(len(v) for v in curated["genres"].values())


def review(raw: dict, curated: dict, timeout: int = TIMEOUT) -> tuple[dict | None, dict, str]:
    """(適用後の curated または None=変更なし, report, 結果の1行)"""
    before = count(curated)
    report = {"date": raw.get("date"), "slot": raw.get("slot"), "before": before, "after": before,
              "dropped": [], "merged": [], "ignored": [], "error": None}
    if not before:
        report["status"] = "skipped"
        return None, report, "Codex レビュー: 記事0件のため省略"
    try:
        answer = run_codex(build_prompt(raw, curated), timeout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
        report.update(status="failed", error=f"{type(e).__name__}: {e}")
        return None, report, f"Codex レビュー失敗のため未レビューで配信: {e}"
    drops, merges, ignored = plan(curated, answer)
    report["ignored"] = ignored
    n_removed = len(drops) + sum(len(m["from"]) for m in merges)
    if n_removed > before * MAX_DROP_RATIO:
        msg = f"Codex が全{before}件中{n_removed}件を外そうとした（{int(MAX_DROP_RATIO * 100)}%超）"
        report.update(status="rejected", error=msg)
        return None, report, f"Codex レビュー失敗のため未レビューで配信: {msg}"
    result, dropped, merged = apply(curated, drops, merges, ignored)
    try:
        validate(raw, result)
    except (ValueError, TypeError, KeyError) as e:
        report.update(status="failed", error=f"適用後の検証失敗: {e}")
        return None, report, f"Codex レビュー失敗のため未レビューで配信: 適用後の検証失敗: {e}"
    after = count(result)
    report.update(status="applied", after=after, dropped=dropped, merged=merged)
    absorbed = sum(len(m["from"]) for m in merged)
    line = f"Codex レビュー: {before}→{after}件（除外{len(dropped)}・統合{absorbed}）"
    if ignored:
        line += f" 無視した操作{len(ignored)}"
    return result, report, line


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--raw", required=True)
    p.add_argument("--curated", required=True)
    p.add_argument("--report", required=True)
    # 定刻配信では deliver.sh が定刻までの残りから決める(既定は15分)
    p.add_argument("--timeout", type=int, default=TIMEOUT)
    args = p.parse_args()
    report: dict = {}
    try:
        raw = json.loads(Path(args.raw).read_text())
        curated = json.loads(Path(args.curated).read_text())
        result, report, line = review(raw, curated, args.timeout)
        if result is not None:
            # 途中で打ち切られても curated が半端に壊れないよう、別ファイルに書いてから置き換える
            tmp = Path(args.curated).with_suffix(".review.tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            os.replace(tmp, args.curated)
    except Exception as e:  # noqa: BLE001  レビューの不具合で配信を止めない（fail-open）
        report = {**report, "status": "failed", "error": f"{type(e).__name__}: {e}"}
        line = f"Codex レビュー失敗のため未レビューで配信: {type(e).__name__}: {e}"
    try:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        line += f"（report を書けず: {e}）"
    print(line)


if __name__ == "__main__":
    main()
