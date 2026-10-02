// Product search: SQL candidate retrieval, BM25 ranking, rating enrichment.

import { fetch, fetchRow, type Querier, type Row } from "./db.ts";
import { scoreBatch } from "./score.ts";
import { formatMoney, pyRound, tokenize } from "./util.ts";

// Products whose name or description contains any query term (first 300 per term).
export async function findCandidates(db: Querier, terms: string[]): Promise<Row[]> {
  const candidates: Row[] = [];
  for (const term of terms) {
    const pattern = "%" + term + "%";
    const rows = await fetch(db,
      "SELECT id, name, description, price_cents, category_id FROM products " +
        "WHERE name ILIKE $1 OR description ILIKE $2 ORDER BY id LIMIT 300",
      [pattern, pattern]);
    for (const row of rows) {
      if (!candidates.some((c) => c.every((v, i) => v === row[i]))) candidates.push(row);
    }
  }
  return candidates;
}

export function rank(terms: string[], candidates: Row[], limit: number): [number, Row][] {
  const docs = candidates.map((c) => c[1] + " " + c[2]);
  const scores = scoreBatch(terms, docs);
  const pairs: [number, Row][] = candidates.map((c, i) => [scores[i], c]);
  pairs.sort((a, b) => (b[0] - a[0]) || (a[1][0] - b[1][0]));
  return pairs.slice(0, limit);
}

export async function ratingSummary(db: Querier, productId: number): Promise<[number, number | null]> {
  const row = await fetchRow(db, "SELECT count(*), avg(rating)::float8 FROM reviews WHERE product_id = $1", [productId]);
  return [row![0], row![1]];
}

export async function searchProducts(db: Querier, query: string, limit: number) {
  const terms = tokenize(query);
  if (terms.length === 0) return { query, terms: [], results: [] };
  const candidates = await findCandidates(db, terms);
  const ranked = rank(terms, candidates, limit);
  const results = [];
  for (const [score, candidate] of ranked) {
    const [count, average] = await ratingSummary(db, candidate[0]);
    results.push({
      id: candidate[0], name: candidate[1], price: formatMoney(candidate[3]), category_id: candidate[4],
      score: pyRound(score, 6), reviews: count, rating: average === null ? null : pyRound(average, 3),
    });
  }
  return { query, terms, results };
}
