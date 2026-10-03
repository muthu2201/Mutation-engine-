// HTTP endpoint handlers. Each takes the database handle plus parsed parameters and returns a
// JSON-serialisable value (or throws HTTPError).

import type pg from "pg";
import { execute, fetch, fetchRow, fetchVal, transaction, type Querier } from "./db.ts";
import { HTTPError, formatMoney, isoformat, parseInt, parseTimestamp, pyRound, utcDate } from "./util.ts";

export async function productDetail(db: Querier, productId: number) {
  const row = await fetchRow(db,
    "SELECT p.id, p.name, p.description, p.price_cents, p.category_id, c.name " +
      "FROM products p JOIN categories c ON c.id = p.category_id WHERE p.id = $1",
    [productId]);
  if (row === undefined) throw new HTTPError(404, "product not found");
  const reviews = await fetch(db,
    "SELECT id, customer_id, rating, body, created_at FROM reviews WHERE product_id = $1 ORDER BY created_at DESC, id DESC",
    [productId]);
  const histogram: Record<string, number> = { "1": 0, "2": 0, "3": 0, "4": 0, "5": 0 };
  for (const review of reviews) histogram[String(review[2])] += 1;
  let average: number | null = null;
  if (reviews.length > 0) {
    let total = 0;
    for (const review of reviews) total += review[2];
    average = pyRound(total / reviews.length, 3);
  }
  const recent = [];
  for (const review of reviews.slice(0, 5)) {
    const author = await fetchVal(db, "SELECT name FROM customers WHERE id = $1", [review[1]]);
    recent.push({ id: review[0], author, rating: review[2], body: review[3], created_at: isoformat(review[4]) });
  }
  const relatedRows = await fetch(db,
    "SELECT id, name, price_cents FROM products WHERE category_id = $1 AND id <> $2 ORDER BY id LIMIT 5",
    [row[4], row[0]]);
  const related = [];
  for (const r of relatedRows) related.push({ id: r[0], name: r[1], price: formatMoney(r[2]) });
  return {
    id: row[0], name: row[1], description: row[2], price: formatMoney(row[3]),
    category: { id: row[4], name: row[5] },
    rating: { average, count: reviews.length, histogram },
    recent_reviews: recent,
    related,
  };
}

export async function customerSummary(db: Querier, customerId: number) {
  const customer = await fetchRow(db, "SELECT id, name, email, country FROM customers WHERE id = $1", [customerId]);
  if (customer === undefined) throw new HTTPError(404, "customer not found");
  const orders = await fetch(db,
    "SELECT id, status, placed_at, total_cents FROM orders WHERE customer_id = $1 ORDER BY placed_at DESC, id DESC",
    [customerId]);
  const orderList = [];
  let totalSpent = 0;
  const productQuantities = new Map<number, number>();
  for (const order of orders) {
    const items = await fetch(db,
      "SELECT product_id, quantity, unit_price_cents FROM order_items WHERE order_id = $1 ORDER BY line_no",
      [order[0]]);
    let itemCount = 0;
    for (const item of items) {
      itemCount += item[1];
      productQuantities.set(item[0], (productQuantities.get(item[0]) ?? 0) + item[1]);
    }
    if (order[1] !== "returned") totalSpent += order[3];
    orderList.push({ id: order[0], status: order[1], placed_at: isoformat(order[2]), total: formatMoney(order[3]), items: itemCount });
  }
  const favourites = [...productQuantities.entries()].sort((x, y) => (y[1] - x[1]) || (x[0] - y[0])).slice(0, 5);
  const favouriteProducts = [];
  for (const [productId, quantity] of favourites) {
    const name = await fetchVal(db, "SELECT name FROM products WHERE id = $1", [productId]);
    favouriteProducts.push({ product_id: productId, name, quantity });
  }
  return {
    customer: { id: customer[0], name: customer[1], email: customer[2], country: customer[3] },
    order_count: orderList.length,
    total_spent: formatMoney(totalSpent),
    recent_orders: orderList.slice(0, 10),
    favourite_products: favouriteProducts,
  };
}

export async function categoryTop(db: Querier, categoryId: number, limit: number) {
  const exists = await fetchVal(db, "SELECT count(*) FROM categories WHERE id = $1", [categoryId]);
  if (exists === 0) throw new HTTPError(404, "category not found");
  const rows = await fetch(db,
    "SELECT p.id, p.name, sum(oi.quantity) AS units, sum(oi.quantity * oi.unit_price_cents) AS revenue " +
      "FROM order_items oi JOIN products p ON p.id = oi.product_id " +
      "WHERE p.category_id = $1 GROUP BY p.id, p.name ORDER BY units DESC, p.id LIMIT $2",
    [categoryId, limit]);
  const products = [];
  for (const row of rows) products.push({ id: row[0], name: row[1], units: row[2], revenue: formatMoney(row[3]) });
  return { category_id: categoryId, products };
}

export async function recommendations(db: Querier, customerId: number) {
  const exists = await fetchVal(db, "SELECT count(*) FROM customers WHERE id = $1", [customerId]);
  if (exists === 0) throw new HTTPError(404, "customer not found");
  const rows = await fetch(db,
    "SELECT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id = o.id " +
      "WHERE o.customer_id = $1 ORDER BY o.placed_at DESC, o.id DESC, oi.line_no",
    [customerId]);
  const bought: number[] = [];
  for (const row of rows) {
    if (!bought.includes(row[0])) bought.push(row[0]);
  }
  const counts = new Map<number, number>();
  for (const productId of bought.slice(0, 3)) {
    const coRows = await fetch(db,
      "SELECT other.product_id FROM order_items mine JOIN order_items other " +
        "ON other.order_id = mine.order_id AND other.product_id <> mine.product_id " +
        "WHERE mine.product_id = $1",
      [productId]);
    for (const coRow of coRows) {
      if (bought.includes(coRow[0])) continue;
      counts.set(coRow[0], (counts.get(coRow[0]) ?? 0) + 1);
    }
  }
  const ranked = [...counts.entries()].sort((x, y) => (y[1] - x[1]) || (x[0] - y[0])).slice(0, 10);
  const suggestions = [];
  for (const [productId, score] of ranked) {
    const name = await fetchVal(db, "SELECT name FROM products WHERE id = $1", [productId]);
    suggestions.push({ product_id: productId, name, score });
  }
  return { customer_id: customerId, based_on: bought.slice(0, 3), recommendations: suggestions };
}

export async function dailyReport(db: Querier, asOf: [number, number, number], days: number) {
  const endMs = Date.UTC(asOf[0], asOf[1] - 1, asOf[2] + 1);
  const startMs = endMs - days * 86_400_000;
  const rows = await fetch(db,
    "SELECT placed_at, total_cents, status FROM orders WHERE placed_at >= $1 AND placed_at < $2 ORDER BY placed_at, id",
    [new Date(startMs).toISOString(), new Date(endMs).toISOString()]);
  const perDay = new Map<string, { date: string; orders: number; returned: number; revenue_cents: number }>();
  for (const [placedAt, totalCents, status] of rows) {
    const day = utcDate(placedAt);
    let entry = perDay.get(day);
    if (entry === undefined) {
      entry = { date: day, orders: 0, returned: 0, revenue_cents: 0 };
      perDay.set(day, entry);
    }
    entry.orders += 1;
    if (status === "returned") entry.returned += 1;
    else entry.revenue_cents += totalCents;
  }
  const daysOut = [];
  for (const day of [...perDay.keys()].sort()) {
    const e = perDay.get(day)!;
    daysOut.push({ date: e.date, orders: e.orders, returned: e.returned, revenue: formatMoney(e.revenue_cents) });
  }
  const p2 = (n: number) => String(n).padStart(2, "0");
  return { as_of: `${String(asOf[0]).padStart(4, "0")}-${p2(asOf[1])}-${p2(asOf[2])}`, days, daily: daysOut };
}

export async function createOrder(pool: pg.Pool, payload: unknown) {
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    throw new HTTPError(400, "body must be a JSON object");
  }
  const body = payload as Record<string, unknown>;
  const customerId = parseInt(body.customer_id ?? null, "customer_id", 1, 1_000_000_000);
  const placedAt = parseTimestamp(body.placed_at, "placed_at");
  const items = body.items;
  if (!Array.isArray(items) || items.length < 1 || items.length > 10) {
    throw new HTTPError(400, "items must be a list of 1..10 entries");
  }
  const wanted = new Map<number, number>();
  for (const item of items) {
    if (typeof item !== "object" || item === null || Array.isArray(item)) throw new HTTPError(400, "each item must be an object");
    const productId = parseInt((item as Record<string, unknown>).product_id ?? null, "product_id", 1, 1_000_000_000);
    const quantity = parseInt((item as Record<string, unknown>).quantity ?? null, "quantity", 1, 100);
    wanted.set(productId, (wanted.get(productId) ?? 0) + quantity);
  }
  return transaction(pool, async (tx) => {
    const customer = await fetchVal(tx, "SELECT id FROM customers WHERE id = $1", [customerId]);
    if (customer === null) throw new HTTPError(404, "customer not found");
    const lines: [number, number, number][] = [];
    let total = 0;
    // Lock product rows in id order so concurrent orders cannot deadlock.
    for (const productId of [...wanted.keys()].sort((a, b) => a - b)) {
      const product = await fetchRow(tx, "SELECT price_cents, stock FROM products WHERE id = $1 FOR UPDATE", [productId]);
      if (product === undefined) throw new HTTPError(404, `product ${productId} not found`);
      if (product[1] < wanted.get(productId)!) throw new HTTPError(409, `insufficient stock for product ${productId}`);
      total += product[0] * wanted.get(productId)!;
      lines.push([productId, wanted.get(productId)!, product[0]]);
    }
    const orderId = await fetchVal(tx,
      "INSERT INTO orders (customer_id, status, placed_at, total_cents) VALUES ($1, 'placed', $2, $3) RETURNING id",
      [customerId, placedAt, total]);
    let lineNo = 0;
    for (const [productId, quantity, price] of lines) {
      lineNo += 1;
      await execute(tx,
        "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) VALUES ($1, $2, $3, $4, $5)",
        [orderId, lineNo, productId, quantity, price]);
      await execute(tx, "UPDATE products SET stock = stock - $1 WHERE id = $2", [quantity, productId]);
    }
    return { order_id: orderId, customer_id: customerId, total: formatMoney(total), lines: lines.length };
  });
}
