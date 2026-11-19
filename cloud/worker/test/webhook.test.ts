// webhook 全体(署名検証 → 正規化 → 各イベント・コマンド・postback)と Cron を、
// 本物の SQLite(D1 の代役)と fetch のモックで確かめる。LINE・GitHub の本物の API は呼ばない。
import { createHmac } from "node:crypto";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { dbTime, hasPendingMorning, normalize } from "../src/events";
import worker from "../src/index";
import { type Env, clearMemberCache } from "../src/line";
import { titleHash } from "../src/specs";
import { type TestDB, makeDB } from "./d1";

const SECRET = "test-secret";
const U = "Uaaaa";
const G = "Cgroup";
// 既定の「今」: 2026-11-19 12:00 JST(静かな時間帯の外)
const NOON = new Date("2026-11-19T03:00:00Z");

let db: TestDB;
let env: Env;
let calls: { url: string; init: RequestInit; body: any }[];
let line: { quota: any; used: any; members: Record<string, number | null>; replyStatus: number };
let githubStatus: number;

beforeEach(() => {
  db = makeDB();
  env = { DB: db.d1, LINE_CHANNEL_SECRET: SECRET, LINE_CHANNEL_ACCESS_TOKEN: "line-token", GITHUB_DISPATCH_TOKEN: "gh-token" };
  calls = [];
  line = { quota: { type: "limited", value: 200 }, used: { totalUsage: 50 }, members: { [G]: 3 }, replyStatus: 200 };
  githubStatus = 204;
  clearMemberCache();
  vi.useFakeTimers({ now: NOON, toFake: ["Date"] });
  vi.spyOn(console, "error").mockImplementation(() => {});
  vi.spyOn(console, "warn").mockImplementation(() => {});
  vi.stubGlobal("fetch", async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init, body: init.body ? JSON.parse(init.body as string) : null });
    const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
    if (url === "https://api.line.me/v2/bot/message/reply") return json({}, line.replyStatus);
    if (url === "https://api.line.me/v2/bot/message/quota") return line.quota ? json(line.quota) : json({}, 500);
    if (url === "https://api.line.me/v2/bot/message/quota/consumption") return line.used ? json(line.used) : json({}, 500);
    const m = /^https:\/\/api\.line\.me\/v2\/bot\/(group|room)\/([^/]+)\/members\/count$/.exec(url);
    if (m) return line.members[m[2]] == null ? json({}, 500) : json({ count: line.members[m[2]] });
    if (url.startsWith("https://api.github.com/")) return new Response(null, { status: githubStatus });
    throw new Error(`想定外の fetch: ${url}`);
  });
});

// ---------------------------------------------------------------- 補助

const sign = (body: string) => createHmac("sha256", SECRET).update(body).digest("base64");

async function post(events: unknown[], opts: { signature?: string; path?: string; method?: string } = {}) {
  const body = JSON.stringify({ destination: "x", events });
  return worker.fetch(
    new Request(`https://w.example${opts.path ?? "/line/callback"}`, {
      method: opts.method ?? "POST",
      headers: { "X-Line-Signature": opts.signature ?? sign(body) },
      body: opts.method === "GET" ? undefined : body,
    }) as any,
    env,
  );
}

const src = (uid: string | null, gid?: string) =>
  gid ? { type: "group", groupId: gid, ...(uid ? { userId: uid } : {}) } : { type: "user", userId: uid };
const msg = (text: string, uid: string | null = U, gid?: string) =>
  ({ type: "message", replyToken: "rt", source: src(uid, gid), message: { type: "text", text } });
const pb = (data: string, uid: string | null = U, gid?: string) =>
  ({ type: "postback", replyToken: "rt", source: src(uid, gid), postback: { data } });
const follow = (uid = U) => ({ type: "follow", replyToken: "rt", source: src(uid) });

const replies = () => calls.filter((c) => c.url.endsWith("/message/reply"));
const dispatches = () => calls.filter((c) => c.url.startsWith("https://api.github.com/"));
/** 直近の reply のメッセージ(テキストは text、Flex は altText)。 */
const lastTexts = () => (replies().at(-1)?.body.messages ?? []).map((m: any) => m.text ?? m.altText);
const lastQR = (i = -1) =>
  (replies().at(-1)!.body.messages.at(i).quickReply?.items ?? []).map((q: any) => q.action.data);
const sub = (uid = U) => db.rows("SELECT * FROM subscriber WHERE line_user_id = ?", uid)[0];

/** 初期設定済みの購読者を作る。 */
function seedSub(uid = U, over: Record<string, unknown> = {}) {
  const row = {
    line_user_id: uid, enabled_genres: '["AI","株"]', pending_genres: '["AI","株"]', morning_hour: 8, morning_minute: 0,
    evening_hour: 21, evening_minute: 0, morning_enabled: 1, evening_enabled: 1, tz: "Asia/Tokyo",
    onboarding_step: "done", is_onboarded: 1, last_morning_on: "2026-11-19", push_to: null,
    created_at: "2026-11-01 00:00:00.000000", ...over,
  };
  const cols = Object.keys(row);
  db.raw.prepare(`INSERT INTO subscriber (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})`)
    .run(...(Object.values(row) as any[]));
}

let nextDigest = 1;
/** 記事を1本入れて id を返す。 */
function seedItem(over: Record<string, unknown> = {}, date = "2026-11-19", genre = "AI"): number {
  let dg = db.rows("SELECT id FROM genredigest WHERE digest_date = ? AND slot = 'morning' AND genre = ?", date, genre)[0]?.id;
  if (!dg) {
    dg = nextDigest++;
    db.raw.prepare("INSERT INTO genredigest (id, digest_date, slot, genre, created_at) VALUES (?, ?, 'morning', ?, ?)")
      .run(dg, date, genre, "2026-11-19 00:00:00.000000");
  }
  const row = {
    genre_digest_id: dg, genre, importance: "small", rank: 0, title: "見出し", summary: "要約", detail: "解説",
    source_urls: "[]", source_tweets: '[{"media":"架空新聞","url":"https://example.com/a"}]', top_view_count: 0,
    genres: "[]", score: 0, ...over,
  };
  const cols = Object.keys(row);
  const r = db.raw.prepare(`INSERT INTO newsitem (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})`)
    .run(...(Object.values(row) as any[]));
  return Number(r.lastInsertRowid);
}

function seedExplanation(itemId: number, over: Record<string, unknown> = {}) {
  const row = {
    news_item_id: itemId, status: "done", text: "【何が起きたか】本文", error: "", claim_token: "tok",
    created_at: dbTime(NOON), updated_at: dbTime(NOON), title: "見出し", push_to: U, push_cost: 1, ...over,
  };
  const cols = Object.keys(row);
  db.raw.prepare(`INSERT INTO newsexplanation (${cols.join(",")}) VALUES (${cols.map(() => "?").join(",")})`)
    .run(...(Object.values(row) as any[]));
}

const explanation = (itemId: number) => db.rows("SELECT * FROM newsexplanation WHERE news_item_id = ?", itemId)[0];
const minutesAgo = (n: number) => dbTime(new Date(NOON.getTime() - n * 60_000));

// ---------------------------------------------------------------- 入口

describe("入口と署名", () => {
  it("署名が正しければ 200 OK", async () => {
    const res = await post([]);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("OK");
  });

  it("署名が違う・無い・base64 でないと 400 で、何も処理しない", async () => {
    for (const signature of [sign("別の本文"), "", "***"]) {
      expect((await post([follow()], { signature })).status).toBe(400);
    }
    expect(calls).toHaveLength(0);
    expect(sub()).toBeUndefined();
  });

  it("別のパス・別のメソッドは 404", async () => {
    expect((await post([], { path: "/" })).status).toBe(404);
    expect((await post([], { method: "GET" })).status).toBe(404);
  });

  it("シークレット未設定は 503", async () => {
    env.LINE_CHANNEL_SECRET = "";
    expect((await post([])).status).toBe(503);
  });

  it("1件のイベントが失敗しても次のイベントを処理して 200 を返す", async () => {
    seedSub();
    let n = 0;
    const inner = fetch;
    vi.stubGlobal("fetch", (url: string, init: RequestInit) => {
      if (url.endsWith("/message/reply") && n++ === 0) return Promise.resolve(new Response("{}", { status: 500 }));
      return inner(url, init);
    });
    expect((await post([msg("メニュー"), msg("ヘルプ")])).status).toBe(200);
    expect(lastTexts()[0]).toContain("【XNewsBotの使い方】");
  });
});

describe("イベントの正規化", () => {
  it("種別ごとの振り分け", () => {
    expect(normalize({ type: "join", replyToken: "t", source: { type: "group", groupId: G } }))
      .toStrictEqual({ kind: "join", line_user_id: null, text: "", data: "", reply_token: "t", target_id: G });
    expect(normalize(follow())?.kind).toBe("follow");
    expect(normalize(msg("あ"))).toMatchObject({ kind: "message", text: "あ", target_id: null, line_user_id: U });
    expect(normalize(pb("help", U, G))).toMatchObject({ kind: "postback", data: "help", target_id: G });
    expect(normalize({ type: "message", replyToken: "t", source: { type: "room", roomId: "R1", userId: U },
      message: { type: "text", text: "x" } })?.target_id).toBe("R1");
  });

  it("扱わない種別・user_id の無いイベントは null", () => {
    expect(normalize({ type: "unfollow", source: src(U) })).toBeNull();
    expect(normalize({ type: "message", replyToken: "t", source: src(U), message: { type: "image" } })).toBeNull();
    expect(normalize(msg("あ", null, G))).toBeNull();
    expect(normalize(pb("help", null, G))).toBeNull();
    expect(normalize({ type: "follow" })).toBeNull();
  });

  it("評価の postback だけは user_id が無くても受ける", () => {
    expect(normalize(pb("rate:k", null, G))).toMatchObject({ kind: "postback", data: "rate:k", line_user_id: null });
    expect(normalize(pb("fb:k:good", null, G))?.kind).toBe("postback");
  });
});

// ---------------------------------------------------------------- オンボーディング

describe("友だち追加・グループ参加", () => {
  it("join は案内を返すだけで購読者を作らない", async () => {
    await post([{ type: "join", replyToken: "rt", source: { type: "group", groupId: G } }]);
    expect(lastTexts()[0]).toContain("グループに追加ありがとうございます。");
    expect(db.rows("SELECT * FROM subscriber")).toHaveLength(0);
  });

  it("follow で購読者を既定値で作り、ジャンル選択を返す", async () => {
    await post([follow()]);
    expect(sub()).toMatchObject({
      enabled_genres: "[]", pending_genres: "[]", morning_hour: 8, morning_minute: 0, evening_hour: 21,
      evening_minute: 0, morning_enabled: 1, evening_enabled: 1, tz: "Asia/Tokyo", onboarding_step: "genres",
      is_onboarded: 0, last_morning_on: null, push_to: null, created_at: "2026-11-19 03:00:00.000000",
    });
    expect(lastTexts()).toStrictEqual([
      "友だち追加ありがとうございます！\nXから毎日その日のニュースをお届けします。\nまず受け取るジャンルを選んでください。",
      "受け取るジャンルを選んでください(複数可)。タップで追加できます。",
    ]);
    const r = replies()[0];
    expect(r.body.replyToken).toBe("rt");
    expect((r.init.headers as any).Authorization).toBe("Bearer line-token");
    expect(lastQR().slice(-3)).toStrictEqual(["genre_all", "genre_done", "cancel"]);
  });

  it("設定済みの人が友だち追加し直すと、今のジャンルを選択済みにして出す", async () => {
    seedSub(U, { pending_genres: "[]" });
    await post([follow()]);
    expect(sub()).toMatchObject({ onboarding_step: "genres", pending_genres: '["AI","株"]', is_onboarded: 1 });
    expect(lastTexts()[1]).toContain("現在の選択: AI / 株");
  });

  it("同じ人のイベントが同時に来ても購読者は1行", async () => {
    await Promise.all([post([follow()]), post([follow()])]);
    expect(db.rows("SELECT * FROM subscriber")).toHaveLength(1);
  });
});

describe("初回設定の流れ", () => {
  it("ジャンル → 朝 → 夜 → 完了", async () => {
    await post([follow()]);
    await post([pb("genre:株")]);
    await post([pb("genre:AI")]);
    expect(sub().pending_genres).toBe('["株","AI"]');
    expect(lastTexts()[0]).toContain("現在の選択: 株 / AI");

    await post([pb("genre_done")]);
    // 確定時は genres.toml の並び順にそろえる
    expect(sub()).toMatchObject({ enabled_genres: '["AI","株"]', onboarding_step: "morning", is_onboarded: 0 });
    expect(lastTexts()).toStrictEqual([
      "ジャンルを設定しました: AI / 株",
      "朝の配信時刻を選んでください。\n他の時刻は「7:30」のように送ってください。",
    ]);

    await post([pb("time:0700")]);
    expect(sub()).toMatchObject({ morning_hour: 7, morning_minute: 0, onboarding_step: "evening", is_onboarded: 0 });
    expect(lastTexts()[0]).toBe("朝の配信時刻を 07:00 にしました。\n次に夜の配信時刻を選んでください。");
    expect(lastQR()).toStrictEqual(["time:1900", "time:2000", "time:2100", "time:2200", "cancel"]);

    await post([msg("21時30分")]);
    expect(sub()).toMatchObject({ evening_hour: 21, evening_minute: 30, onboarding_step: "done", is_onboarded: 1 });
    expect(lastTexts()).toStrictEqual([
      "設定が完了しました！\n現在の設定\n・ジャンル: AI / 株\n・朝の配信: 07:00\n・夜の配信: 21:30" +
        "\n\n上の設定した時間にニュースをお届けします。「今すぐ配信」で今すぐ試せます。",
      "メニュー",
    ]);
  });

  it("ジャンルの付け外し・すべて・存在しないジャンル", async () => {
    await post([follow()]);
    await post([pb("genre:AI")]);
    await post([pb("genre:AI")]);
    expect(sub().pending_genres).toBe("[]");
    await post([pb("genre:存在しない")]);
    expect(sub().pending_genres).toBe("[]");
    expect(replies()).toHaveLength(4);
    await post([pb("genre_all")]);
    expect(JSON.parse(sub().pending_genres)).toHaveLength(9);
  });

  it("1つも選ばずに決定すると選び直しを促す", async () => {
    await post([follow()]);
    await post([pb("genre_done")]);
    expect(lastTexts()[0]).toBe("少なくとも1つのジャンルを選んでください。");
    expect(sub().onboarding_step).toBe("genres");
  });

  it("ジャンル選択中のテキストはボタンへ誘導する(コマンドも効かない)", async () => {
    await post([follow()]);
    await post([msg("メニュー")]);
    expect(lastTexts()[0]).toBe("下のボタンからジャンルを選んでください。");
  });

  it("時刻入力中に読めないテキストが来たら入力し直しを促す", async () => {
    seedSub(U, { onboarding_step: "morning", is_onboarded: 0 });
    await post([msg("あさ7じ")]);
    expect(lastTexts()[0]).toBe("時刻が読み取れません。「7:30」の形式で送ってください。");
    expect(lastQR()).toStrictEqual(["time:0600", "time:0700", "time:0800", "time:0900", "cancel"]);
    expect(sub().onboarding_step).toBe("morning");
  });

  it("初回設定の途中のキャンセルは中断を伝えるだけ", async () => {
    await post([follow()]);
    await post([msg("キャンセル")]);
    expect(lastTexts()).toStrictEqual(["設定を中断しました。続けるときは何かメッセージを送ってください。"]);
    await post([pb("cancel")]);
    expect(lastTexts()).toStrictEqual(["設定を中断しました。続けるときは何かメッセージを送ってください。"]);
    expect(sub().onboarding_step).toBe("genres");
  });
});

// ---------------------------------------------------------------- 設定済みの操作

describe("設定済みのテキストコマンド", () => {
  beforeEach(() => seedSub());

  it("メニュー", async () => {
    for (const t of ["メニュー", "menu", "MENU", " メニュー "]) {
      await post([msg(t)]);
      expect(lastTexts()).toStrictEqual(["メニューです。操作を選んでください。"]);
    }
    expect(lastQR()).toStrictEqual(["genre_edit", "morning_edit", "evening_edit", "deliver_now", "show_settings", "help"]);
  });

  it("ヘルプ", async () => {
    for (const t of ["ヘルプ", "help", "Help", "使い方"]) {
      await post([msg(t)]);
      expect(lastTexts()[0]).toContain("【XNewsBotの使い方】");
    }
    expect(replies()).toHaveLength(4);
  });

  it("どれでもないテキストはメニューへ誘導", async () => {
    await post([msg("こんにちは")]);
    expect(lastTexts()).toStrictEqual(["メニューから操作できます。", "メニュー"]);
  });

  it("キャンセルは編集中の選択を捨てて done に戻す", async () => {
    for (const t of ["キャンセル", "やめる", "中止", "cancel", "やめ"]) {
      db.raw.exec(`UPDATE subscriber SET onboarding_step = 'genres', pending_genres = '["話題"]'`);
      await post([msg(t)]);
      expect(sub()).toMatchObject({ onboarding_step: "done", pending_genres: '["AI","株"]' });
      expect(lastTexts()).toStrictEqual(["操作をキャンセルしました。設定は変更していません。", "メニュー"]);
    }
  });

  it("今すぐ配信: 返信してから deliver.yml を mode=user で起動する", async () => {
    for (const t of ["今すぐ", "今すぐ配信", "最新", "最新ニュース", "配信", "ニュース配信"]) {
      calls = [];
      await post([msg(t)]);
      expect(lastTexts()).toStrictEqual([
        "最新のニュースを今すぐお送りします。収集とキュレーションに10分ほどかかります。できあがり次第このトークにお届けします。",
      ]);
      expect(dispatches()).toHaveLength(1);
    }
    const d = dispatches()[0];
    expect(d.url).toBe("https://api.github.com/repos/Tomato-1101/xnewsbot-cloud/actions/workflows/deliver.yml/dispatches");
    expect(d.init.method).toBe("POST");
    expect(d.init.headers).toMatchObject({ Authorization: "Bearer gh-token", "User-Agent": "xnewsbot-worker" });
    expect(d.body).toStrictEqual({ ref: "main", inputs: { mode: "user", user: U } });
    expect(calls.map((c) => c.url.includes("github"))).toStrictEqual([false, true]);
  });

  it("起動に失敗しても webhook は落ちない", async () => {
    githubStatus = 500;
    expect((await post([msg("今すぐ")])).status).toBe(200);
    expect(replies()).toHaveLength(1);
  });
});

describe("「テスト」(レイアウト確認用のサンプル)", () => {
  beforeEach(() => seedSub());
  const setMock = (v: unknown) =>
    db.raw.prepare("INSERT INTO kv (key, value, updated_at) VALUES ('mock_specs', ?, 'x')").run(JSON.stringify(v));

  it("kv の LINE メッセージをそのまま返す", async () => {
    const messages = [{ type: "text", text: "警告" }, { type: "flex", altText: "要点", contents: { type: "bubble" } }];
    setMock(messages);
    for (const t of ["テスト", "test", "TEST", "てすと", "モック", "mock", "サンプル", "レイアウト"]) {
      await post([msg(t)]);
      expect(replies().at(-1)!.body.messages).toStrictEqual(messages);
    }
    expect(dispatches()).toHaveLength(0);
  });

  it("spec の形で入っていても LINE のメッセージにして返す", async () => {
    setMock([{ type: "text", text: "警告", quick_reply: null }, { type: "flex", alt: "要点", contents: { type: "bubble" } }]);
    await post([msg("テスト")]);
    expect(replies()[0].body.messages).toStrictEqual([
      { type: "text", text: "警告" }, { type: "flex", altText: "要点", contents: { type: "bubble" } },
    ]);
  });

  it("5通を超える分は送らない", async () => {
    setMock(Array.from({ length: 7 }, (_, i) => ({ type: "text", text: `m${i}` })));
    await post([msg("テスト")]);
    expect(lastTexts()).toStrictEqual(["m0", "m1", "m2", "m3", "m4"]);
  });

  it("まだ無い・壊れているときは短い文を返す", async () => {
    await post([msg("テスト")]);
    expect(lastTexts()).toStrictEqual(["サンプルは準備中です。"]);
    db.raw.exec("INSERT INTO kv (key, value, updated_at) VALUES ('mock_specs', '{壊れ', 'x')");
    await post([msg("テスト")]);
    expect(lastTexts()).toStrictEqual(["サンプルは準備中です。"]);
  });
});

describe("設定済みの postback", () => {
  beforeEach(() => seedSub());

  it("ジャンル変更 → 決定", async () => {
    db.raw.exec(`UPDATE subscriber SET pending_genres = '[]'`);
    await post([pb("genre_edit")]);
    expect(sub()).toMatchObject({ onboarding_step: "genres", pending_genres: '["AI","株"]' });
    await post([pb("genre:AI")]);
    await post([pb("genre:話題")]);
    await post([pb("genre_done")]);
    expect(sub()).toMatchObject({ enabled_genres: '["株","話題"]', onboarding_step: "done" });
    expect(lastTexts()).toStrictEqual([
      "ジャンルを変更しました。\n現在の設定\n・ジャンル: 株 / 話題\n・朝の配信: 08:00\n・夜の配信: 21:00", "メニュー",
    ]);
  });

  it("朝・夜の時刻変更(ボタンとテキスト)", async () => {
    await post([pb("morning_edit")]);
    expect(sub().onboarding_step).toBe("morning");
    expect(lastTexts()[0]).toContain("朝の配信時刻を選んでください。");
    await post([pb("time:0600")]);
    expect(sub()).toMatchObject({ morning_hour: 6, morning_minute: 0, onboarding_step: "done" });
    expect(lastTexts()[0]).toBe("朝の配信時刻を 06:00 に変更しました。\n現在の設定\n・ジャンル: AI / 株\n・朝の配信: 06:00\n・夜の配信: 21:00");

    await post([pb("evening_edit")]);
    expect(lastTexts()[0]).toContain("夜の配信時刻を選んでください。");
    await post([msg("22:15")]);
    expect(sub()).toMatchObject({ evening_hour: 22, evening_minute: 15, morning_hour: 6, onboarding_step: "done" });
    expect(lastTexts()[0]).toContain("夜の配信時刻を 22:15 に変更しました。");
  });

  it("時刻設定中でないときの時刻ボタン・形の違う時刻は、設定を変えずメニューを返す", async () => {
    await post([pb("time:0600")]);
    expect(sub().morning_hour).toBe(8);
    expect(lastTexts()).toStrictEqual(["メニュー"]);
    for (const bad of ["time:2460", "time:600", "time:ab00", "time:"]) {
      await post([pb("morning_edit")]);
      await post([pb(bad)]);
      expect(lastTexts()).toStrictEqual(["メニュー"]);
      expect(sub()).toMatchObject({ morning_hour: 8, morning_minute: 0 });
    }
  });

  it("設定確認・ヘルプ・キャンセル・未知の data", async () => {
    await post([pb("show_settings")]);
    expect(lastTexts()).toStrictEqual(["現在の設定\n・ジャンル: AI / 株\n・朝の配信: 08:00\n・夜の配信: 21:00", "メニュー"]);
    await post([pb("help")]);
    expect(lastTexts()[0]).toContain("【XNewsBotの使い方】");
    await post([pb("genre_edit")]);
    await post([pb("cancel")]);
    expect(lastTexts()[0]).toBe("操作をキャンセルしました。設定は変更していません。");
    expect(sub().onboarding_step).toBe("done");
    await post([pb("なにか")]);
    expect(lastTexts()).toStrictEqual(["メニュー"]);
  });

  it("今すぐ配信のボタン", async () => {
    await post([pb("deliver_now")]);
    expect(lastTexts()).toStrictEqual(["今日のニュースをお送りします。収集とキュレーションに10分ほどかかります。できあがり次第お届けします。"]);
    expect(dispatches()[0].body).toStrictEqual({ ref: "main", inputs: { mode: "user", user: U } });
  });

  it("Actions が書く列(last_morning_on)は設定の保存で変えない", async () => {
    await post([pb("genre_edit")]);
    expect(sub().last_morning_on).toBe("2026-11-19");
  });
});

describe("配信先の切り替えとグループ", () => {
  beforeEach(() => seedSub());

  it("グループで合言葉を送るとそのグループが配信先になる", async () => {
    for (const t of ["このグループに配信", "ここに配信", "グループ配信", "ここに配信して"]) {
      db.raw.exec("UPDATE subscriber SET push_to = NULL");
      await post([msg(t, U, G)]);
      expect(sub().push_to).toBe(G);
      expect(lastTexts()[0]).toContain("これ以降、ニュースはこのトークに配信します。");
    }
  });

  it("1:1 で送ると案内だけ", async () => {
    await post([msg("このグループに配信")]);
    expect(sub().push_to).toBeNull();
    expect(lastTexts()[0]).toContain("ここは1:1トークです。");
  });

  it("個別に戻す", async () => {
    for (const t of ["個別に配信", "個別配信", "1対1に配信", "個人に配信"]) {
      db.raw.prepare("UPDATE subscriber SET push_to = ?").run(G);
      await post([msg(t, U, G)]);
      expect(sub().push_to).toBeNull();
      expect(lastTexts()).toStrictEqual(["配信先を個別トークに戻しました。"]);
    }
  });

  it("グループの雑談・キャンセル・時刻には反応しない", async () => {
    db.raw.exec("UPDATE subscriber SET onboarding_step = 'morning'");
    await post([msg("こんにちは", U, G), msg("キャンセル", U, G), msg("7:30", U, G)]);
    expect(calls).toHaveLength(0);
    expect(sub()).toMatchObject({ onboarding_step: "morning", morning_hour: 8 });
  });

  it("グループでも明示コマンドには応える", async () => {
    await post([msg("メニュー", U, G)]);
    expect(lastTexts()).toStrictEqual(["メニューです。操作を選んでください。"]);
    await post([msg("今すぐ", U, G)]);
    expect(dispatches()[0].body.inputs).toStrictEqual({ mode: "user", user: U });
  });
});

// ---------------------------------------------------------------- 詳細・評価

describe("詳細", () => {
  beforeEach(() => seedSub());

  it("安定キーで引き、AI解説と評価のボタンを付ける", async () => {
    seedItem({ rank: 3, title: "見出しX" });
    await post([pb("detail:20261119:morning:AI:3")]);
    expect(lastTexts()).toStrictEqual(["【AI】見出しX\n\n解説\n\n出典\n・架空新聞 https://example.com/a"]);
    const h = await titleHash("見出しX");
    expect(lastQR()).toStrictEqual([
      "explain:20261119:morning:AI:3", `fb:20261119:morning:AI:${h}:good`, `fb:20261119:morning:AI:${h}:bad`,
    ]);
  });

  it("旧ボタンの id 直指定でも引ける(評価ボタンは付けない)", async () => {
    const id = seedItem();
    await post([pb(`detail:${id}`)]);
    expect(lastTexts()[0]).toContain("【AI】見出し");
    expect(lastQR()).toStrictEqual([`explain:${id}`]);
  });

  it("見つからない・形の違うキー", async () => {
    seedItem();
    for (const p of ["999", "20261119:morning:AI:9", "20261118:morning:AI:0", "20261119:evening:AI:0", "20261340:morning:AI:0",
      "20261119:morning:AI:x", "a:b", ""]) {
      await post([pb(`detail:${p}`)]);
      expect(lastTexts()).toStrictEqual(["この記事は見つかりませんでした(配信が更新された可能性があります)。"]);
    }
  });
});

describe("評価", () => {
  let key: string;
  let id: number;
  beforeEach(async () => {
    seedSub();
    id = seedItem({ rank: 0, title: "別の記事" });
    id = seedItem({ rank: 1, title: "見出しX" });
    key = `20261119:morning:AI:${await titleHash("見出しX")}`;
  });
  const feedbackRows = () => db.rows("SELECT * FROM newsfeedback ORDER BY id");

  it("rate: 見出しと評価ボタンのバブルを返す", async () => {
    await post([pb(`rate:${key}`)]);
    const m = replies()[0].body.messages[0];
    expect(m.altText).toBe("この記事の評価: 見出しX");
    expect(m.contents.body.contents[3].contents.map((b: any) => b.action.data)).toStrictEqual([`fb:${key}:good`, `fb:${key}:bad`]);
    expect(feedbackRows()).toHaveLength(0);
  });

  it("fb: 記録し、押し直しは同じ行を上書きする", async () => {
    await post([pb(`fb:${key}:good`)]);
    expect(lastTexts()).toStrictEqual(["興味あり として記録しました"]);
    expect(feedbackRows()).toEqual([{
      id: 1, newsitem_id: id, user_id: U, verdict: "good", genre: "AI", title: "見出しX", digest_date: "2026-11-19",
      created_at: "2026-11-19 03:00:00.000000", updated_at: "2026-11-19 03:00:00.000000",
    }]);
    vi.setSystemTime(new Date(NOON.getTime() + 60_000));
    await post([pb(`fb:${key}:bad`)]);
    expect(lastTexts()).toStrictEqual(["興味なし として記録しました"]);
    expect(feedbackRows()).toMatchObject([{ verdict: "bad", created_at: "2026-11-19 03:00:00.000000", updated_at: "2026-11-19 03:01:00.000000" }]);
  });

  it("配信先のグループなら、購読者でない人・user_id の無い人の評価も受ける(購読者の行は作らない)", async () => {
    db.raw.prepare("UPDATE subscriber SET push_to = ?").run(G);
    await post([pb(`fb:${key}:good`, "Uother", G), pb(`fb:${key}:bad`, null, G)]);
    expect(feedbackRows().map((r) => [r.user_id, r.verdict])).toStrictEqual([["Uother", "good"], ["group", "bad"]]);
    expect(db.rows("SELECT * FROM subscriber")).toHaveLength(1);
  });

  it("配信先でないグループ・購読者でない人・設定途中の人の 1:1 は黙って無視する", async () => {
    seedSub("Uhalf", { is_onboarded: 0 });
    await post([pb(`fb:${key}:good`, U, "Cother"), pb(`fb:${key}:good`, "Uother"), pb(`rate:${key}`, "Uhalf"), pb(`fb:${key}:good`, null)]);
    expect(calls).toHaveLength(0);
    expect(feedbackRows()).toHaveLength(0);
    expect(db.rows("SELECT * FROM subscriber WHERE line_user_id = 'Uother'")).toHaveLength(0);
  });

  it("見つからない記事・rank のキー・id 直指定", async () => {
    for (const d of ["fb:20261119:morning:AI:00000000:good", "rate:20261119:morning:AI:1", `rate:${id}`, "fb:good", "rate:"]) {
      await post([pb(d)]);
      expect(lastTexts()).toStrictEqual(["この記事は見つかりませんでした（配信し直されたため評価できません）"]);
    }
  });

  it("サンプル記事は記録しない", async () => {
    seedItem({ title: "モック" }, "2000-01-01");
    const k = `20000101:morning:AI:${await titleHash("モック")}`;
    await post([pb(`rate:${k}`), pb(`fb:${k}:good`)]);
    expect(replies().map((r) => r.body.messages[0].text)).toStrictEqual(Array(2).fill("サンプル記事なので評価は記録されません"));
    expect(feedbackRows()).toHaveLength(0);
  });

  it("good/bad 以外はメニューを返す", async () => {
    await post([pb(`fb:${key}:maybe`)]);
    expect(lastTexts()).toStrictEqual(["メニュー"]);
    expect(feedbackRows()).toHaveLength(0);
  });

  it("記録に失敗したら無応答にせず、失敗を伝える", async () => {
    db.raw.exec("DROP TABLE newsfeedback");
    expect((await post([pb(`fb:${key}:good`)])).status).toBe(200);
    expect(lastTexts()).toStrictEqual(["記録に失敗しました。時間をおいてもう一度押してください"]);
  });
});

// ---------------------------------------------------------------- AI解説

describe("AI解説", () => {
  let id: number;
  const KEY = "20261119:morning:AI:0";
  beforeEach(() => {
    seedSub();
    id = seedItem();
  });
  const press = (key = KEY, uid = U, gid?: string) => post([pb(`explain:${key}`, uid, gid)]);

  it("確保して explain.yml を起動し、受付を返す", async () => {
    await press();
    expect(lastTexts()).toStrictEqual(["AI解説を作っています。1〜2分お待ちください。"]);
    const row = explanation(id);
    expect(row).toMatchObject({
      status: "running", text: "", error: "", title: "見出し", push_to: U, push_cost: 1,
      created_at: "2026-11-19 03:00:00.000000", updated_at: "2026-11-19 03:00:00.000000",
    });
    expect(row.claim_token).toMatch(/^[0-9a-f]{32}$/);
    expect(dispatches()).toHaveLength(1);
    expect(dispatches()[0].url).toBe("https://api.github.com/repos/Tomato-1101/xnewsbot-cloud/actions/workflows/explain.yml/dispatches");
    expect(dispatches()[0].body).toStrictEqual({ ref: "main", inputs: { explanation_id: String(row.id) } });
  });

  it("グループで押すとグループ宛て・人数分の通数で確保する", async () => {
    await press(KEY, U, G);
    expect(explanation(id)).toMatchObject({ push_to: G, push_cost: 3 });
  });

  it("作成済みは作り直さず、その場で返す", async () => {
    seedExplanation(id);
    await press();
    expect(lastTexts()).toStrictEqual(["【AI解説】見出し\n\n【何が起きたか】本文"]);
    expect(dispatches()).toHaveLength(0);
    expect(calls).toHaveLength(1); // 通数の API も叩かない
  });

  it("作成中(15分以内)は待ってもらう", async () => {
    seedExplanation(id, { status: "running", text: "", updated_at: minutesAgo(14) });
    await press();
    expect(lastTexts()).toStrictEqual(["いま作成中です。届かないときは、少し後にもう一度押すとすぐ表示します。"]);
    expect(dispatches()).toHaveLength(0);
  });

  it.each([
    ["失効した作成中(15分超)", { status: "running", text: "", updated_at: minutesAgo(16) }],
    ["失敗", { status: "failed", text: "", error: "時間切れ" }],
    ["見出しが違う作成済み(記事 id の使い回し)", { title: "前の記事" }],
    ["見出しが違う作成中(記事 id の使い回し)", { status: "running", title: "前の記事" }],
  ])("%s は取り直す", async (_name, over) => {
    seedExplanation(id, over);
    const before = explanation(id);
    await press();
    expect(lastTexts()).toStrictEqual(["AI解説を作っています。1〜2分お待ちください。"]);
    const row = explanation(id);
    expect(row).toMatchObject({ id: before.id, status: "running", text: "", error: "", title: "見出し", updated_at: dbTime(NOON) });
    expect(row.claim_token).not.toBe("tok");
    expect(dispatches()).toHaveLength(1);
  });

  it("記事が無い・サンプル記事", async () => {
    await press("20261119:morning:AI:9");
    expect(lastTexts()).toStrictEqual(["この記事は見つかりませんでした(配信が更新された可能性があります)。"]);
    const mock = seedItem({}, "2000-01-01");
    await press(String(mock));
    expect(lastTexts()).toStrictEqual(["この記事はレイアウト確認用のサンプル(架空の内容)なので、AI解説は作れません。"]);
    expect(dispatches()).toHaveLength(0);
  });

  it.each([["07:00", true], ["08:29", true], ["06:59", false], ["08:30", false]])(
    "朝の準備中(7:00〜8:30)は受け付けない: %s", async (hhmm, quiet) => {
      vi.setSystemTime(new Date(`2026-11-19T${hhmm}:00+09:00`));
      await press();
      expect(dispatches()).toHaveLength(quiet ? 0 : 1);
      if (quiet) {
        expect(lastTexts()).toStrictEqual(["朝の配信の準備中(7:00〜8:30)はAI解説を受け付けていません。8:30以降にもう一度押してください。"]);
        expect(explanation(id)).toBeUndefined();
      }
    });

  it("静かな時間帯でも作成済みは返す", async () => {
    vi.setSystemTime(new Date("2026-11-19T07:30:00+09:00"));
    seedExplanation(id);
    await press();
    expect(lastTexts()[0]).toContain("【AI解説】見出し");
  });

  it("同時に作るのは2件まで(失効した作成中は数えない)", async () => {
    seedExplanation(seedItem({ rank: 1 }), { status: "running", updated_at: minutesAgo(1) });
    seedExplanation(seedItem({ rank: 2 }), { status: "running", updated_at: minutesAgo(20) });
    await press();
    expect(dispatches()).toHaveLength(1);

    const other = seedItem({ rank: 3, title: "別の見出し" });
    await press("20261119:morning:AI:3");
    expect(lastTexts()).toStrictEqual(["いまほかのAI解説を作っています。少し後にもう一度押してください。"]);
    expect(explanation(other)).toBeUndefined();
    expect(dispatches()).toHaveLength(1);
    expect(calls.filter((c) => c.url.includes("/quota"))).toHaveLength(2); // 2回目は通数の API を叩く前に断る
  });

  it.each([
    ["上限なしのプラン", () => { line.quota = { type: "none" }; }],
    ["上限の取得に失敗", () => { line.quota = null; }],
    ["使用数の取得に失敗", () => { line.used = null; }],
    ["人数の取得に失敗", () => { db.raw.prepare("UPDATE subscriber SET push_to = ?").run("Cunknown"); }],
  ])("通数を確かめられないときは作らない: %s", async (_name, arrange) => {
    arrange();
    await press();
    expect(lastTexts()).toStrictEqual([
      "LINEの送信枠の残りを確認できなかったため、朝の配信を守るためにAI解説は使えません。時間をおいてもう一度お試しください。",
    ]);
    expect(explanation(id)).toBeUndefined();
    expect(dispatches()).toHaveLength(0);
  });

  describe("朝の配信に要る通数を残す", () => {
    // 11/19 の朝は配信済み → 月末までの朝は 11 回。グループ(3人)宛てなので 33 通要る
    beforeEach(() => db.raw.prepare("UPDATE subscriber SET push_to = ?").run(G));

    it("ちょうど残せるなら作る(1:1 で押した分は1通)", async () => {
      line.used = { totalUsage: 200 - 34 };
      await press();
      expect(dispatches()).toHaveLength(1);
      expect(explanation(id)).toMatchObject({ push_to: U, push_cost: 1 });
    });

    it("1通でも足りなければ断り、残りと必要数を伝える", async () => {
      line.used = { totalUsage: 200 - 33 };
      await press();
      expect(lastTexts()).toStrictEqual([
        "今月のLINEの送信枠が残り少ないため、朝の配信を守るためにAI解説は使えません（残り33通・月末までの朝配信に33通必要）。",
      ]);
      expect(explanation(id)).toBeUndefined();
      expect(dispatches()).toHaveLength(0);
    });

    it("作成中の分(まだ送っていない通数)も引いて判定する", async () => {
      seedExplanation(seedItem({ rank: 1 }), { status: "running", push_cost: 3, updated_at: minutesAgo(1) });
      line.used = { totalUsage: 200 - 36 };
      await press();
      expect(lastTexts()[0]).toContain("（残り33通・月末までの朝配信に33通必要）");
    });

    it("今日の朝がまだなら今日の分も残す(12 回 × 3 通)", async () => {
      db.raw.exec("UPDATE subscriber SET last_morning_on = '2026-11-18'");
      line.used = { totalUsage: 200 - 36 };
      await press();
      expect(lastTexts()[0]).toContain("（残り36通・月末までの朝配信に36通必要）");
    });

    it("朝がオフ・ジャンル無し・設定途中の購読者の分は数えない", async () => {
      db.raw.exec("UPDATE subscriber SET morning_enabled = 0");
      seedSub("U2", { enabled_genres: "[]" });
      seedSub("U3", { is_onboarded: 0 });
      line.used = { totalUsage: 199 };
      await press();
      expect(dispatches()).toHaveLength(1);
    });
  });

  it("同じ記事を同時に押されても、確保と起動は1回だけ", async () => {
    await Promise.all([press(), press(), press()]);
    expect(dispatches()).toHaveLength(1);
    expect(db.rows("SELECT * FROM newsexplanation")).toHaveLength(1);
    const texts = replies().map((r) => r.body.messages[0].text).sort();
    expect(texts).toStrictEqual([
      "AI解説を作っています。1〜2分お待ちください。",
      "いま作成中です。届かないときは、少し後にもう一度押すとすぐ表示します。",
      "いま作成中です。届かないときは、少し後にもう一度押すとすぐ表示します。",
    ]);
  });

  it("別々の記事を同時に押されても、同時に作るのは2件まで", async () => {
    for (let r = 1; r <= 3; r++) seedItem({ rank: r, title: `見出し${r}` });
    await Promise.all([0, 1, 2, 3].map((r) => press(`20261119:morning:AI:${r}`)));
    expect(db.rows("SELECT * FROM newsexplanation WHERE status = 'running'")).toHaveLength(2);
    expect(dispatches()).toHaveLength(2);
    expect(replies().filter((r) => r.body.messages[0].text === "いまほかのAI解説を作っています。少し後にもう一度押してください。")).toHaveLength(2);
  });

  it("同時に押されても、通数の残りを超えて確保しない", async () => {
    db.raw.prepare("UPDATE subscriber SET push_to = ?").run(G);
    seedItem({ rank: 1, title: "見出し1" });
    line.used = { totalUsage: 200 - 34 }; // 朝の 33 通のほかに余るのは1通だけ
    await Promise.all([press(), press("20261119:morning:AI:1")]);
    expect(db.rows("SELECT * FROM newsexplanation")).toHaveLength(1);
    expect(dispatches()).toHaveLength(1);
    expect(replies().filter((r) => r.body.messages[0].text.includes("（残り33通・月末までの朝配信に33通必要）"))).toHaveLength(1);
  });

  it("起動に失敗したら確保を手放して、使えない旨を返す(すぐ押し直せる)", async () => {
    githubStatus = 500;
    await press();
    expect(lastTexts()).toStrictEqual(["いまはAI解説を使えません。"]);
    expect(explanation(id)).toMatchObject({ status: "failed", error: "AI解説の起動に失敗しました" });
    githubStatus = 204;
    await press();
    expect(lastTexts()).toStrictEqual(["AI解説を作っています。1〜2分お待ちください。"]);
    expect(explanation(id).status).toBe("running");
  });
});

// ---------------------------------------------------------------- Cron

describe("Cron", () => {
  const run = (cron: string, at = NOON) => worker.scheduled({ cron, scheduledTime: at.getTime() } as any, env);

  it("07:25 JST は必ず deliver mode=due を起動する", async () => {
    await run("25 22 * * *");
    expect(dispatches().map((d) => [d.url.split("/").at(-2), d.body])).toStrictEqual([
      ["deliver.yml", { ref: "main", inputs: { mode: "due" } }],
    ]);
  });

  it.each(["30 3 * * *", "0 8 * * *", "0 12 * * *"])("復旧の回(%s): 当日朝が未配信の人がいる時だけ recover を起動する", async (cron) => {
    seedSub();
    await run(cron);
    expect(dispatches()).toHaveLength(0);
    db.raw.exec("UPDATE subscriber SET last_morning_on = '2026-11-18'");
    await run(cron);
    expect(dispatches().map((d) => d.body)).toStrictEqual([{ ref: "main", inputs: { mode: "recover" } }]);
  });

  it("未配信の判定: 一度も配信していない人は未配信、朝オフ・ジャンル無し・設定途中の人は対象外", async () => {
    expect(await hasPendingMorning(db.d1, NOON)).toBe(false);
    seedSub("U1", { last_morning_on: null, morning_enabled: 0 });
    seedSub("U2", { last_morning_on: null, enabled_genres: "[]" });
    seedSub("U3", { last_morning_on: null, is_onboarded: 0 });
    expect(await hasPendingMorning(db.d1, NOON)).toBe(false);
    seedSub("U4", { last_morning_on: null });
    expect(await hasPendingMorning(db.d1, NOON)).toBe(true);
  });

  it("「今日」は日本時間で数える(UTC ではまだ前日の朝でも)", async () => {
    seedSub(U, { last_morning_on: "2026-11-19" });
    // 2026-11-19 23:30 UTC = 11-20 08:30 JST
    expect(await hasPendingMorning(db.d1, new Date("2026-11-19T23:30:00Z"))).toBe(true);
    // 2026-11-18 16:00 UTC = 11-19 01:00 JST
    expect(await hasPendingMorning(db.d1, new Date("2026-11-18T16:00:00Z"))).toBe(false);
  });
});
