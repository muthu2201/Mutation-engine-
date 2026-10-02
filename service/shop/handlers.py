"""HTTP endpoint handlers. Each takes the database handle plus parsed parameters and
returns a JSON-serialisable value (or raises util.HTTPError)."""

import datetime

from shop import util


async def product_detail(db, product_id):
    row = await db.fetchrow(
        "SELECT p.id, p.name, p.description, p.price_cents, p.category_id, c.name "
        "FROM products p JOIN categories c ON c.id = p.category_id WHERE p.id = %s",
        (product_id,),
    )
    if row is None:
        raise util.HTTPError(404, "product not found")
    reviews = await db.fetch(
        "SELECT id, customer_id, rating, body, created_at FROM reviews WHERE product_id = %s ORDER BY created_at DESC, id DESC",
        (product_id,),
    )
    histogram = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for review in reviews:
        histogram[str(review[2])] += 1
    average = None
    if reviews:
        total = 0
        for review in reviews:
            total += review[2]
        average = round(total / len(reviews), 3)
    recent = []
    for review in reviews[:5]:
        author = await db.fetchval("SELECT name FROM customers WHERE id = %s", (review[1],))
        recent.append(
            {"id": review[0], "author": author, "rating": review[2], "body": review[3], "created_at": review[4].isoformat()}
        )
    related_rows = await db.fetch(
        "SELECT id, name, price_cents FROM products WHERE category_id = %s AND id <> %s ORDER BY id LIMIT 5",
        (row[4], row[0]),
    )
    related = []
    for related_row in related_rows:
        related.append({"id": related_row[0], "name": related_row[1], "price": util.format_money(related_row[2])})
    return {
        "id": row[0],
        "name": row[1],
        "description": row[2],
        "price": util.format_money(row[3]),
        "category": {"id": row[4], "name": row[5]},
        "rating": {"average": average, "count": len(reviews), "histogram": histogram},
        "recent_reviews": recent,
        "related": related,
    }


async def customer_summary(db, customer_id):
    customer = await db.fetchrow("SELECT id, name, email, country FROM customers WHERE id = %s", (customer_id,))
    if customer is None:
        raise util.HTTPError(404, "customer not found")
    orders = await db.fetch(
        "SELECT id, status, placed_at, total_cents FROM orders WHERE customer_id = %s ORDER BY placed_at DESC, id DESC",
        (customer_id,),
    )
    order_list = []
    total_spent = 0
    product_quantities = {}
    for order in orders:
        items = await db.fetch(
            "SELECT product_id, quantity, unit_price_cents FROM order_items WHERE order_id = %s ORDER BY line_no",
            (order[0],),
        )
        item_count = 0
        for item in items:
            item_count += item[1]
            if item[0] in product_quantities:
                product_quantities[item[0]] = product_quantities[item[0]] + item[1]
            else:
                product_quantities[item[0]] = item[1]
        if order[1] != "returned":
            total_spent += order[3]
        order_list.append(
            {
                "id": order[0],
                "status": order[1],
                "placed_at": order[2].isoformat(),
                "total": util.format_money(order[3]),
                "items": item_count,
            }
        )
    favourites = sorted(product_quantities.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    favourite_products = []
    for product_id, quantity in favourites:
        name = await db.fetchval("SELECT name FROM products WHERE id = %s", (product_id,))
        favourite_products.append({"product_id": product_id, "name": name, "quantity": quantity})
    return {
        "customer": {"id": customer[0], "name": customer[1], "email": customer[2], "country": customer[3]},
        "order_count": len(order_list),
        "total_spent": util.format_money(total_spent),
        "recent_orders": order_list[:10],
        "favourite_products": favourite_products,
    }


async def category_top(db, category_id, limit):
    exists = await db.fetchval("SELECT count(*) FROM categories WHERE id = %s", (category_id,))
    if exists == 0:
        raise util.HTTPError(404, "category not found")
    rows = await db.fetch(
        "SELECT p.id, p.name, sum(oi.quantity) AS units, sum(oi.quantity * oi.unit_price_cents) AS revenue "
        "FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "WHERE p.category_id = %s GROUP BY p.id, p.name ORDER BY units DESC, p.id LIMIT %s",
        (category_id, limit),
    )
    products = []
    for row in rows:
        products.append({"id": row[0], "name": row[1], "units": int(row[2]), "revenue": util.format_money(row[3])})
    return {"category_id": category_id, "products": products}


async def recommendations(db, customer_id):
    exists = await db.fetchval("SELECT count(*) FROM customers WHERE id = %s", (customer_id,))
    if exists == 0:
        raise util.HTTPError(404, "customer not found")
    rows = await db.fetch(
        "SELECT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id = o.id "
        "WHERE o.customer_id = %s ORDER BY o.placed_at DESC, o.id DESC, oi.line_no",
        (customer_id,),
    )
    bought = []
    for row in rows:
        if row[0] not in bought:
            bought.append(row[0])
    counts = {}
    for product_id in bought[:3]:
        co_rows = await db.fetch(
            "SELECT other.product_id FROM order_items mine JOIN order_items other "
            "ON other.order_id = mine.order_id AND other.product_id <> mine.product_id "
            "WHERE mine.product_id = %s",
            (product_id,),
        )
        for co_row in co_rows:
            if co_row[0] in bought:
                continue
            counts[co_row[0]] = counts.get(co_row[0], 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    suggestions = []
    for product_id, score in ranked:
        name = await db.fetchval("SELECT name FROM products WHERE id = %s", (product_id,))
        suggestions.append({"product_id": product_id, "name": name, "score": score})
    return {"customer_id": customer_id, "based_on": bought[:3], "recommendations": suggestions}


async def daily_report(db, as_of, days):
    end = datetime.datetime.combine(as_of + datetime.timedelta(days=1), datetime.time(), datetime.timezone.utc)
    start = end - datetime.timedelta(days=days)
    rows = await db.fetch(
        "SELECT placed_at, total_cents, status FROM orders WHERE placed_at >= %s AND placed_at < %s ORDER BY placed_at, id",
        (start, end),
    )
    per_day = {}
    for placed_at, total_cents, status in rows:
        day = placed_at.astimezone(datetime.timezone.utc).date().isoformat()
        if day not in per_day:
            per_day[day] = {"date": day, "orders": 0, "returned": 0, "revenue_cents": 0}
        entry = per_day[day]
        entry["orders"] += 1
        if status == "returned":
            entry["returned"] += 1
        else:
            entry["revenue_cents"] += total_cents
    days_out = []
    for day in sorted(per_day):
        entry = per_day[day]
        days_out.append(
            {"date": entry["date"], "orders": entry["orders"], "returned": entry["returned"], "revenue": util.format_money(entry["revenue_cents"])}
        )
    return {"as_of": as_of.isoformat(), "days": days, "daily": days_out}


async def create_order(db, payload):
    if not isinstance(payload, dict):
        raise util.HTTPError(400, "body must be a JSON object")
    customer_id = util.parse_int(payload.get("customer_id"), "customer_id", 1, 10**9)
    placed_at = util.parse_timestamp(payload.get("placed_at"), "placed_at")
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise util.HTTPError(400, "items must be a list of 1..10 entries")
    wanted = {}
    for item in items:
        if not isinstance(item, dict):
            raise util.HTTPError(400, "each item must be an object")
        product_id = util.parse_int(item.get("product_id"), "product_id", 1, 10**9)
        quantity = util.parse_int(item.get("quantity"), "quantity", 1, 100)
        wanted[product_id] = wanted.get(product_id, 0) + quantity
    async with db.transaction() as tx:
        customer = await tx.fetchval("SELECT id FROM customers WHERE id = %s", (customer_id,))
        if customer is None:
            raise util.HTTPError(404, "customer not found")
        lines = []
        total = 0
        # Lock product rows in id order so concurrent orders cannot deadlock.
        for product_id in sorted(wanted):
            product = await tx.fetchrow("SELECT price_cents, stock FROM products WHERE id = %s FOR UPDATE", (product_id,))
            if product is None:
                raise util.HTTPError(404, f"product {product_id} not found")
            if product[1] < wanted[product_id]:
                raise util.HTTPError(409, f"insufficient stock for product {product_id}")
            total += product[0] * wanted[product_id]
            lines.append((product_id, wanted[product_id], product[0]))
        order_id = await tx.fetchval(
            "INSERT INTO orders (customer_id, status, placed_at, total_cents) VALUES (%s, 'placed', %s, %s) RETURNING id",
            (customer_id, placed_at, total),
        )
        line_no = 0
        for product_id, quantity, price in lines:
            line_no += 1
            await tx.execute(
                "INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents) VALUES (%s, %s, %s, %s, %s)",
                (order_id, line_no, product_id, quantity, price),
            )
            await tx.execute("UPDATE products SET stock = stock - %s WHERE id = %s", (quantity, product_id))
    return {"order_id": order_id, "customer_id": customer_id, "total": util.format_money(total), "lines": len(lines)}
