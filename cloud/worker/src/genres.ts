// xnewsbot/genres.py のうち webhook が使う分。中身は scripts/build-genres.mjs が genres.toml から作る。
import data from "./genres.generated.json";

export const GENRE_KEYS: string[] = data.keys;
export const GENRE_LABELS: Record<string, string> = data.labels;
export const SELECTABLE_KEYS: string[] = data.selectable;

export function isGenre(key: string): boolean {
  return Object.hasOwn(GENRE_LABELS, key);
}

export function genreLabel(key: string): string {
  return isGenre(key) ? GENRE_LABELS[key] : key;
}
