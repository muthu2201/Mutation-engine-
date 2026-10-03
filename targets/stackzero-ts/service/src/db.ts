// Database access on node-postgres (the most widely used Postgres client for JavaScript).

import pg from "pg";

// int8 (counts, sums, bigserial ids) arrive as strings by default: every value here is far below
// 2^53, so plain numbers are exact. timestamptz stays text: a JS Date has millisecond precision
// and the contract's timestamps carry microseconds.
pg.types.setTypeParser(20, (v: string) => Number(v));
pg.types.setTypeParser(1184, (v: string) => v);

export type Row = any[];

export interface Querier {
  query(config: { text: string; values?: unknown[]; rowMode: "array" }): Promise<{ rows: Row[]; rowCount: number | null }>;
}

export async function fetch(db: Querier, sql: string, params: unknown[] = []): Promise<Row[]> {
  const res = await db.query({ text: sql, values: params, rowMode: "array" });
  return res.rows;
}

export async function fetchRow(db: Querier, sql: string, params: unknown[] = []): Promise<Row | undefined> {
  return (await fetch(db, sql, params))[0];
}

export async function fetchVal(db: Querier, sql: string, params: unknown[] = []): Promise<any> {
  const row = await fetchRow(db, sql, params);
  return row === undefined ? null : row[0];
}

export async function execute(db: Querier, sql: string, params: unknown[] = []): Promise<number> {
  const res = await db.query({ text: sql, values: params, rowMode: "array" });
  return res.rowCount ?? 0;
}

export async function transaction<T>(pool: pg.Pool, fn: (tx: Querier) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const out = await fn(client);
    await client.query("COMMIT");
    return out;
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }
}

export function openPool(dsn: string, options: string, max: number): pg.Pool {
  const kv: Record<string, string> = {};
  for (const part of dsn.split(/\s+/).filter(Boolean)) {
    const [k, ...v] = part.split("=");
    kv[k] = v.join("=");
  }
  return new pg.Pool({ host: kv.host, database: kv.dbname, user: kv.user, max, options: options || undefined });
}
