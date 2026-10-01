"""設定管理。ジャンル / APIキー / 収集パラメータ / 配信時刻 を編集する。

すべて PRG パターン(POST→処理→/manage へ 303 リダイレクト)。結果は ?ok= / ?err= で表示。
ファイル書き込みは stores.py 経由。反映は「次回 collect が新プロセスで読み直す」で自動
(配信時刻のみ launchd 再登録を伴う)。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config import get_settings
from . import news, stores
from .web import require_auth, templates

router = APIRouter()

_OK_MESSAGES = {
    "genres": "ジャンル設定を保存しました(次回の収集から反映されます)。",
    "keys": "APIキーを更新しました(次回の収集から反映されます)。",
    "collect": "収集パラメータを保存しました(次回の収集から反映されます)。",
    "delivery": "配信時刻を変更し、配信ジョブを再登録しました。",
    "run_now": "今すぐ配信を開始しました。バックグラウンドで最新を収集・要約し、全購読者へ送信します"
               "(完了まで約20分。結果は配信ログに記録されます)。",
    "run_refresh": "今すぐ更新を開始しました。バックグラウンドで最新を収集・要約します"
                   "(完了まで約20分。LINE へは送信しません)。",
}


def _redirect(query: str) -> RedirectResponse:
    return RedirectResponse(url=f"/manage?{query}", status_code=303)


@router.get("/manage", response_class=HTMLResponse)
def manage_index(
    request: Request,
    ok: str | None = None,
    err: str | None = None,
    _user: str = Depends(require_auth),
) -> HTMLResponse:
    keys = [{"index": i, "masked": stores.mask_key(k)} for i, k in enumerate(stores.read_keys())]
    collect = [
        {"env": env, "label": label, "value": stores.read_collect_params()[env], "min": minimum}
        for env, _attr, _cast, label, minimum in stores.COLLECT_FIELDS
    ]
    with news.get_session() as session:
        usage = news.usage_context(session, datetime.now(ZoneInfo(get_settings().default_tz)))
    return templates.TemplateResponse(
        request,
        "manage.html",
        {
            "active": "manage",
            "genres_text": stores.read_genres_text(),
            "keys": keys,
            "collect": collect,
            "delivery": stores.read_delivery_times(),
            "flash_ok": _OK_MESSAGES.get(ok or ""),
            "flash_err": err,
            "usage": usage,
        },
    )


@router.post("/manage/run")
def run_now(mode: str = Form(...), _user: str = Depends(require_auth)) -> RedirectResponse:
    """「今すぐ」: 定刻を待たず収集→キュレーションを起動する(deliver.sh をバックグラウンド実行)。
    mode='now'=全購読者へ配信(無料枠を消費する不可逆操作) / 'refresh'=配信せず収集のみ(無料)。"""
    if mode not in ("now", "refresh"):
        return _redirect("err=不正なモードです。")
    try:
        stores.launch_deliver(mode)
    except Exception as e:
        return _redirect(f"err=起動に失敗: {e}")
    return _redirect(f"ok=run_{mode}")


@router.post("/manage/genres")
def save_genres(toml_text: str = Form(...), _user: str = Depends(require_auth)) -> RedirectResponse:
    try:
        stores.write_genres_text(toml_text)
    except Exception as e:  # TOMLDecodeError / ValueError
        return _redirect(f"err=ジャンル保存に失敗: {e}")
    return _redirect("ok=genres")


@router.post("/manage/keys")
def edit_keys(
    action: str = Form(...),
    value: str = Form(""),
    index: int = Form(-1),
    _user: str = Depends(require_auth),
) -> RedirectResponse:
    keys = stores.read_keys()
    if action == "add":
        v = value.strip()
        if not v:
            return _redirect("err=キーが空です。")
        if v in keys:
            return _redirect("err=同じキーが既に登録されています。")
        keys.append(v)
    elif action == "delete" and 0 <= index < len(keys):
        keys.pop(index)
    elif action == "up" and 0 < index < len(keys):
        keys[index - 1], keys[index] = keys[index], keys[index - 1]
    elif action == "down" and 0 <= index < len(keys) - 1:
        keys[index + 1], keys[index] = keys[index], keys[index + 1]
    else:
        return _redirect("err=不正な操作です。")
    stores.write_keys(keys)
    return _redirect("ok=keys")


@router.post("/manage/collect")
async def save_collect(request: Request, _user: str = Depends(require_auth)) -> RedirectResponse:
    form = await request.form()
    for env, _attr, cast, label, minimum in stores.COLLECT_FIELDS:
        raw = (form.get(env) or "").strip()
        if raw == "":
            continue
        try:
            value = cast(raw)  # 数値として妥当か検証(不正なら書かない)
        except (TypeError, ValueError):
            return _redirect(f"err={label}({env}) の値が不正です: {raw}")
        # 0/負値は収集を無音で壊す(0件で配信中止・期間制限の消失)ため保存しない
        if value < minimum:
            return _redirect(f"err={label}({env}) は {minimum:g} 以上にしてください: {raw}")
        stores.set_env_var(env, raw)
    return _redirect("ok=collect")


@router.post("/manage/delivery")
def save_delivery(
    slot: str = Form(...),
    hhmm: str = Form(...),
    _user: str = Depends(require_auth),
) -> RedirectResponse:
    try:
        stores.write_delivery_time(slot, hhmm.strip().replace(":", ""))
    except Exception as e:
        return _redirect(f"err=配信時刻の変更に失敗(変更前に戻しました): {e}")
    return _redirect("ok=delivery")
