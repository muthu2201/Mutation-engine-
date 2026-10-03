"""The language-neutral SQL policy (L0 static check and the dynamic audit's rule set)."""

import pytest

from colloid_evaluator.policy import python_strings, scan_sql, sql_violations


@pytest.mark.parametrize("sql", [
    "SELECT count(*), avg(rating)::float8 FROM reviews WHERE product_id = %s",
    "SELECT id FROM products WHERE id = ANY($1) ORDER BY id",
    "INSERT INTO orders (customer_id, status) VALUES ($1, 'placed') RETURNING id",
    "UPDATE products SET stock = stock - %s WHERE id = %s",
    "WITH recent AS (SELECT id FROM orders) INSERT INTO t SELECT * FROM recent",
    "SELECT 1;",
    "create_order",            # a route name, not a statement
    "listen: %v",              # a log format, not LISTEN
    "insufficient stock for product %d",
])
def test_plain_dml_and_non_sql_strings_pass(sql):
    assert sql_violations(sql) == []


@pytest.mark.parametrize("sql,why", [
    ("SELECT set_config('colloid.memo', %s, false)", "set_config"),
    ("SELECT current_setting('colloid.memo')", "current_setting"),
    ("SELECT pg_sleep(0.01)", "pg_sleep"),
    ("SELECT pg_advisory_lock(42)", "pg_advisory_lock"),
    ("CREATE TEMP TABLE memo AS SELECT 1", "CREATE"),
    ("SET work_mem = '1GB'", "SET"),
    ("SELECT 1; SELECT 2", "one statement"),
    ("SELECT id INTO memo FROM products", "INTO"),
    ("DISCARD ALL", "DISCARD"),
    ("COMMIT", "COMMIT"),
    ("VACUUM", "VACUUM"),
])
def test_state_ddl_and_multi_statements_are_rejected(sql, why):
    reasons = sql_violations(sql)
    assert reasons and any(why.lower() in r.lower() for r in reasons), reasons


def test_python_constant_concatenations_are_folded_before_the_check():
    src = 'async def f(db):\n    q = "SELECT set_" + "config(\'x\', \'1\', false)"\n    return await db.fetch(q)\n'
    assert any("set_config" in s for s in python_strings(src))
    assert scan_sql(python_strings(src))
