"""Product search: SQL candidate retrieval, C ranking, rating enrichment."""

from shop import native, util


async def find_candidates(db, terms):
    """Products whose name or description contains any query term (first 300 per term)."""
    candidates = []
    for term in terms:
        pattern = "%" + term + "%"
        rows = await db.fetch(
            "SELECT id, name, description, price_cents, category_id FROM products "
            "WHERE name ILIKE %s OR description ILIKE %s ORDER BY id LIMIT 300",
            (pattern, pattern),
        )
        for row in rows:
            if row not in candidates:
                candidates.append(row)
    return candidates


def rank(terms, candidates, limit):
    docs = []
    for candidate in candidates:
        docs.append(candidate[1] + " " + candidate[2])
    scores = native.score(terms, docs)
    pairs = []
    for i in range(len(candidates)):
        pairs.append((scores[i], candidates[i]))
    pairs.sort(key=lambda pair: (-pair[0], pair[1][0]))
    return pairs[:limit]


async def rating_summary(db, product_id):
    row = await db.fetchrow("SELECT count(*), avg(rating)::float8 FROM reviews WHERE product_id = %s", (product_id,))
    return row[0], row[1]


async def search_products(db, query, limit):
    terms = util.tokenize(query)
    if not terms:
        return {"query": query, "terms": [], "results": []}
    candidates = await find_candidates(db, terms)
    ranked = rank(terms, candidates, limit)
    results = []
    for score, candidate in ranked:
        count, average = await rating_summary(db, candidate[0])
        results.append(
            {
                "id": candidate[0],
                "name": candidate[1],
                "price": util.format_money(candidate[3]),
                "category_id": candidate[4],
                "score": round(score, 6),
                "reviews": count,
                "rating": round(average, 3) if average is not None else None,
            }
        )
    return {"query": query, "terms": terms, "results": results}
