// xnewsbot/line_client.py の spec 生成のうち webhook が使う分の移植。
// 出力が Python と完全に一致することを golden/golden.json で確かめている(test/golden.test.ts)。
import { GENRE_LABELS, SELECTABLE_KEYS, genreLabel, isGenre } from "./genres";

export interface QuickReply {
  label: string;
  data: string;
}
export interface TextSpec {
  type: "text";
  text: string;
  quick_reply: QuickReply[] | null;
}
export interface FlexSpec {
  type: "flex";
  alt: string;
  contents: unknown;
}
export type Spec = TextSpec | FlexSpec;

export interface Sub {
  id: number;
  line_user_id: string;
  enabled_genres: string[];
  pending_genres: string[];
  morning_hour: number;
  morning_minute: number;
  evening_hour: number;
  evening_minute: number;
  morning_enabled: boolean;
  evening_enabled: boolean;
  tz: string;
  onboarding_step: string;
  is_onboarded: boolean;
  last_morning_on: string | null;
  push_to: string | null;
}

export interface Item {
  id: number;
  genre_digest_id: number;
  genre: string;
  genres: string[];
  title: string;
  summary: string;
  detail: string;
  source_urls: string[];
  source_tweets: Record<string, unknown>[];
}

const QUICK_REPLY_MAX = 13;
const ALT_MAX_CHARS = 400;
const DETAIL_MAX_CHARS = 4800;
const TITLE_COLOR = "#111111";
const META_COLOR = "#999999";
const FEEDBACK_NOTE = "今後の配信の選び方に反映します";
export const SLOT_LABEL: Record<string, string> = { morning: "朝", evening: "夜" };

/** Python の s[:n] と同じ切り方(文字数は UTF-16 の単位ではなくコードポイントで数える)。 */
export function cut(s: string, n: number): string {
  if (s.length <= n) return s;
  return Array.from(s).slice(0, n).join("");
}

function codePoints(s: string): number {
  // 上限よりずっと短い文は数えない(毎回の配列化を避ける)
  return s.length <= DETAIL_MAX_CHARS ? s.length : Array.from(s).length;
}

const pad2 = (n: number) => String(n).padStart(2, "0");

export async function titleHash(title: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-1", new TextEncoder().encode(title || ""));
  return Array.from(new Uint8Array(buf).slice(0, 4), (b) => b.toString(16).padStart(2, "0")).join("");
}

export function textSpec(text: string, quickReply: QuickReply[] | null = null): TextSpec {
  return { type: "text", text, quick_reply: quickReply };
}

export function qr(label: string, data: string): QuickReply {
  return { label: cut(label, 20), data };
}

export function genreSelectSpec(selected: string[]): TextSpec {
  const sel = selected.filter(isGenre);
  const head = sel.length
    ? "受け取るジャンルを選んでください(複数可)。\n現在の選択: " + sel.join(" / ")
    : "受け取るジャンルを選んでください(複数可)。タップで追加できます。";
  const toggles = SELECTABLE_KEYS.map((key) =>
    qr(`${sel.includes(key) ? "✓" : "＋"}${GENRE_LABELS[key]}`, `genre:${key}`),
  );
  const actions = [qr("すべて", "genre_all"), qr("これで決定", "genre_done"), qr("キャンセル", "cancel")];
  return textSpec(head, [...toggles.slice(0, QUICK_REPLY_MAX - actions.length), ...actions]);
}

const TIME_CHOICES: Record<string, [string, string][]> = {
  morning: [["6:00", "0600"], ["7:00", "0700"], ["8:00", "0800"], ["9:00", "0900"]],
  evening: [["19:00", "1900"], ["20:00", "2000"], ["21:00", "2100"], ["22:00", "2200"]],
};

export function timeSelectSpec(slot = "morning"): TextSpec {
  const label = SLOT_LABEL[slot] ?? "";
  const head = `${label}の配信時刻を選んでください。\n他の時刻は「7:30」のように送ってください。`;
  const items = (TIME_CHOICES[slot] ?? TIME_CHOICES.morning).map(([disp, hhmm]) => qr(disp, `time:${hhmm}`));
  items.push(qr("キャンセル", "cancel"));
  return textSpec(head, items);
}

export function menuQuickReply(): QuickReply[] {
  return [
    qr("ジャンル変更", "genre_edit"),
    qr("朝の時刻", "morning_edit"),
    qr("夜の時刻", "evening_edit"),
    qr("今すぐ配信", "deliver_now"),
    qr("設定確認", "show_settings"),
    qr("ヘルプ", "help"),
  ];
}

export function menuSpec(text = "メニュー"): TextSpec {
  return textSpec(text, menuQuickReply());
}

export function settingsSummaryText(sub: Sub): string {
  const g = sub.enabled_genres.length ? sub.enabled_genres.join(" / ") : "(未設定)";
  const m = sub.morning_enabled ? `${pad2(sub.morning_hour)}:${pad2(sub.morning_minute)}` : "オフ";
  const e = sub.evening_enabled ? `${pad2(sub.evening_hour)}:${pad2(sub.evening_minute)}` : "オフ";
  return `現在の設定\n・ジャンル: ${g}\n・朝の配信: ${m}\n・夜の配信: ${e}`;
}

export function tagLabels(item: Item): string[] {
  const keys = item.genres.length ? item.genres : [item.genre];
  return keys.map(genreLabel);
}

export function tagText(item: Item): string {
  return tagLabels(item).join("/");
}

export function sourceName(s: Record<string, unknown>): string {
  if (s.media) return String(s.media);
  const author = (s.author as string) || "";
  if (!author || author === "?") return "";
  return s.kind === "news" ? author : `@${author}`;
}

export function feedbackQuickReply(key: string): QuickReply[] {
  return [qr("興味あり", `fb:${key}:good`), qr("興味なし", `fb:${key}:bad`)];
}

export async function detailSpec(item: Item, payload: string | null = null): Promise<TextSpec> {
  const lines = [`【${tagText(item)}】${item.title}`];
  const body = item.detail || item.summary;
  if (body) lines.push("", body);

  let src: string[];
  if (item.source_tweets.length) {
    src = item.source_tweets
      .slice(0, 3)
      .map((s) => [sourceName(s), (s.url as string) ?? ""].filter(Boolean).join(" "));
  } else {
    src = item.source_urls.slice(0, 3);
  }
  src = src.filter(Boolean);
  if (src.length) lines.push("", "出典", ...src.map((s) => `・${s}`));

  let text = lines.join("\n");
  if (codePoints(text) > DETAIL_MAX_CHARS) text = cut(text, DETAIL_MAX_CHARS) + "…";
  const key = payload ?? String(item.id);
  const out = [qr("AI解説", `explain:${key}`)];
  const parts = key.split(":");
  // 安定キー(日付:スロット:ジャンル:rank)のときだけ評価ボタンを付ける。id 直指定はモック・旧ボタン
  if (parts.length === 4) {
    out.push(...feedbackQuickReply([...parts.slice(0, 3), await titleHash(item.title)].join(":")));
  }
  return textSpec(text, out);
}

export function feedbackSpec(title: string, key: string): FlexSpec {
  const button = (label: string, verdict: string, style: string) => ({
    type: "button", style, height: "sm", flex: 1,
    action: { type: "postback", label, data: `fb:${key}:${verdict}` },
  });
  const bubble = {
    type: "bubble", size: "kilo",
    body: {
      type: "box", layout: "vertical", paddingAll: "16px", contents: [
        { type: "text", text: "この記事の評価", size: "xxs", color: META_COLOR },
        { type: "text", text: title, size: "sm", weight: "bold", color: TITLE_COLOR, wrap: true, margin: "sm" },
        { type: "text", text: FEEDBACK_NOTE, size: "xxs", color: META_COLOR, wrap: true, margin: "md" },
        {
          type: "box", layout: "horizontal", spacing: "sm", margin: "md",
          contents: [button("興味あり", "good", "primary"), button("興味なし", "bad", "secondary")],
        },
      ],
    },
  };
  return { type: "flex", alt: cut(`この記事の評価: ${title}`, ALT_MAX_CHARS), contents: bubble };
}

export function helpSpec(): TextSpec {
  return textSpec(
    "【XNewsBotの使い方】\n" +
      "毎日、設定した時間に X(Twitter)から集めたニュースをお届けします。\n" +
      "・1通目: 今日の要点・市況（仮想通貨を含む）・今日の予定（経済指標や決算など）・残り使用量\n" +
      "・2通目から: ジャンルごとのニュースの見出し。見出しをタップすると解説が出ます\n" +
      "・ジャンルは「仮想通貨」「話題」なども選べます\n\n" +
      "「メニュー」と送ると、ジャンルや朝/夜の時刻をいつでも変更できます。",
    menuQuickReply(),
  );
}

export function lineText(title: string, text: string): string {
  return `【AI解説】${title}\n\n${text}`;
}

/** spec → LINE Messaging API のメッセージ(line_client._spec_to_message と同じ形)。 */
export function toMessage(spec: Spec): Record<string, unknown> {
  if (spec.type === "text") {
    const msg: Record<string, unknown> = { type: "text", text: spec.text };
    if (spec.quick_reply?.length) {
      msg.quickReply = {
        items: spec.quick_reply.map((q) => ({
          type: "action",
          action: { type: "postback", label: q.label, data: q.data, displayText: q.label },
        })),
      };
    }
    return msg;
  }
  return { type: "flex", altText: spec.alt, contents: spec.contents };
}
