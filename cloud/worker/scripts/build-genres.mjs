// config/genres.toml → src/genres.generated.json。
// Worker は実行時にリポジトリのファイルを読めないので、webhook が使う分(キー順・表示名・選択可否)だけ同梱する。
import { readFileSync, writeFileSync } from "node:fs";
import { parse } from "smol-toml";

const src = new URL("../../../config/genres.toml", import.meta.url);
const out = new URL("../src/genres.generated.json", import.meta.url);

const keys = [];
const labels = {};
const selectable = [];
for (const g of parse(readFileSync(src, "utf8")).genre ?? []) {
  if (!g.key) throw new Error("genres.toml: key の無い [[genre]] があります");
  if (g.key in labels) throw new Error(`genres.toml: key が重複しています: ${g.key}`);
  keys.push(g.key);
  labels[g.key] = g.label ?? g.key;
  if (g.selectable ?? true) selectable.push(g.key);
}
if (!keys.length) throw new Error("genres.toml にジャンルが1つもありません");
writeFileSync(out, JSON.stringify({ keys, labels, selectable }, null, 1) + "\n");
