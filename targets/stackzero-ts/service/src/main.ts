// The StackZero shop API on a Unix socket, in TypeScript: the same HTTP contract, database
// schema and SQL as the Python reference (targets/stackzero). The same code runs on Node
// (type stripping) and on Bun; only node: built-ins and node-postgres are used.

import http from "node:http";
import { existsSync, unlinkSync } from "node:fs";
import { openPool } from "./db.ts";
import { categoryTop, createOrder, customerSummary, dailyReport, productDetail, recommendations } from "./handlers.ts";
import { searchProducts } from "./search.ts";
import { HTTPError, parseDate, parseInt } from "./util.ts";

const ROUTES: [string, RegExp, string][] = [
  ["GET", /^\/healthz$/, "health"],
  ["GET", /^\/products\/search$/, "search"],
  ["GET", /^\/products\/(\d+)$/, "product"],
  ["GET", /^\/customers\/(\d+)\/summary$/, "customer_summary"],
  ["GET", /^\/customers\/(\d+)\/recommendations$/, "recommendations"],
  ["GET", /^\/categories\/(\d+)\/top$/, "category_top"],
  ["GET", /^\/reports\/daily$/, "daily_report"],
  ["POST", /^\/orders$/, "create_order"],
];

const env = process.env;
const pool = openPool(env.SHOP_DSN ?? "dbname=shop user=shop", env.SHOP_PG_OPTIONS ?? "", Number(env.SHOP_POOL_MAX ?? "10"));

function unquotePlus(s: string): string {
  s = s.replace(/\+/g, " ");
  const bytes: number[] = [];
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "%" && i + 2 < s.length && /^[0-9a-fA-F]{2}$/.test(s.slice(i + 1, i + 3))) {
      bytes.push(parseInt16(s.slice(i + 1, i + 3)));
      i += 2;
    } else {
      bytes.push(...Buffer.from(s[i], "utf8"));
    }
  }
  return Buffer.from(bytes).toString("utf8");
}

function parseInt16(h: string): number {
  return Number.parseInt(h, 16);
}

// urllib.parse.parse_qs: '&'-separated pairs, '+' is a space, blank values dropped.
function queryValues(raw: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const pair of raw.split("&")) {
    const at = pair.indexOf("=");
    if (at < 0 || at === pair.length - 1) continue;
    const name = unquotePlus(pair.slice(0, at)), value = unquotePlus(pair.slice(at + 1));
    out.set(name, [...(out.get(name) ?? []), value]);
  }
  return out;
}

const param = (q: Map<string, string[]>, name: string) => q.get(name)?.[0];

// Ids beyond the integer column range cannot exist: they answer 404 like the reference.
function pathId(s: string): number {
  const n = Number(s);
  return Number.isSafeInteger(n) && n <= 2147483647 ? n : -1;
}

async function dispatch(route: string, match: RegExpExecArray, q: Map<string, string[]>, body: Buffer): Promise<unknown> {
  switch (route) {
    case "health":
      return { ok: true };
    case "search":
      return searchProducts(pool, param(q, "q") ?? "", parseInt(param(q, "limit") ?? "10", "limit", 1, 50));
    case "product":
      return productDetail(pool, pathId(match[1]));
    case "customer_summary":
      return customerSummary(pool, pathId(match[1]));
    case "recommendations":
      return recommendations(pool, pathId(match[1]));
    case "category_top":
      return categoryTop(pool, pathId(match[1]), parseInt(param(q, "limit") ?? "10", "limit", 1, 100));
    case "daily_report": {
      const asOf = parseDate(param(q, "as_of"), "as_of");
      return dailyReport(pool, asOf, parseInt(param(q, "days") ?? "7", "days", 1, 366));
    }
    case "create_order": {
      let payload: unknown;
      try {
        payload = body.length === 0 ? null : JSON.parse(body.toString("utf8"));
      } catch {
        throw new HTTPError(400, "invalid JSON body");
      }
      return createOrder(pool, payload);
    }
  }
  throw new HTTPError(404, "not found");
}

function respond(res: http.ServerResponse, status: number, payload: unknown): void {
  const body = Buffer.from(JSON.stringify(payload));
  res.writeHead(status, { "content-type": "application/json", "content-length": body.length });
  res.end(body);
}

const server = http.createServer((req, res) => {
  const url = req.url ?? "/";
  const qAt = url.indexOf("?");
  const rawPath = qAt < 0 ? url : url.slice(0, qAt);
  let path = rawPath;
  try {
    path = decodeURIComponent(rawPath);
  } catch {
    // a malformed escape stays literal (as urllib's unquote leaves it)
  }
  const rawQuery = qAt < 0 ? "" : url.slice(qAt + 1);
  let matched: [string, RegExpExecArray] | null = null;
  for (const [method, pattern, route] of ROUTES) {
    const m = pattern.exec(path);
    if (m !== null && method === req.method) {
      matched = [route, m];
      break;
    }
  }
  if (matched === null) {
    respond(res, 404, { error: "not found" });
    req.resume();
    return;
  }
  const chunks: Buffer[] = [];
  req.on("data", (c: Buffer) => chunks.push(c));
  req.on("end", () => {
    const [route, match] = matched!;
    dispatch(route, match, queryValues(rawQuery), req.method === "POST" ? Buffer.concat(chunks) : Buffer.alloc(0)).then(
      (payload) => respond(res, route === "create_order" ? 201 : 200, payload),
      (err: unknown) => {
        if (err instanceof HTTPError) {
          respond(res, err.status, { error: err.message });
          return;
        }
        console.error(`error serving ${req.method} ${path}:`, err);
        respond(res, 500, { error: "internal server error" });
      },
    );
  });
});
server.keepAliveTimeout = 30_000;

const uds = process.argv[process.argv.indexOf("--uds") + 1];
if (!process.argv.includes("--uds") || !uds) {
  console.error("usage: main.ts --uds PATH");
  process.exit(2);
}
await pool.query("SELECT 1");
if (existsSync(uds)) unlinkSync(uds);
server.listen(uds, () => console.error(`shop listening on ${uds}`));
const stop = () => server.close(() => pool.end().then(() => process.exit(0)));
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
