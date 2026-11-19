// XNewsBot の LINE webhook と定刻の起動(Cron → GitHub Actions)。設計は cloud/DESIGN.md。
import { handleEvent, hasPendingMorning, normalize } from "./events";
import { type Env, dispatch, verifySignature } from "./line";

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/line/callback") {
      return new Response("Not Found", { status: 404 });
    }
    if (!env.LINE_CHANNEL_SECRET || !env.LINE_CHANNEL_ACCESS_TOKEN) {
      return new Response("LINE のトークン/シークレットが未設定です。", { status: 503 });
    }
    const body = await request.text();
    const signature = request.headers.get("X-Line-Signature") ?? "";
    if (!(await verifySignature(env.LINE_CHANNEL_SECRET, body, signature))) {
      return new Response("署名が不正です", { status: 400 });
    }
    let events: unknown;
    try {
      events = JSON.parse(body).events;
    } catch {
      return new Response("本文が JSON ではありません", { status: 400 });
    }
    for (const raw of Array.isArray(events) ? events : []) {
      const ev = normalize(raw);
      if (!ev) continue;
      try {
        await handleEvent(env, ev);
      } catch (e) {
        // 1件の失敗で残りのイベントを止めない。LINE には 200 を返す(再送されても同じ結果になるだけ)
        console.error(`イベント処理に失敗: ${ev.kind}`, e);
      }
    }
    return new Response("OK");
  },

  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    if (controller.cron === "25 22 * * *") {
      await dispatch(env, "deliver.yml", { mode: "due" });
      return;
    }
    // 復旧の回は、当日朝が未配信の人がいる日だけ起動する(配信済みの日に Actions を空回しさせない)
    if (await hasPendingMorning(env.DB, new Date(controller.scheduledTime))) {
      await dispatch(env, "deliver.yml", { mode: "recover" });
    }
  },
} satisfies ExportedHandler<Env>;
