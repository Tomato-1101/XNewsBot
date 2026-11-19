// LINE Messaging API と GitHub workflow_dispatch の呼び出し(fetch)。
import { type Spec, toMessage } from "./specs";

export interface Env {
  DB: D1Database;
  LINE_CHANNEL_SECRET: string;
  LINE_CHANNEL_ACCESS_TOKEN: string;
  GITHUB_DISPATCH_TOKEN: string;
}

const LINE_API = "https://api.line.me/v2/bot";
const DISPATCH_BASE = "https://api.github.com/repos/Tomato-1101/xnewsbot-cloud/actions/workflows";
const MAX_MESSAGES = 5;

export async function verifySignature(secret: string, body: string, signature: string): Promise<boolean> {
  let sig: Uint8Array;
  try {
    sig = Uint8Array.from(atob(signature), (c) => c.charCodeAt(0));
  } catch {
    return false;
  }
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
  return crypto.subtle.verify("HMAC", key, sig, enc.encode(body));
}

export async function replyMessages(env: Env, replyToken: string, messages: unknown[]): Promise<void> {
  if (!messages.length) return;
  const res = await fetch(`${LINE_API}/message/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}` },
    body: JSON.stringify({ replyToken, messages: messages.slice(0, MAX_MESSAGES) }),
  });
  if (!res.ok) throw new Error(`LINE reply に失敗 status=${res.status} ${(await res.text()).slice(0, 300)}`);
}

export function reply(env: Env, replyToken: string, specs: Spec[]): Promise<void> {
  return replyMessages(env, replyToken, specs.map(toMessage));
}

async function lineGet(env: Env, path: string): Promise<Record<string, unknown> | null> {
  try {
    const res = await fetch(`${LINE_API}${path}`, {
      headers: { Authorization: `Bearer ${env.LINE_CHANNEL_ACCESS_TOKEN}` },
    });
    if (!res.ok) {
      console.warn(`LINE API ${path} status=${res.status}`);
      return null;
    }
    return (await res.json()) as Record<string, unknown>;
  } catch (e) {
    console.warn(`LINE API ${path} に失敗`, e);
    return null;
  }
}

/** 今月の上限と使用数。上限なし・取得失敗は null(呼び出し側が安全側=送らない に倒す)。 */
export async function fetchLimitUsed(env: Env): Promise<{ limit: number; used: number } | null> {
  const quota = await lineGet(env, "/message/quota");
  if (!quota || quota.type !== "limited") return null;
  const used = await lineGet(env, "/message/quota/consumption");
  if (!used || typeof quota.value !== "number" || typeof used.totalUsage !== "number") return null;
  return { limit: quota.value, used: used.totalUsage };
}

const MEMBER_TTL_MS = 600_000;
// アイソレートが生きている間だけ効く覚え書き(押すたびに人数の API を叩かない。消えても取り直すだけ)
const members = new Map<string, [number, number]>();

/** to へ push 1回で消費する通数(グループ/ルームは人数、ユーザーは1)。取得失敗は null。 */
export async function memberCount(env: Env, to: string): Promise<number | null> {
  if (!(to.startsWith("C") || to.startsWith("R"))) return 1;
  const hit = members.get(to);
  if (hit && Date.now() - hit[0] < MEMBER_TTL_MS) return hit[1];
  const kind = to.startsWith("C") ? "group" : "room";
  const res = await lineGet(env, `/${kind}/${to}/members/count`);
  if (!res || typeof res.count !== "number") return null;
  members.set(to, [Date.now(), res.count]);
  return res.count;
}

export function clearMemberCache(): void {
  members.clear();
}

/** GitHub Actions のワークフローを起動する。起動できたら true。 */
export async function dispatch(env: Env, workflow: string, inputs: Record<string, string>): Promise<boolean> {
  try {
    const res = await fetch(`${DISPATCH_BASE}/${workflow}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        // GitHub API は User-Agent の無いリクエストを 403 で弾く
        "User-Agent": "xnewsbot-worker",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    });
    if (!res.ok) console.error(`workflow_dispatch ${workflow} に失敗 status=${res.status} ${(await res.text()).slice(0, 300)}`);
    return res.ok;
  } catch (e) {
    console.error(`workflow_dispatch ${workflow} に失敗`, e);
    return false;
  }
}
