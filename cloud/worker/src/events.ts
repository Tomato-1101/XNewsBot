// LINE イベントの処理。xnewsbot/api/line_webhook.py(_normalize)と xnewsbot/onboarding.py(handle_event)の移植。
// Python と変えた所: 今すぐ配信・AI解説は GitHub Actions を起動する / 「テスト」は kv の mock_specs を返す。
import { GENRE_KEYS, SELECTABLE_KEYS } from "./genres";
import { type Env, dispatch, fetchLimitUsed, memberCount, reply, replyMessages } from "./line";
import {
  type Item, SLOT_LABEL, type Spec, type Sub, detailSpec, feedbackSpec, genreSelectSpec, helpSpec, lineText,
  menuSpec, settingsSummaryText, textSpec, timeSelectSpec, titleHash, toMessage,
} from "./specs";

export interface Ev {
  kind: "join" | "follow" | "message" | "postback";
  line_user_id: string | null;
  text: string;
  data: string;
  reply_token: string;
  target_id: string | null;
}

type Row = Record<string, unknown>;

/** webhook の生イベントを正規化する(扱わない種別は null)。 */
export function normalize(raw: unknown): Ev | null {
  const ev = (raw ?? {}) as Record<string, any>;
  const src = ev.source ?? {};
  const uid: string | null = src.userId || null;
  const base = {
    line_user_id: uid, text: "", data: "", reply_token: ev.replyToken || "",
    target_id: (src.groupId || src.roomId || null) as string | null,
  };
  if (ev.type === "join") return { ...base, kind: "join" };
  const data: string = ev.type === "postback" ? (ev.postback?.data ?? "") : "";
  // 記事の評価だけは user_id が来ないグループのメンバーからも受ける(handleFeedback)
  if (!uid && ev.type === "postback" && (data.startsWith("rate:") || data.startsWith("fb:"))) {
    return { ...base, kind: "postback", data };
  }
  if (!uid) return null;
  if (ev.type === "follow") return { ...base, kind: "follow" };
  if (ev.type === "message" && ev.message?.type === "text") {
    return { ...base, kind: "message", text: ev.message.text ?? "" };
  }
  if (ev.type === "postback") return { ...base, kind: "postback", data };
  return null;
}

// ---------------------------------------------------------------- 時刻

const pad2 = (n: number) => String(n).padStart(2, "0");

/** DB の DATETIME 列の形(SQLAlchemy が書く naive UTC「YYYY-MM-DD HH:MM:SS.ffffff」。文字列順で比べられる)。 */
export function dbTime(d: Date): string {
  return d.toISOString().replace("T", " ").replace("Z", "000");
}

function localNow(d: Date, tz: string): { date: string; year: number; month: number; day: number; minutes: number } {
  const p: Record<string, number> = {};
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric",
  });
  for (const part of fmt.formatToParts(d)) p[part.type] = Number(part.value);
  return {
    date: `${String(p.year).padStart(4, "0")}-${pad2(p.month)}-${pad2(p.day)}`,
    year: p.year, month: p.month, day: p.day, minutes: p.hour * 60 + p.minute,
  };
}

const asciiDigits = (s: string) => s.replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));
// Python の \d は全角数字にも一致するので合わせる
const TIME_COLON = /^\s*([0-9０-９]{1,2})\s*[:：時]\s*([0-9０-９]{1,2})?\s*分?\s*$/;
const TIME_4DIGIT = /^\s*([0-9０-９]{2})([0-9０-９]{2})\s*$/;

/** 「7:30」「7時30分」「0730」等を [hour, minute] に。範囲外は null。 */
export function parseTime(text: string): [number, number] | null {
  const m = TIME_COLON.exec(text) ?? TIME_4DIGIT.exec(text);
  if (!m) return null;
  const h = Number(asciiDigits(m[1]));
  const mi = Number(asciiDigits(m[2] ?? "0"));
  return h >= 0 && h <= 23 && mi >= 0 && mi <= 59 ? [h, mi] : null;
}

/** キーの先頭 "YYYYMMDD" を DB の DATE 列の形に。日付として不正なら null。 */
function parseYmd(ymd: string): string | null {
  const parts = [ymd.slice(0, 4), ymd.slice(4, 6), ymd.slice(6, 8)];
  if (!parts.every((p) => /^[0-9]+$/.test(p))) return null;
  const [y, m, d] = parts.map(Number);
  const dt = new Date(Date.UTC(2000, m - 1, d));
  dt.setUTCFullYear(y);
  if (y < 1 || dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) return null;
  return `${String(y).padStart(4, "0")}-${pad2(m)}-${pad2(d)}`;
}

// ---------------------------------------------------------------- DB

function jsonList<T>(v: unknown): T[] {
  if (typeof v !== "string") return [];
  try {
    const out = JSON.parse(v);
    return Array.isArray(out) ? out : [];
  } catch {
    return [];
  }
}

function toSub(r: Row): Sub {
  return {
    id: r.id as number,
    line_user_id: r.line_user_id as string,
    enabled_genres: jsonList<string>(r.enabled_genres),
    pending_genres: jsonList<string>(r.pending_genres),
    morning_hour: r.morning_hour as number,
    morning_minute: r.morning_minute as number,
    evening_hour: r.evening_hour as number,
    evening_minute: r.evening_minute as number,
    morning_enabled: !!r.morning_enabled,
    evening_enabled: !!r.evening_enabled,
    tz: r.tz as string,
    onboarding_step: r.onboarding_step as string,
    is_onboarded: !!r.is_onboarded,
    last_morning_on: (r.last_morning_on as string | null) ?? null,
    push_to: (r.push_to as string | null) || null,
  };
}

function toItem(r: Row): Item {
  return {
    id: r.id as number,
    genre_digest_id: r.genre_digest_id as number,
    genre: (r.genre as string) ?? "",
    genres: jsonList<string>(r.genres),
    title: (r.title as string) ?? "",
    summary: (r.summary as string) ?? "",
    detail: (r.detail as string) ?? "",
    source_urls: jsonList<string>(r.source_urls),
    source_tweets: jsonList<Record<string, unknown>>(r.source_tweets),
  };
}

async function getOrCreateSubscriber(db: D1Database, uid: string, now: Date): Promise<Sub> {
  // 同じ人のイベントが同時に来ても行が2つできないよう、一意制約に任せて入れてから読む
  await db.prepare(
    `INSERT INTO subscriber (line_user_id, enabled_genres, pending_genres, morning_hour, morning_minute,
       evening_hour, evening_minute, morning_enabled, evening_enabled, tz, onboarding_step, is_onboarded, created_at)
     VALUES (?, '[]', '[]', 8, 0, 21, 0, 1, 1, 'Asia/Tokyo', 'genres', 0, ?)
     ON CONFLICT(line_user_id) DO NOTHING`,
  ).bind(uid, dbTime(now)).run();
  const row = await db.prepare("SELECT * FROM subscriber WHERE line_user_id = ?").bind(uid).first<Row>();
  return toSub(row!);
}

/** Worker が持ち主の列だけを書く(last_morning_on/last_evening_on は Actions のもの。cloud/DESIGN.md)。 */
async function saveSub(db: D1Database, sub: Sub): Promise<void> {
  await db.prepare(
    `UPDATE subscriber SET enabled_genres = ?, pending_genres = ?, morning_hour = ?, morning_minute = ?,
       evening_hour = ?, evening_minute = ?, onboarding_step = ?, is_onboarded = ?, push_to = ? WHERE id = ?`,
  ).bind(
    JSON.stringify(sub.enabled_genres), JSON.stringify(sub.pending_genres), sub.morning_hour, sub.morning_minute,
    sub.evening_hour, sub.evening_minute, sub.onboarding_step, sub.is_onboarded ? 1 : 0, sub.push_to, sub.id,
  ).run();
}

async function digestId(db: D1Database, genre: string, date: string, slot: string): Promise<number | null> {
  return db.prepare("SELECT id FROM genredigest WHERE digest_date = ? AND slot = ? AND genre = ? LIMIT 1")
    .bind(date, slot, genre).first<number>("id");
}

/** 詳細・AI解説のキー: 安定キー "YYYYMMDD:slot:genre:rank" か、整数 id(履歴に残る旧ボタン)。 */
async function resolveDetailItem(db: D1Database, payload: string): Promise<Item | null> {
  payload = (payload || "").trim();
  if (/^[0-9]+$/.test(payload)) {
    const row = await db.prepare("SELECT * FROM newsitem WHERE id = ?").bind(Number(payload)).first<Row>();
    return row ? toItem(row) : null;
  }
  const parts = payload.split(":");
  if (parts.length !== 4) return null;
  const [ymd, slot, genre, rankS] = parts;
  const date = parseYmd(ymd);
  if (date === null || !/^\s*[+-]?[0-9]+\s*$/.test(rankS)) return null;
  const dg = await digestId(db, genre, date, slot);
  if (dg === null) return null;
  const row = await db.prepare("SELECT * FROM newsitem WHERE genre_digest_id = ? AND rank = ? LIMIT 1")
    .bind(dg, Number(rankS)).first<Row>();
  return row ? toItem(row) : null;
}

/** 評価のキー "YYYYMMDD:slot:genre:<見出しハッシュ8桁>"。rank は使わない(取り込み直しで同じ rank が別の記事を指すため)。 */
async function resolveFeedbackItem(db: D1Database, key: string): Promise<Item | null> {
  const parts = (key || "").trim().split(":");
  if (parts.length !== 4) return null;
  const [ymd, slot, genre, h] = parts;
  const date = parseYmd(ymd);
  if (date === null) return null;
  const dg = await digestId(db, genre, date, slot);
  if (dg === null) return null;
  const { results } = await db.prepare("SELECT * FROM newsitem WHERE genre_digest_id = ? ORDER BY rank")
    .bind(dg).all<Row>();
  for (const r of results) {
    if ((await titleHash((r.title as string) ?? "")) === h) return toItem(r);
  }
  return null;
}

const MOCK_DATE = "2000-01-01";

async function digestDate(db: D1Database, item: Item): Promise<string | null> {
  return db.prepare("SELECT digest_date FROM genredigest WHERE id = ?").bind(item.genre_digest_id)
    .first<string>("digest_date");
}

// ---------------------------------------------------------------- 入口

const BIND_WORDS = ["このグループに配信", "ここに配信", "グループ配信", "ここに配信して"];
const UNBIND_WORDS = ["個別に配信", "個別配信", "1対1に配信", "個人に配信"];
const DELIVER_WORDS = ["今すぐ", "今すぐ配信", "最新", "最新ニュース", "配信", "ニュース配信"];
const MOCK_WORDS = ["テスト", "test", "てすと", "モック", "mock", "サンプル", "レイアウト"];
const CANCEL_WORDS = ["キャンセル", "やめる", "中止", "cancel", "やめ"];

interface Ctx {
  env: Env;
  db: D1Database;
  token: string;
  now: Date;
  say: (...specs: Spec[]) => Promise<void>;
}

export async function handleEvent(env: Env, ev: Ev, now: Date = new Date()): Promise<void> {
  const c: Ctx = {
    env, db: env.DB, token: ev.reply_token, now,
    say: (...specs) => reply(env, ev.reply_token, specs),
  };

  if (ev.kind === "join") {
    await c.say(textSpec(
      "グループに追加ありがとうございます。\n" +
        "このトークに毎日のニュースを配信するには、受け取りたい方が" +
        "(まず XNewsBot と1:1で初期設定を済ませたうえで)ここで「このグループに配信」と送ってください。"));
    return;
  }

  const uid = ev.line_user_id;
  if (ev.kind === "postback" && (ev.data.startsWith("rate:") || ev.data.startsWith("fb:"))) {
    // 評価はグループ全員の好みとしてまとめて使うので、配信先のグループのメンバーなら購読者でなくても受ける
    // (user_id が来ないメンバーは "group")。配信先でないグループ・購読者でない人の 1:1 は黙って無視する。
    if (await feedbackAllowed(c.db, ev.target_id, uid)) await handleFeedback(c, ev.data, uid || "group");
    return;
  }
  if (!uid) return;
  const sub = await getOrCreateSubscriber(c.db, uid, now);

  if (ev.kind === "follow") await startOnboarding(c, sub);
  else if (ev.kind === "message") await handleMessage(c, sub, ev.text, ev.target_id);
  else if (ev.kind === "postback") await handlePostback(c, sub, ev.data, ev.target_id);
}

async function startOnboarding(c: Ctx, sub: Sub): Promise<void> {
  sub.onboarding_step = "genres";
  sub.pending_genres = sub.is_onboarded ? [...sub.enabled_genres] : [];
  await saveSub(c.db, sub);
  await c.say(
    textSpec("友だち追加ありがとうございます！\nXから毎日その日のニュースをお届けします。\nまず受け取るジャンルを選んでください。"),
    genreSelectSpec(sub.pending_genres),
  );
}

/** その購読者へ「今すぐ」配信する(収集→キュレーション→送信は GitHub Actions)。 */
async function deliverNow(c: Ctx, sub: Sub): Promise<void> {
  await dispatch(c.env, "deliver.yml", { mode: "user", user: sub.line_user_id });
}

async function replyMock(c: Ctx): Promise<void> {
  // レイアウト確認用のサンプルは Actions が Python で描いて kv に入れてある(Worker では描かない)
  const raw = await c.db.prepare("SELECT value FROM kv WHERE key = 'mock_specs'").first<string>("value");
  let messages: Record<string, any>[] = [];
  try {
    const parsed = JSON.parse(raw ?? "[]");
    if (Array.isArray(parsed)) messages = parsed;
  } catch {
    // 壊れた値は「無い」のと同じ扱い
  }
  if (!messages.length) {
    await c.say(textSpec("サンプルは準備中です。"));
    return;
  }
  // spec の形(alt / quick_reply)で入っていても LINE のメッセージの形でも返せるようにする
  await replyMessages(c.env, c.token, messages.map((m) => ("alt" in m || "quick_reply" in m ? toMessage(m as Spec) : m)));
}

/** 1:1でもグループでも効く明示コマンド。処理したら true(グループではこれだけに応答する)。 */
async function tryCommand(c: Ctx, sub: Sub, text: string): Promise<boolean> {
  const low = text.toLowerCase();
  if (MOCK_WORDS.includes(low)) {
    await replyMock(c);
    return true;
  }
  if (DELIVER_WORDS.includes(text)) {
    await c.say(textSpec(
      "最新のニュースを今すぐお送りします。収集とキュレーションに10分ほどかかります。" +
        "できあがり次第このトークにお届けします。"));
    await deliverNow(c, sub);
    return true;
  }
  if (text === "メニュー" || low === "menu") {
    await c.say(menuSpec("メニューです。操作を選んでください。"));
    return true;
  }
  if (text === "ヘルプ" || text === "使い方" || low === "help") {
    await c.say(helpSpec());
    return true;
  }
  return false;
}

async function handleMessage(c: Ctx, sub: Sub, text: string, targetId: string | null): Promise<void> {
  text = (text || "").trim();

  if (BIND_WORDS.includes(text)) {
    if (targetId) {
      sub.push_to = targetId;
      await saveSub(c.db, sub);
      await c.say(textSpec("これ以降、ニュースはこのトークに配信します。\n(個別トークに戻すには「個別に配信」と送ってください)"));
    } else {
      await c.say(textSpec("ここは1:1トークです。配信先にしたいグループ/複数人トークで「このグループに配信」と送ってください。"));
    }
    return;
  }
  if (UNBIND_WORDS.includes(text)) {
    sub.push_to = null;
    await saveSub(c.db, sub);
    await c.say(textSpec("配信先を個別トークに戻しました。"));
    return;
  }

  // グループ/ルームは明示コマンドだけ。オンボーディングは1:1専用、雑談には無反応
  if (targetId) {
    await tryCommand(c, sub, text);
    return;
  }

  if (CANCEL_WORDS.includes(text)) {
    await doCancel(c, sub);
    return;
  }

  if (sub.onboarding_step === "morning" || sub.onboarding_step === "evening") {
    const slot = sub.onboarding_step;
    const t = parseTime(text);
    if (t) await applySlotTime(c, sub, slot, t[0], t[1]);
    else await c.say(textSpec("時刻が読み取れません。「7:30」の形式で送ってください。"), timeSelectSpec(slot));
    return;
  }

  if (sub.onboarding_step === "genres" && !sub.is_onboarded) {
    await c.say(textSpec("下のボタンからジャンルを選んでください。"), genreSelectSpec(sub.pending_genres));
    return;
  }

  if (await tryCommand(c, sub, text)) return;
  await c.say(textSpec("メニューから操作できます。"), menuSpec("メニュー"));
}

const afterColon = (s: string) => s.slice(s.indexOf(":") + 1);

async function handlePostback(c: Ctx, sub: Sub, data: string, targetId: string | null): Promise<void> {
  if (data.startsWith("genre:")) {
    const key = afterColon(data);
    if (SELECTABLE_KEYS.includes(key)) {
      const pending = [...sub.pending_genres];
      const i = pending.indexOf(key);
      if (i >= 0) pending.splice(i, 1);
      else pending.push(key);
      sub.pending_genres = pending;
      await saveSub(c.db, sub);
    }
    await c.say(genreSelectSpec(sub.pending_genres));
  } else if (data === "genre_all") {
    sub.pending_genres = [...SELECTABLE_KEYS];
    await saveSub(c.db, sub);
    await c.say(genreSelectSpec(sub.pending_genres));
  } else if (data === "genre_done") {
    if (!sub.pending_genres.length) {
      await c.say(textSpec("少なくとも1つのジャンルを選んでください。"), genreSelectSpec(sub.pending_genres));
      return;
    }
    // 表示順を GENRE_KEYS に合わせて確定
    sub.enabled_genres = GENRE_KEYS.filter((g) => sub.pending_genres.includes(g));
    if (sub.is_onboarded) {
      sub.onboarding_step = "done";
      await saveSub(c.db, sub);
      await c.say(textSpec("ジャンルを変更しました。\n" + settingsSummaryText(sub)), menuSpec("メニュー"));
    } else {
      sub.onboarding_step = "morning";
      await saveSub(c.db, sub);
      await c.say(textSpec("ジャンルを設定しました: " + sub.enabled_genres.join(" / ")), timeSelectSpec("morning"));
    }
  } else if (data.startsWith("time:")) {
    // 時刻設定中以外で、履歴に残った古い時刻ボタンを押されても時刻を書き換えない
    if (sub.onboarding_step !== "morning" && sub.onboarding_step !== "evening") {
      await c.say(menuSpec("メニュー"));
      return;
    }
    const hhmm = afterColon(data);
    const slot = sub.onboarding_step;
    const h = Number(hhmm.slice(0, 2));
    const m = Number(hhmm.slice(2));
    if (/^[0-9]{4}$/.test(hhmm) && h < 24 && m < 60) await applySlotTime(c, sub, slot, h, m);
    else await c.say(menuSpec("メニュー")); // 旧形式など想定外の data でも無反応にしない
  } else if (data === "genre_edit") {
    sub.onboarding_step = "genres";
    sub.pending_genres = [...sub.enabled_genres];
    await saveSub(c.db, sub);
    await c.say(genreSelectSpec(sub.pending_genres));
  } else if (data === "morning_edit" || data === "evening_edit") {
    const slot = data === "morning_edit" ? "morning" : "evening";
    sub.onboarding_step = slot;
    await saveSub(c.db, sub);
    await c.say(timeSelectSpec(slot));
  } else if (data === "show_settings") {
    await c.say(textSpec(settingsSummaryText(sub)), menuSpec("メニュー"));
  } else if (data === "help") {
    await c.say(helpSpec());
  } else if (data === "deliver_now") {
    await c.say(textSpec("今日のニュースをお送りします。収集とキュレーションに10分ほどかかります。できあがり次第お届けします。"));
    await deliverNow(c, sub);
  } else if (data === "cancel") {
    await doCancel(c, sub);
  } else if (data.startsWith("detail:")) {
    await handleDetail(c, afterColon(data));
  } else if (data.startsWith("explain:")) {
    // 押されたトーク(グループならグループ)へ解説を届ける
    await handleExplain(c, sub, afterColon(data), targetId || sub.line_user_id);
  } else {
    await c.say(menuSpec("メニュー"));
  }
}

const NOT_FOUND_MSG = "この記事は見つかりませんでした(配信が更新された可能性があります)。";

async function handleDetail(c: Ctx, payload: string): Promise<void> {
  const item = await resolveDetailItem(c.db, payload);
  if (!item) {
    await c.say(textSpec(NOT_FOUND_MSG));
    return;
  }
  await c.say(await detailSpec(item, payload.trim()));
}

async function doCancel(c: Ctx, sub: Sub): Promise<void> {
  if (sub.is_onboarded) {
    sub.pending_genres = [...sub.enabled_genres]; // 編集中の選択を破棄
    sub.onboarding_step = "done";
    await saveSub(c.db, sub);
    await c.say(textSpec("操作をキャンセルしました。設定は変更していません。"), menuSpec("メニュー"));
  } else {
    await c.say(textSpec("設定を中断しました。続けるときは何かメッセージを送ってください。"));
  }
}

async function applySlotTime(c: Ctx, sub: Sub, slot: string, hour: number, minute: number): Promise<void> {
  if (slot === "morning") [sub.morning_hour, sub.morning_minute] = [hour, minute];
  else [sub.evening_hour, sub.evening_minute] = [hour, minute];
  const hhmm = `${pad2(hour)}:${pad2(minute)}`;

  if (sub.is_onboarded) {
    sub.onboarding_step = "done";
    await saveSub(c.db, sub);
    await c.say(
      textSpec(`${SLOT_LABEL[slot]}の配信時刻を ${hhmm} に変更しました。\n` + settingsSummaryText(sub)),
      menuSpec("メニュー"),
    );
    return;
  }
  if (slot === "morning") {
    sub.onboarding_step = "evening";
    await saveSub(c.db, sub);
    await c.say(textSpec(`朝の配信時刻を ${hhmm} にしました。\n次に夜の配信時刻を選んでください。`), timeSelectSpec("evening"));
  } else {
    sub.is_onboarded = true;
    sub.onboarding_step = "done";
    await saveSub(c.db, sub);
    await c.say(
      textSpec("設定が完了しました！\n" + settingsSummaryText(sub) +
        "\n\n上の設定した時間にニュースをお届けします。「今すぐ配信」で今すぐ試せます。"),
      menuSpec("メニュー"),
    );
  }
}

// ---------------------------------------------------------------- 評価

const VERDICT_LABELS: Record<string, string> = { good: "興味あり", bad: "興味なし" };

/** 評価を受け付ける送り主か: 配信先のグループ/ルーム、または 1:1 でオンボーディング済みの購読者。 */
async function feedbackAllowed(db: D1Database, targetId: string | null, uid: string | null): Promise<boolean> {
  if (targetId) {
    return (await db.prepare("SELECT 1 AS x FROM subscriber WHERE push_to = ? LIMIT 1").bind(targetId).first()) !== null;
  }
  if (!uid) return false;
  return (await db.prepare("SELECT 1 AS x FROM subscriber WHERE line_user_id = ? AND is_onboarded = 1 LIMIT 1")
    .bind(uid).first()) !== null;
}

async function recordFeedback(db: D1Database, item: Item, date: string | null, userId: string, verdict: string, now: Date) {
  const t = dbTime(now);
  // 記事 id は取り込み直しで別の記事に使い回されうるので、見出しも一致した行だけを同じ記事として上書きする
  const res = await db.prepare(
    "UPDATE newsfeedback SET verdict = ?, updated_at = ? WHERE newsitem_id = ? AND user_id = ? AND title = ?",
  ).bind(verdict, t, item.id, userId, item.title).run();
  if (res.meta.changes) return;
  await db.prepare(
    `INSERT INTO newsfeedback (newsitem_id, user_id, verdict, genre, title, digest_date, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).bind(item.id, userId, verdict, item.genre, item.title, date, t, t).run();
}

async function handleFeedback(c: Ctx, data: string, userId: string): Promise<void> {
  const kind = data.slice(0, data.indexOf(":"));
  let rest = afterColon(data);
  let verdict = "";
  if (kind === "fb") {
    const i = rest.lastIndexOf(":");
    verdict = rest.slice(i + 1);
    rest = i < 0 ? "" : rest.slice(0, i);
  }
  const item = await resolveFeedbackItem(c.db, rest);
  if (!item) {
    await c.say(textSpec("この記事は見つかりませんでした（配信し直されたため評価できません）"));
    return;
  }
  const date = await digestDate(c.db, item);
  if (date === MOCK_DATE) {
    await c.say(textSpec("サンプル記事なので評価は記録されません"));
    return;
  }
  if (kind === "rate") {
    await c.say(feedbackSpec(item.title, rest.trim()));
    return;
  }
  if (!Object.hasOwn(VERDICT_LABELS, verdict)) {
    await c.say(menuSpec("メニュー"));
    return;
  }
  try {
    await recordFeedback(c.db, item, date, userId, verdict, c.now);
  } catch (e) {
    await c.say(textSpec("記録に失敗しました。時間をおいてもう一度押してください")); // 無応答にしない
    throw e;
  }
  await c.say(textSpec(`${VERDICT_LABELS[verdict]} として記録しました`));
}

// ---------------------------------------------------------------- AI解説

const EXPLAIN_RUNNING_MSG = "いま作成中です。届かないときは、少し後にもう一度押すとすぐ表示します。";
const EXPLAIN_BUSY_MSG = "いまほかのAI解説を作っています。少し後にもう一度押してください。";
const STALE_AFTER_MS = 15 * 60_000; // running がこれより古ければ(Actions が落ちた等)取り直せる
const MAX_RUNNING = 2; // 同時に作る数の上限(サブスク枠を食い尽くさない)
const QUIET_START = 7 * 60; // 朝のキュレーションの時間帯(7:00〜8:30)は新しく作らない
const QUIET_END = 8 * 60 + 30;

/** "none" | "running" | "done" | "failed"。見出しが違う行(記事 id の使い回し)は無いものとし、失効した running は failed。 */
async function explainStatus(db: D1Database, item: Item, stale: string): Promise<{ status: string; text: string }> {
  const row = await db.prepare("SELECT status, text, title, updated_at FROM newsexplanation WHERE news_item_id = ?")
    .bind(item.id).first<Row>();
  if (!row || row.title !== item.title) return { status: "none", text: "" };
  if (row.status === "running" && (row.updated_at as string) < stale) return { status: "failed", text: "" };
  return { status: row.status as string, text: (row.text as string) ?? "" };
}

/** 月末までにこの購読者へ残っている朝の配信回数(今日の分が配信済みなら今日は数えない)。 */
function runsLeft(sub: Sub, now: Date): number {
  const t = localNow(now, sub.tz);
  const daysInMonth = new Date(Date.UTC(t.year, t.month, 0)).getUTCDate();
  return daysInMonth - t.day + 1 - (sub.last_morning_on === t.date ? 1 : 0);
}

/** 朝の配信を受ける購読者(初期設定済み・朝が有効・ジャンルあり)。 */
async function morningSubscribers(db: D1Database): Promise<Sub[]> {
  const { results } = await db.prepare("SELECT * FROM subscriber WHERE is_onboarded = 1").all<Row>();
  return results.map(toSub).filter((s) => s.morning_enabled && s.enabled_genres.length > 0);
}

/** 朝の配信以外の push を to へ送る判定の材料。1つでも取れなければ null(送らない側に倒す)。 */
async function fetchExtraPush(c: Ctx, to: string): Promise<{ remaining: number; need: number; cost: number } | null> {
  const lu = await fetchLimitUsed(c.env);
  if (!lu) return null;
  const subs = await morningSubscribers(c.db);
  const costs = new Map<string, number>();
  for (const t of [to, ...subs.map((s) => s.push_to || s.line_user_id)]) {
    if (costs.has(t)) continue;
    const n = await memberCount(c.env, t);
    if (n === null) return null;
    costs.set(t, n);
  }
  const need = subs.reduce((sum, s) => sum + runsLeft(s, c.now) * costs.get(s.push_to || s.line_user_id)!, 0);
  return { remaining: Math.max(lu.limit - lu.used, 0), need, cost: costs.get(to)! };
}

async function handleExplain(c: Ctx, sub: Sub, payload: string, to: string): Promise<void> {
  const say = (text: string) => c.say(textSpec(text));
  const item = await resolveDetailItem(c.db, payload);
  if (!item) return say(NOT_FOUND_MSG);
  if ((await digestDate(c.db, item)) === MOCK_DATE) {
    return say("この記事はレイアウト確認用のサンプル(架空の内容)なので、AI解説は作れません。");
  }
  const nowS = dbTime(c.now);
  const stale = dbTime(new Date(c.now.getTime() - STALE_AFTER_MS));
  const st = await explainStatus(c.db, item, stale);
  if (st.status === "done") return say(lineText(item.title, st.text));
  if (st.status === "running") return say(EXPLAIN_RUNNING_MSG);

  const minutes = localNow(c.now, sub.tz).minutes;
  if (minutes >= QUIET_START && minutes < QUIET_END) {
    return say("朝の配信の準備中(7:00〜8:30)はAI解説を受け付けていません。8:30以降にもう一度押してください。");
  }
  // 通数の API を叩く前に断れる分は断る
  const running = await c.db.prepare("SELECT COUNT(*) AS n FROM newsexplanation WHERE status = 'running' AND updated_at >= ?")
    .bind(stale).first<number>("n");
  if ((running ?? 0) >= MAX_RUNNING) return say(EXPLAIN_BUSY_MSG);

  const q = await fetchExtraPush(c, to);
  if (!q) {
    return say("LINEの送信枠の残りを確認できなかったため、朝の配信を守るためにAI解説は使えません。" +
      "時間をおいてもう一度お試しください。");
  }

  // 確保は1文で行う。「同時に作る数」と「作成中の分を引いた通数の残り」の判定を同じ文に入れるので、
  // 同時に押されても二重に確保されず、数え漏らしも起きない(D1 の書き込みは直列)。
  // 行が無ければ INSERT、あれば failed・失効した running・見出しが違う(記事 id の使い回し)ときだけ取り直す。
  const token = crypto.randomUUID().replaceAll("-", "");
  const id = await c.db.prepare(
    `INSERT INTO newsexplanation (news_item_id, status, text, error, claim_token, created_at, updated_at, title, push_to, push_cost)
     SELECT ?1, 'running', '', '', ?2, ?3, ?3, ?4, ?5, ?6
     WHERE (SELECT COUNT(*) FROM newsexplanation
            WHERE status = 'running' AND updated_at >= ?7 AND news_item_id != ?1) < ?8
       AND ?9 - (SELECT COALESCE(SUM(push_cost), 0) FROM newsexplanation
                 WHERE status = 'running' AND updated_at >= ?7) - ?6 >= ?10
     ON CONFLICT(news_item_id) DO UPDATE SET
       status = 'running', text = '', error = '', claim_token = excluded.claim_token,
       created_at = excluded.created_at, updated_at = excluded.updated_at,
       title = excluded.title, push_to = excluded.push_to, push_cost = excluded.push_cost
     WHERE newsexplanation.status = 'failed'
        OR (newsexplanation.status = 'running' AND newsexplanation.updated_at < ?7)
        OR newsexplanation.title != excluded.title
     RETURNING id`,
  ).bind(item.id, token, nowS, item.title, to, q.cost, stale, MAX_RUNNING, q.remaining, q.need).first<number>("id");

  if (id === null) {
    const reserved = (await c.db.prepare(
      "SELECT COALESCE(SUM(push_cost), 0) AS n FROM newsexplanation WHERE status = 'running' AND updated_at >= ?",
    ).bind(stale).first<number>("n")) ?? 0;
    if (q.remaining - reserved - q.cost < q.need) {
      return say("今月のLINEの送信枠が残り少ないため、朝の配信を守るためにAI解説は使えません" +
        `（残り${q.remaining - reserved}通・月末までの朝配信に${q.need}通必要）。`);
    }
    // 確認の直後に別の押下が先に確保した / 同時に作れる数の上限
    return say((await explainStatus(c.db, item, stale)).status === "running" ? EXPLAIN_RUNNING_MSG : EXPLAIN_BUSY_MSG);
  }

  if (!(await dispatch(c.env, "explain.yml", { explanation_id: String(id) }))) {
    // 起動できなかった確保を残すと、15分のあいだ押し直せず通数も押さえたままになるので手放す
    await c.db.prepare(
      "UPDATE newsexplanation SET status = 'failed', error = ?, updated_at = ? WHERE id = ? AND claim_token = ? AND status = 'running'",
    ).bind("AI解説の起動に失敗しました", nowS, id, token).run();
    return say("いまはAI解説を使えません。");
  }
  return say("AI解説を作っています。1〜2分お待ちください。");
}

// ---------------------------------------------------------------- Cron

/** 当日の朝の分がまだ届いていない購読者がいるか(scripts/pipeline.py の pending と同じ条件)。 */
export async function hasPendingMorning(db: D1Database, now: Date): Promise<boolean> {
  return (await morningSubscribers(db)).some((s) => s.last_morning_on !== localNow(now, s.tz).date);
}
