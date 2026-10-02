// Unit tests for pure helpers (the same cases as the Python and Go implementations' tests).

import assert from "node:assert/strict";
import { test } from "node:test";
import { scoreBatch } from "./score.ts";
import { HTTPError, formatMoney, isoformat, parseInt, parseTimestamp, pyRound, tokenize } from "./util.ts";

test("tokenize keeps first-seen order and drops duplicates, stopwords and short tokens", () => {
  assert.deepEqual(tokenize("The Waterproof waterproof Leather BACKPACK, for camping!"), ["waterproof", "leather", "backpack", "camping"]);
  assert.deepEqual(tokenize("a b and of x2 to"), ["x2"]);
  assert.deepEqual(tokenize(""), []);
});

test("formatMoney", () => {
  assert.equal(formatMoney(0), "0.00");
  assert.equal(formatMoney(5), "0.05");
  assert.equal(formatMoney(123456), "1234.56");
  assert.equal(formatMoney(-250), "-2.50");
});

test("parseInt follows Python int() and checks bounds", () => {
  assert.equal(parseInt("7", "n", 1, 10), 7);
  assert.equal(parseInt(" +3 ", "n", 1, 10), 3);
  assert.equal(parseInt(4.9, "n", 1, 10), 4);
  for (const bad of ["11", "x", null, "1__0"]) assert.throws(() => parseInt(bad, "n", 1, 10), HTTPError);
});

test("timestamps need an offset and keep microseconds", () => {
  assert.equal(parseTimestamp("2030-01-01T10:00:00+00:00", "t"), "2030-01-01T10:00:00.000000+00:00");
  assert.throws(() => parseTimestamp("2030-01-01T10:00:00", "t"), /timezone offset/);
  assert.equal(isoformat("2024-03-05 10:20:30.12+00"), "2024-03-05T10:20:30.120000+00:00");
  assert.equal(isoformat("2024-03-05 10:20:30+00"), "2024-03-05T10:20:30+00:00");
});

test("BM25: exact matches outrank typo matches, unrelated text scores 0", () => {
  const scores = scoreBatch(["leather", "backpack"], ["Rugged Leather Backpack 120", "Steel Kettle Glass 300", "Rugged Lether Bakpack 120"]);
  assert.ok(scores[0] > scores[2] && scores[2] > 0);
  assert.equal(scores[1], 0);
  assert.deepEqual(scoreBatch([], ["anything here"]), [0]);
});

test("pyRound", () => {
  assert.equal(pyRound(2.675, 2), 2.67);
  assert.equal(pyRound(0.0005, 3), 0.001);
});
