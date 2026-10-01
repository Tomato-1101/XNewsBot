"""記事1本の「AI解説」。ヘッドレス Claude(本人のサブスク)がウェブ検索して解説を書く。

- 生成: claude CLI を WebSearch/WebFetch だけ許して実行(ops/explain_prompt.md)。300秒で打ち切り。
- 保存: NewsExplanation に記事ごと1行。作成済みは使い回す。
- 排他: webhook(LINE)と管理UIは別プロセスなので、news_item_id の一意制約と条件付き UPDATE で
  「running を確保できた1人だけが生成する」。確保ごとの claim_token が一致する生成だけが結果を書け、
  LINE への push もその生成だけが行う(失効後に古い生成が終わっても二重に送らない)。
- 同時に作るのは MAX_RUNNING 件まで(webhook と管理UIの共通。確保と同じ書き込みトランザクションで数える)。
- 朝のキュレーションと同じサブスク枠を使うので、QUIET_START〜QUIET_END は新しく作らない。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import and_, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from . import db
from . import line_client as lc
from .models import NewsExplanation, NewsItem

log = logging.getLogger("xnewsbot.explain")

CLAUDE_BIN = "/Users/tomato/.local/bin/claude"
MODEL = "sonnet"
EFFORT = "medium"
TOOLS = ("WebSearch", "WebFetch")
TIMEOUT = 300                       # 秒。超えたらプロセスごと kill して失敗扱い
STALE_AFTER = timedelta(minutes=15)  # running がこれより古ければ(プロセスが落ちた等)取り直せる
MAX_CHARS = 4500                    # LINE テキストの上限 5000 字に見出しを足しても収まるように切る
PROMPT_PATH = Path(__file__).resolve().parent.parent / "ops" / "explain_prompt.md"
MIN_CHARS = 200                     # これより短い出力は解説になっていないとみなす
REQUIRED_HEADING = "【何が起きたか】"  # プロンプトの書式に従っていない出力(拒否文など)を弾く
MAX_RUNNING = 2                     # 同時に作る数の上限(サブスク枠を食い尽くさない)
QUIET_START, QUIET_END = time(7, 0), time(8, 30)  # 朝のキュレーション(07:15〜)の時間帯は受け付けない
# claude 子プロセスに渡す環境変数。LINE のトークン等の秘密を外部テキストを読むプロセスへ渡さない
CLAUDE_ENV_KEYS = ("HOME", "PATH", "USER", "LOGNAME", "LANG", "TMPDIR", "SHELL")
# webhook プロセス内で「通数の判定→確保」を1区間にする(作成中の分を数え漏らさない)
LINE_CLAIM_LOCK = threading.Lock()

# (news_item_id, claim_token, 送り先) を受けて裏で生成→push する関数(webhook が渡す)
ExplainStart = Callable[[int, str, str], None]


class ExplainError(Exception):
    """生成に失敗した(理由は管理UIにも出すので短い日本語で)。"""


def _now() -> datetime:
    # SQLite は tz を落とすので naive UTC で統一する(STALE_AFTER の比較を崩さない)
    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------- 状態と排他

def get(session, item_id: int, title: str | None = None) -> NewsExplanation | None:
    """記事の解説行。title を渡すと、行の見出しが違う(記事 id が使い回された)ときは None。"""
    row = session.exec(
        select(NewsExplanation).where(NewsExplanation.news_item_id == item_id)
    ).first()
    if row is not None and title is not None and row.title != title:
        return None
    return row


def in_quiet_hours(now_local: datetime) -> bool:
    """朝のキュレーションの時間帯か(この間は新しく作らない)。now_local は購読者/管理UIの現地時刻。"""
    return QUIET_START <= now_local.time() < QUIET_END


def _live_running(now: datetime):
    E = NewsExplanation
    return and_(E.status == "running", E.updated_at >= now - STALE_AFTER)


def running_count(session, now: datetime | None = None) -> int:
    """失効していない作成中の件数。"""
    return session.exec(select(func.count()).select_from(NewsExplanation)
                        .where(_live_running(now or _now()))).one()


def reserved_push_cost(session, now: datetime | None = None) -> int:
    """失効していない作成中の分が、終わったときに push で使う通数の合計。"""
    return session.exec(select(func.coalesce(func.sum(NewsExplanation.push_cost), 0))
                        .where(_live_running(now or _now()))).one()


def _is_stale(row: NewsExplanation, now: datetime) -> bool:
    return row.status == "running" and row.updated_at < now - STALE_AFTER


def status_of(row: NewsExplanation | None, now: datetime | None = None) -> str:
    """"none" | "running" | "done" | "failed"。失効した running は failed として扱う(押し直せる)。"""
    if row is None:
        return "none"
    if _is_stale(row, now or _now()):
        return "failed"
    return row.status


def state(row: NewsExplanation | None, now: datetime | None = None) -> dict:
    """管理UIの状態 JSON。elapsed は作成中のときだけ、確保からの経過秒。"""
    now = now or _now()
    st = status_of(row, now)
    error = ""
    if st == "failed":
        error = row.error if row.status == "failed" else "作成が途中で止まりました"
    return {
        "status": st,
        "text": row.text if st == "done" else "",
        "error": error,
        "elapsed": int((now - row.updated_at).total_seconds()) if st == "running" else None,
    }


def done_texts(session, titles: dict[int, str]) -> dict[int, str]:
    """作成済み(done)の解説を記事 id ごとに一括で引く(ニュース画面の初期表示用)。

    titles は {記事 id: 今の見出し}。見出しが違う行(記事 id の使い回し)は返さない。"""
    if not titles:
        return {}
    rows = session.exec(select(NewsExplanation).where(
        NewsExplanation.news_item_id.in_(list(titles)), NewsExplanation.status == "done")).all()
    return {r.news_item_id: r.text for r in rows if r.title == titles[r.news_item_id]}


def claim(session, item_id: int, now: datetime | None = None, *, title: str = "",
          push_to: str = "", push_cost: int = 0) -> str | None:
    """生成役を確保する。確保できたら claim_token、他が作成中・作成済み・同時上限なら None。

    1) 行が無ければ running で INSERT(一意制約で同時に入れられるのは1人だけ)。
    2) 既にあれば、failed か失効した running か見出しが違う(記事 id の使い回し)ときだけ
       条件付き UPDATE で取り直す(1文の UPDATE なので、別プロセスと同時でも書けるのは1人だけ)。
    3) 同じトランザクションで作成中の件数を数え、MAX_RUNNING を超えたら取り消す。SQLite は
       書き込みが直列なので、別プロセスの確保と同時でも上限を超えない。"""
    now = now or _now()
    token = uuid.uuid4().hex
    fields = {"status": "running", "claim_token": token, "title": title, "push_to": push_to,
              "push_cost": push_cost, "text": "", "error": "", "updated_at": now}
    session.add(NewsExplanation(news_item_id=item_id, created_at=now, **fields))
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        E = NewsExplanation
        res = session.execute(
            update(E).where(E.news_item_id == item_id).where(or_(
                E.status == "failed",
                and_(E.status == "running", E.updated_at < now - STALE_AFTER),
                E.title != title,
            )).values(created_at=now, **fields)
        )
        if res.rowcount != 1:
            session.rollback()
            return None
    if running_count(session, now) > MAX_RUNNING:
        session.rollback()
        return None
    session.commit()
    return token


def finish(session, item_id: int, token: str, *, text: str | None = None,
           error: str | None = None) -> bool:
    """結果を書く。確保が自分のもの(claim_token 一致かつ running)のときだけ書けて True。"""
    E = NewsExplanation
    values = ({"status": "done", "text": text, "error": ""} if text is not None
              else {"status": "failed", "text": "", "error": (error or "不明なエラー")[:500]})
    res = session.execute(
        update(E).where(E.news_item_id == item_id, E.claim_token == token, E.status == "running")
        .values(updated_at=_now(), **values)
    )
    session.commit()
    return res.rowcount == 1


# ---------------------------------------------------------------- 生成

def item_payload(item: NewsItem) -> dict:
    """プロンプトに入れる記事データ(外部由来。プロンプト側でデータとして区切る)。"""
    posts = [{"source": lc._source_name(s), "url": s.get("url", ""),
              "text": str(s.get("text", ""))[:1000]}
             for s in (item.source_tweets or [])[:5]]
    return {
        "genre": item.genre,
        "title": item.title,
        "summary": item.summary,
        "detail": item.detail,
        "source_urls": list(item.source_urls or [])[:5],
        "posts": posts,
    }


def build_prompt(payload: dict) -> str:
    # 区切りの合言葉は毎回ランダム。データ側に終端の行を仕込まれても区切りを抜け出せない。
    # 記事データは JSON にして入れる(先に合言葉を置換し、データ内の文字列を書き換えない)。
    nonce = secrets.token_hex(8)
    template = PROMPT_PATH.read_text(encoding="utf-8").replace("__NONCE__", nonce)
    return template.replace("__ARTICLE__", json.dumps(payload, ensure_ascii=False, indent=1))


def build_argv(prompt: str) -> list[str]:
    # 記事と検索結果は信用できない外部テキスト。安全フラグは ops/deliver.sh の claude 呼び出しを踏襲し、
    # ツールはウェブ検索と取得だけ(Read/Write/Bash は与えない)。dontAsk で許可外は即拒否(ハングさせない)。
    return [CLAUDE_BIN, "--model", MODEL, "--effort", EFFORT, "-p", prompt,
            "--tools", ",".join(TOOLS), "--permission-mode", "dontAsk", "--setting-sources", "",
            "--strict-mcp-config", "--no-session-persistence", "--safe-mode",
            "--allowedTools", *TOOLS]


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # 子プロセスごと止める(start_new_session で別グループ)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def claude_env() -> dict[str, str]:
    return {k: os.environ[k] for k in CLAUDE_ENV_KEYS if k in os.environ}


def run_claude(prompt: str) -> str:
    """claude を実行して標準出力を返す。失敗・時間切れは ExplainError。"""
    # cwd は実行ごとの空ディレクトリ(.env のあるリポジトリ直下で動かさない)
    work = tempfile.mkdtemp(prefix="xnews_explain_")
    try:
        try:
            proc = subprocess.Popen(build_argv(prompt), cwd=work, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True, env=claude_env())
        except OSError as e:
            raise ExplainError(f"claude を起動できませんでした: {e}") from e
        try:
            out, err = proc.communicate(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill(proc)
            raise ExplainError(f"時間切れ({TIMEOUT}秒)") from None
        if proc.returncode != 0:
            tail = ((err or "") + (out or "")).strip()[-300:]
            raise ExplainError(f"claude が異常終了しました(exit={proc.returncode}) {tail}".strip())
        return out or ""
    finally:
        shutil.rmtree(work, ignore_errors=True)


_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*", re.M)


def clean(text: str) -> str:
    """指示しても混ざりがちな Markdown の見出し記号・太字記号を落とし、上限で切る。

    空・短すぎる・決めた書式(【何が起きたか】)が無い出力は、解説になっていないので失敗扱い。"""
    text = _HEADING.sub("", text or "").replace("**", "").strip()
    if not text:
        raise ExplainError("解説が空でした")
    if len(text) < MIN_CHARS:
        raise ExplainError(f"解説が短すぎました({len(text)}字)")
    if REQUIRED_HEADING not in text:
        raise ExplainError("解説の書式になっていませんでした")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "…"
    return text


def generate(payload: dict) -> str:
    return clean(run_claude(build_prompt(payload)))


def run_job(item_id: int, token: str, session_factory=None) -> dict | None:
    """確保済みの記事について生成し、結果を保存する(裏のスレッドで呼ぶ)。

    返り値 {"ok", "text", "error", "title"}。確保が他に移っていて書けなかったら None。"""
    factory = session_factory or db.get_session
    with factory() as s:
        item = s.get(NewsItem, item_id)
        payload = item_payload(item) if item else None
        title = item.title if item else ""
    text = error = None
    if payload is None:
        error = "記事が見つかりません"
    else:
        try:
            text = generate(payload)
        except ExplainError as e:
            error = str(e)
        except Exception as e:  # noqa: BLE001 — 裏スレッドで落ちると running のまま15分残るため失敗として記録する
            log.exception("AI解説の生成で想定外のエラー item=%s", item_id)
            error = f"想定外のエラー: {type(e).__name__}"
    if error:
        log.warning("AI解説を作れませんでした item=%s: %s", item_id, error)
    with factory() as s:
        mine = finish(s, item_id, token, text=text, error=error)
    if not mine:
        log.warning("AI解説の確保が別の生成に移っていたため結果を捨てました item=%s", item_id)
        return None
    return {"ok": text is not None, "text": text or "", "error": error or "", "title": title}


# ---------------------------------------------------------------- LINE

def line_text(title: str, text: str) -> str:
    return f"【AI解説】{title}\n\n{text}"


def run_line_job(item_id: int, token: str, to: str, messenger, session_factory=None) -> None:
    """生成して、押されたトーク(to)へテキスト1通を push する(失敗時も1通で知らせる)。"""
    res = run_job(item_id, token, session_factory)
    if res is None:  # 確保が他に移った=そちらが送る。ここからは送らない(二重送信防止)
        return
    if res["ok"]:
        spec = lc.text_spec(line_text(res["title"], res["text"]))
    else:
        spec = lc.text_spec(f"「{res['title']}」のAI解説を作れませんでした。"
                            "時間をおいて、もう一度「AI解説」を押してください。")
    try:
        messenger.push(to, [spec])
    except Exception:  # noqa: BLE001 — 裏スレッド。送れなかったことはログに残す
        log.exception("AI解説の push に失敗 item=%s to=%s", item_id, to)


def make_line_starter(messenger, session_factory=None) -> ExplainStart:
    """webhook 用。webhook の処理を塞がないよう、生成と push は別スレッドで行う。"""
    def _start(item_id: int, token: str, to: str) -> None:
        threading.Thread(target=run_line_job, args=(item_id, token, to, messenger, session_factory),
                         daemon=True, name=f"explain-{item_id}").start()
    return _start
