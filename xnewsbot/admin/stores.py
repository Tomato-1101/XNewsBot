"""設定ファイルの読み書き(単一の窓口)。

管理画面が触る対象: .key(APIキー) / .env(収集パラメータ) / config/genres.toml(ジャンル) /
ops/deliver.sh + launchd plist(配信時刻)。秘密の値(LINEトークン等)は読まない・出さない。
パスはモジュール定数にし、テストで monkeypatch して実ファイルを汚さない。
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import tomllib
from pathlib import Path

from ..config import Settings
from .. import xclient

ROOT = Path(__file__).resolve().parent.parent.parent

ENV_FILE = ROOT / ".env"
KEY_FILE = xclient.KEY_FILE  # .key (xclient.load_keys と同一ファイルを読み書きする)
GENRES_FILE = ROOT / "config" / "genres.toml"
DELIVER_SH = ROOT / "ops" / "deliver.sh"
REPO_PLIST = ROOT / "ops" / "com.tomato.xnewsbot-deliver.plist"
INSTALLED_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.tomato.xnewsbot-deliver.plist"
DELIVER_LABEL = "com.tomato.xnewsbot-deliver"

# (.env キー, Settings 属性, 変換, ラベル, 最小値) — 収集パラメータの編集対象
# 最小値は「無音で壊れる値」を弾くためのもの: 取得上限0だと全ジャンル0件で配信中止、
# 収集時間0だと期間の絞り込みが消えて全期間が対象になる(どちらもエラーにならず気づけない)。
COLLECT_FIELDS: list[tuple[str, str, type, str, float]] = [
    ("COLLECT_MAX_TWEETS", "collect_max_tweets", int, "1ジャンルの取得上限", 1),
    ("COLLECT_HOURS", "collect_hours", float, "収集対象の直近時間(h)", 1),
    ("COLLECT_MIN_FAVES", "collect_min_faves", int, "最低いいね数", 0),
    ("COLLECT_MIN_VIEWS_FLOOR", "collect_min_views_floor", int, "表示回数の下限(いいね下限の代替)", 0),
]


# --- APIキー (.key, 1行1キー・優先度順) ---

def read_keys() -> list[str]:
    """優先度順の APIキー配列(.key を 1行1キーで読む。xclient.load_keys の .key 部と同一)。"""
    if not KEY_FILE.exists():
        return []
    return xclient._split_keys(KEY_FILE.read_text(encoding="utf-8"))


def write_keys(keys: list[str]) -> None:
    text = "".join(k.strip() + "\n" for k in keys if k.strip())
    KEY_FILE.write_text(text, encoding="utf-8")
    try:
        KEY_FILE.chmod(0o600)
    except OSError:
        pass


def mask_key(k: str) -> str:
    """キーをマスク表示(先頭4 + 末尾4)。短いキーは全マスク。生値は画面に出さない。"""
    k = k.strip()
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"


# --- .env (収集パラメータ。他キー・コメント・秘密値は保持) ---

def _env_lines() -> list[str]:
    return ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []


def get_env_var(key: str) -> str | None:
    for line in _env_lines():
        s = line.strip()
        if s.startswith(f"{key}="):
            return s[len(key) + 1:]
    return None


def set_env_var(key: str, value: str) -> None:
    """.env の該当行だけ置換(無ければ追記)。他の行・コメントは変更しない。"""
    lines = _env_lines()
    out: list[str] = []
    found = False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def read_collect_params() -> dict[str, object]:
    """収集パラメータの現在値(.env にあればそれ、無ければ Settings の既定値)。"""
    out: dict[str, object] = {}
    for env, attr, cast, _label, _min in COLLECT_FIELDS:
        raw = get_env_var(env)
        if raw not in (None, ""):
            try:
                out[env] = cast(raw)
            except (TypeError, ValueError):
                out[env] = raw
        else:
            out[env] = Settings.model_fields[attr].default
    return out


# --- ジャンル (config/genres.toml 全文) ---

def read_genres_text() -> str:
    return GENRES_FILE.read_text(encoding="utf-8") if GENRES_FILE.exists() else ""


def validate_genres_toml(text: str) -> None:
    """TOML 構文 + genres.py._load 相当の検証。不正なら ValueError(保存しない)。"""
    data = tomllib.loads(text)  # 構文エラーは TOMLDecodeError(ValueError サブクラス)
    genres = data.get("genre", [])
    if not genres:
        raise ValueError("genre が1つもありません([[genre]] を最低1つ定義してください)。")
    seen: set[str] = set()
    for g in genres:
        key = g.get("key")
        if not key:
            raise ValueError("key の無い [[genre]] があります。")
        if key in seen:
            raise ValueError(f"key が重複しています: {key}")
        seen.add(key)


def write_genres_text(text: str) -> None:
    validate_genres_toml(text)  # 不正なら書かずに送出
    GENRES_FILE.write_text(text, encoding="utf-8")


# --- 配信時刻 (deliver.sh 定刻 + plist の15分前 + launchd 再登録) ---

def _validate_hhmm(hhmm: str) -> None:
    if not re.fullmatch(r"\d{4}", hhmm):
        raise ValueError(f"時刻は HHMM の4桁で指定してください: {hhmm!r}")
    h, m = int(hhmm[:2]), int(hhmm[2:])
    if h > 23 or m > 59:
        raise ValueError(f"時刻が不正です: {hhmm}")


def read_delivery_times() -> dict[str, str]:
    text = DELIVER_SH.read_text(encoding="utf-8") if DELIVER_SH.exists() else ""

    def find(var: str) -> str:
        m = re.search(rf'^{var}="(\d{{4}})"', text, re.M)
        return m.group(1) if m else ""

    return {"morning": find("MORNING_HHMM"), "evening": find("EVENING_HHMM")}


def _trigger_before(hhmm: str, minutes: int = 15) -> tuple[int, int]:
    """定刻の N 分前を (Hour, Minute) で返す(launchd 起動時刻)。"""
    total = (int(hhmm[:2]) * 60 + int(hhmm[2:]) - minutes) % (24 * 60)
    return total // 60, total % 60


def _set_deliver_sh(text: str, slot: str, hhmm: str) -> str:
    var = "MORNING_HHMM" if slot == "morning" else "EVENING_HHMM"
    new, n = re.subn(rf'^{var}="\d{{4}}"', f'{var}="{hhmm}"', text, flags=re.M)
    if n == 0:
        raise ValueError(f"{var} が deliver.sh に見つかりません。")
    return new


def _set_plist_trigger(text: str, slot: str, hour: int, minute: int) -> str:
    """plist の StartCalendarInterval(morning=index0 / evening=index1)の Hour/Minute を更新。
    コメントを保つため整数値のみ置換する。"""
    m = re.search(r"(<key>StartCalendarInterval</key>\s*<array>)(.*?)(</array>)", text, re.S)
    if not m:
        raise ValueError("plist に StartCalendarInterval が見つかりません。")
    head, body, tail = m.group(1), m.group(2), m.group(3)
    dicts = re.findall(r"<dict>.*?</dict>", body, re.S)
    idx = 0 if slot == "morning" else 1
    block = (
        "<dict>\n            <key>Hour</key><integer>{h}</integer>\n"
        "            <key>Minute</key><integer>{m}</integer>\n        </dict>"
    )
    while len(dicts) <= idx:
        dicts.append(block.format(h=0, m=0))
    d = dicts[idx]
    d = re.sub(r"(<key>Hour</key><integer>)\d+(</integer>)", rf"\g<1>{hour}\g<2>", d)
    d = re.sub(r"(<key>Minute</key><integer>)\d+(</integer>)", rf"\g<1>{minute}\g<2>", d)
    dicts[idx] = d
    new_body = "\n        " + "\n        ".join(dicts) + "\n    "
    return text[:m.start()] + head + new_body + tail + text[m.end():]


def reload_deliver_agent() -> None:
    """launchd の配信ジョブを bootout→bootstrap で再登録(plist 変更は kickstart では反映されない)。"""
    uid = os.getuid()
    subprocess.run(  # 未ロードでも失敗は無視(bootstrap で確実に登録する)
        ["launchctl", "bootout", f"gui/{uid}/{DELIVER_LABEL}"],
        capture_output=True, text=True,
    )
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(INSTALLED_PLIST)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap 失敗: {(r.stderr or r.stdout).strip()}")


# --- 今すぐ実行 (配信 / 更新。deliver.sh をバックグラウンド起動) ---

_RUN_MODES = {"now": "--now", "refresh": "--refresh"}


def launch_deliver(mode: str) -> None:
    """deliver.sh をバックグラウンド起動する(収集→キュレーション→[配信])。約10分かかるため
    Web リクエストをブロックせず投げっぱなしにする(進捗・結果は配信ログに残る)。
    mode='now'=定刻を待たず全購読者へ今すぐ配信(無料枠を消費) /
    mode='refresh'=配信せず収集・取り込みのみ(Web表示の更新だけ・無料)。"""
    flag = _RUN_MODES.get(mode)
    if flag is None:
        raise ValueError(f"不正なモードです: {mode}")
    if not DELIVER_SH.exists():
        raise FileNotFoundError(f"deliver.sh が見つかりません: {DELIVER_SH}")
    subprocess.Popen(
        ["/bin/bash", str(DELIVER_SH), flag],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def write_delivery_time(slot: str, hhmm: str) -> None:
    """配信時刻を変更: deliver.sh(定刻) + plist(15分前) を更新し launchd を再登録。
    途中で失敗したらファイルを変更前に戻し、可能なら配信ジョブも復旧する(ロールバック)。"""
    if slot not in ("morning", "evening"):
        raise ValueError(f"slot が不正です: {slot}")
    _validate_hhmm(hhmm)

    sh_before = DELIVER_SH.read_text(encoding="utf-8") if DELIVER_SH.exists() else None
    inst_before = INSTALLED_PLIST.read_text(encoding="utf-8") if INSTALLED_PLIST.exists() else None
    repo_before = REPO_PLIST.read_text(encoding="utf-8") if REPO_PLIST.exists() else None

    try:
        if sh_before is None:
            raise ValueError("deliver.sh が見つかりません。")
        DELIVER_SH.write_text(_set_deliver_sh(sh_before, slot, hhmm), encoding="utf-8")

        hour, minute = _trigger_before(hhmm)
        for path, before in ((INSTALLED_PLIST, inst_before), (REPO_PLIST, repo_before)):
            if before is None:
                continue
            new = _set_plist_trigger(before, slot, hour, minute)
            plistlib.loads(new.encode("utf-8"))  # 壊れていないか検証
            path.write_text(new, encoding="utf-8")

        reload_deliver_agent()
    except Exception:
        # ロールバック: ファイルを戻し、配信ジョブを復旧(best-effort)。
        if sh_before is not None:
            DELIVER_SH.write_text(sh_before, encoding="utf-8")
        if inst_before is not None:
            INSTALLED_PLIST.write_text(inst_before, encoding="utf-8")
        if repo_before is not None:
            REPO_PLIST.write_text(repo_before, encoding="utf-8")
        try:
            reload_deliver_agent()
        except Exception:
            pass
        raise
