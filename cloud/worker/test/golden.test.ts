// Python(xnewsbot)の出力(golden/golden.json。golden/make_golden.py が書く)と TS の出力の一致を確かめる。
import { describe, expect, it } from "vitest";
import golden from "../golden/golden.json";
import { parseTime } from "../src/events";
import { GENRE_KEYS, GENRE_LABELS, SELECTABLE_KEYS } from "../src/genres";
import * as s from "../src/specs";

const item = (a: any): s.Item => ({ genre_digest_id: 1, ...a });

const FUNCS: Record<string, (a: any) => unknown> = {
  text_spec: (a) => s.textSpec(a.text, a.quick_reply),
  _qr: (a) => s.qr(a.label, a.data),
  genre_select_spec: (a) => s.genreSelectSpec(a.selected),
  time_select_spec: (a) => s.timeSelectSpec(a.slot),
  menu_quick_reply: () => s.menuQuickReply(),
  menu_spec: (a) => s.menuSpec(a.text),
  _help_spec: () => s.helpSpec(),
  settings_summary_text: (a) => s.settingsSummaryText(a.sub),
  _source_name: (a) => s.sourceName(a.s),
  _tag_labels: (a) => s.tagLabels(item(a.item)),
  _tag_text: (a) => s.tagText(item(a.item)),
  detail_spec: (a) => s.detailSpec(item(a.item), a.payload),
  feedback_quick_reply: (a) => s.feedbackQuickReply(a.key),
  feedback_spec: (a) => s.feedbackSpec(a.title, a.key),
  title_hash: (a) => s.titleHash(a.title),
  line_text: (a) => s.lineText(a.title, a.text),
  parse_time: (a) => parseTime(a.text),
  to_message: (a) => s.toMessage(a.spec),
};

describe("Python の出力との一致", () => {
  it("同梱した genres が genres.toml(Python の読み方)と同じ", () => {
    expect({ keys: GENRE_KEYS, labels: GENRE_LABELS, selectable: SELECTABLE_KEYS }).toStrictEqual(golden.genres);
  });

  it("golden の全関数に対応する TS がある", () => {
    expect([...new Set(golden.cases.map((c) => c.fn))].sort()).toStrictEqual(Object.keys(FUNCS).sort());
  });

  golden.cases.forEach((c, i) => {
    it(`${c.fn} #${i}`, async () => {
      // JSON にしてから比べる(undefined のキーなど JSON に出ない差を一致扱いにしない)
      const got = JSON.parse(JSON.stringify(await FUNCS[c.fn](c.args)));
      expect(got).toStrictEqual(c.out);
    });
  });
});
