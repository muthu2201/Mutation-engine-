"""CRL: SQL facts, the grammar (parser, validator, canonical printer, identity) and matching."""

import pytest

from colloid.core.rules import (
    CRLError,
    SortKey,
    covers,
    index_columns,
    parse,
    primary_keys,
    proposals,
    query_facts,
    render_file,
)

TABLES = ("categories", "customers", "products", "orders", "order_items", "reviews")

SORTED = """
# learned from the lake
rule equality-filter-sorted-index v1 {
  doc "Index the equality filter and the sort order"
  when query filters $table.$column = ? and orders by $table.$sort
  propose index $table ($column, $sort)
  unless covered
  evidence lake fc1bc599367a2d44 gain 7.48% ci [1.11%, 13.44%] on stackzero
}
"""


def test_facts_join_alias_sort_prefix_and_or_refusal():
    f = query_facts("SELECT oi.product_id FROM orders o JOIN order_items oi ON oi.order_id = o.id WHERE o.customer_id = %s "
                    "ORDER BY o.placed_at DESC, o.id DESC, oi.line_no", TABLES)
    assert f.equality == (("orders", "customer_id"),)
    assert f.sort_prefix("orders") == (SortKey("placed_at", True), SortKey("id", True))  # stops at the other table's key
    assert query_facts("SELECT id FROM products WHERE name ILIKE $1 OR description ILIKE $2", TABLES).equality == ()
    assert query_facts("SELECT * FROM orders WHERE placed_at >= ? AND placed_at < ?", TABLES).equality == ()  # ranges are not equality
    same = [query_facts(q, TABLES) for q in ("SELECT name FROM customers WHERE id = %s", "SELECT name FROM customers WHERE id = $1")]
    assert same[0] == same[1]  # psycopg and pgx placeholders: the same fact


def test_schema_and_index_readers():
    pk = primary_keys("CREATE TABLE a (id integer PRIMARY KEY, x int);\nCREATE TABLE b (k int, n int, PRIMARY KEY (k, n));")
    assert pk == {"a": (SortKey("id"),), "b": (SortKey("k"), SortKey("n"))}
    assert index_columns("CREATE INDEX i ON t (a, b DESC)") == ("t", (SortKey("a"), SortKey("b", True)))
    assert index_columns("CREATE INDEX i ON t USING gin (name gin_trgm_ops)") is None
    assert covers((SortKey("a"), SortKey("b", True)), (SortKey("a"),))
    assert covers((SortKey("a", True), SortKey("b")), (SortKey("a"), SortKey("b", True)))  # fully reversed: backward scan
    assert not covers((SortKey("a"), SortKey("b")), (SortKey("a"), SortKey("b", True)))


def test_round_trip_and_identity():
    rules = parse(SORTED)
    text = render_file(rules)
    assert render_file(parse(text)) == text
    more = parse(text.replace("on stackzero\n}", "on stackzero\n  evidence lake 0123456789ab gain 5% ci [1%, 9%] on stackzero-go\n}"))
    assert more[0].rule_id == rules[0].rule_id  # evidence does not change what a rule says


@pytest.mark.parametrize("broken,message", [
    (SORTED.replace("v1", "1"), "version"),
    (SORTED.replace("($column, $sort)", "($sort, $column)"), "lead with the filtered column"),
    (SORTED.replace("($column, $sort)", "($column, $other)"), "not bound"),
    (SORTED.replace("ci [1.11%, 13.44%]", "ci [-0.5%, 13.44%]"), "not above zero"),
    ("\n".join(line for line in SORTED.splitlines() if "evidence" not in line), "evidence"),
    (SORTED + SORTED, "defined twice"),
    (SORTED.replace("propose", "suggest"), "expected 'propose'"),
])
def test_invalid_rules_are_refused_with_a_location(broken, message):
    with pytest.raises(CRLError, match=message):
        parse(broken)


def test_matching_respects_primary_keys_and_sort_requirements():
    rules = parse(SORTED)
    queries = {
        "q1": query_facts("SELECT id FROM orders WHERE customer_id = %s ORDER BY placed_at DESC, id DESC", TABLES),
        "q2": query_facts("SELECT product_id FROM order_items WHERE order_id = %s ORDER BY line_no", TABLES),  # the PK serves it
        "q3": query_facts("SELECT count(*) FROM reviews WHERE product_id = %s", TABLES),  # no sort: the sorted rule does not fire
    }
    existing = {"order_items": [(SortKey("order_id"), SortKey("line_no"))]}
    found = proposals(rules, queries, existing)
    assert [(p.table, p.render()) for p in found] == [("orders", "orders (customer_id, placed_at desc, id desc)")]
    assert found[0].queries == ("q1",)
