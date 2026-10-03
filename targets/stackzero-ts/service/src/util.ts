// Shared helpers: text tokenisation, money formatting, request parsing, timestamps.

export class HTTPError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

// int(float('inf')) in the reference raises OverflowError: a server error, not a client one.
export class OverflowError extends Error {}

const STOPWORDS = ["a", "an", "and", "the", "for", "with", "of", "to", "in", "on", "by", "at", "or"];

// Lower-case alphanumeric words, without stopwords or duplicates, in first-seen order.
export function tokenize(text: string): string[] {
  const words = text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
  const terms: string[] = [];
  for (const word of words) {
    if (STOPWORDS.includes(word) || word.length < 2) {
      continue;
    }
    if (!terms.includes(word)) {
      terms.push(word);
    }
  }
  return terms;
}

export function formatMoney(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(cents));
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

// round(x, ndigits) on the exact binary value (ties are only possible for exactly representable
// halves, where Python rounds to even and toFixed up; the data never produces one).
export function pyRound(x: number, ndigits: number): number {
  return Number(x.toFixed(ndigits));
}

function pyIntString(s: string): { value: number; big: boolean } | null {
  s = s.replace(/^[\s\x1c-\x1f]+|[\s\x1c-\x1f]+$/g, "");
  let neg = false;
  if (s.startsWith("+") || s.startsWith("-")) {
    neg = s[0] === "-";
    s = s.slice(1);
  }
  if (!/^\d(?:_?\d)*$/.test(s)) {
    return null;
  }
  const digits = s.replace(/_/g, "").replace(/^0+(?=\d)/, "");
  if (digits.length > 15) {
    return { value: 0, big: true };
  }
  return { value: neg ? -Number(digits) : Number(digits), big: false };
}

// Python int() semantics for query-string and JSON values, then an inclusive range check.
export function parseInt(value: unknown, name: string, low: number, high: number): number {
  const notInt = new HTTPError(400, `${name} must be an integer`);
  const outOfRange = new HTTPError(400, `${name} must be between ${low} and ${high}`);
  let n: number;
  if (typeof value === "string") {
    const parsed = pyIntString(value);
    if (parsed === null) throw notInt;
    if (parsed.big) throw outOfRange;
    n = parsed.value;
  } else if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      if (Number.isNaN(value)) throw notInt;
      throw new OverflowError("cannot convert float infinity to integer");
    }
    n = Math.trunc(value);
  } else if (typeof value === "boolean") {
    n = value ? 1 : 0;
  } else {
    throw notInt;
  }
  if (n < low || n > high) throw outOfRange;
  return n;
}

function daysIn(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

// An ISO calendar date (YYYY-MM-DD or YYYYMMDD) as [year, month, day]; a missing value is invalid.
export function parseDate(value: string | undefined, name: string): [number, number, number] {
  const bad = new HTTPError(400, `${name} must be an ISO date (YYYY-MM-DD)`);
  if (value === undefined) throw bad;
  const m = /^(\d{4})-?(\d{2})-?(\d{2})$/.exec(value);
  if (m === null || (value.length !== 10 && value.length !== 8) || (value.length === 10 && (value[4] !== "-" || value[7] !== "-"))) {
    throw bad;
  }
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  if (y < 1 || mo < 1 || mo > 12 || d < 1 || d > daysIn(y, mo)) throw bad;
  return [y, mo, d];
}

const ISO_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2})(?::(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?)?(Z|[+-]\d{2}(?::?\d{2}(?::?\d{2}(?:\.\d{1,6})?)?)?)?$/;

// An ISO 8601 timestamp with a timezone offset, normalised to a string Postgres reads as the
// same instant (fraction truncated to microseconds, as the reference does).
export function parseTimestamp(value: unknown, name: string): string {
  const bad = new HTTPError(400, `${name} must be an ISO timestamp`);
  if (typeof value !== "string") throw bad;
  const m = ISO_TIMESTAMP.exec(value);
  if (m === null) throw bad;
  const num = (x: string | undefined) => (x ? Number(x) : 0);
  const [y, mo, d, h, mi, s] = [num(m[1]), num(m[2]), num(m[3]), num(m[4]), num(m[5]), num(m[6])];
  if (y < 1 || mo < 1 || mo > 12 || d < 1 || d > daysIn(y, mo) || h > 23 || mi > 59 || s > 59) throw bad;
  if (!m[8]) throw new HTTPError(400, `${name} must include a timezone offset`);
  let offset = "+00:00";
  if (m[8] !== "Z") {
    const parts = m[8].slice(1).replace(/:/g, "");
    const oh = Number(parts.slice(0, 2)), om = Number(parts.slice(2, 4) || "0"), os = Number(parts.slice(4, 6) || "0");
    if (oh > 23 || om > 59 || os > 59) throw bad;
    offset = `${m[8][0]}${parts.slice(0, 2)}:${(parts.slice(2, 4) || "00")}${parts.length >= 6 ? ":" + parts.slice(4, 6) : ""}`;
  }
  const frac = (m[7] ?? "").slice(0, 6).padEnd(6, "0");
  const p2 = (n: number) => String(n).padStart(2, "0");
  return `${String(y).padStart(4, "0")}-${p2(mo)}-${p2(d)}T${p2(h)}:${p2(mi)}:${p2(s)}.${frac}${offset}`;
}

// Postgres text output of a timestamptz in a UTC session ("2024-03-05 10:20:30.12+00") as
// Python's datetime.isoformat() renders it ("2024-03-05T10:20:30.120000+00:00").
export function isoformat(pg: string): string {
  const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2})(?::?(\d{2}))?$/.exec(pg);
  if (m === null) throw new Error(`unexpected timestamp ${pg}`);
  const frac = m[3] ? "." + m[3].padEnd(6, "0") : "";
  return `${m[1]}T${m[2]}${frac}${m[4]}:${m[5] ?? "00"}`;
}

// The UTC calendar date of a Postgres timestamptz text value (the session runs in UTC).
export function utcDate(pg: string): string {
  return pg.slice(0, 10);
}
