// テスト用の D1。node:sqlite のメモリ DB に本番と同じスキーマ(cloud/d1/schema.sql)を作り、
// Worker が使う D1 の API(prepare/bind/first/all/run)だけを被せる。SQL は本物の SQLite が解釈する。
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";

const SCHEMA = readFileSync(fileURLToPath(import.meta.resolve("../../d1/schema.sql")), "utf8");

class Stmt {
  constructor(private db: DatabaseSync, private sql: string, private params: any[] = []) {}
  bind(...params: any[]) {
    return new Stmt(this.db, this.sql, params);
  }
  async first(col?: string) {
    const row = this.db.prepare(this.sql).all(...this.params)[0] as Record<string, unknown> | undefined;
    if (!row) return null;
    return col ? row[col] : row;
  }
  async all() {
    return { results: this.db.prepare(this.sql).all(...this.params), success: true, meta: {} };
  }
  async run() {
    const r = this.db.prepare(this.sql).run(...this.params);
    return { success: true, meta: { changes: Number(r.changes) } };
  }
}

export interface TestDB {
  d1: D1Database;
  raw: DatabaseSync;
  rows: (sql: string, ...params: any[]) => Record<string, any>[];
}

export function makeDB(): TestDB {
  const raw = new DatabaseSync(":memory:");
  raw.exec("PRAGMA foreign_keys = ON;"); // D1 は外部キーを強制する
  raw.exec(SCHEMA);
  return {
    raw,
    d1: { prepare: (sql: string) => new Stmt(raw, sql) } as unknown as D1Database,
    rows: (sql, ...params) => raw.prepare(sql).all(...params) as Record<string, any>[],
  };
}
