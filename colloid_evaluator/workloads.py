"""Request generators for StackZero: training workload, hidden holdout workload, and the
correctness-oracle sequence. Owned by the evaluator; the search side never sees them.

Design rules that make the evaluator hard to game:

* **Fresh seeds per evaluation.** Every comparison draws new request parameters, so a
  candidate cannot memorise answers to a fixed corpus (the "caching results keyed by input"
  hack of CUDA-L1).
* **Write-independent reads under load.** Writes in the load workloads use reserved
  customers (19001-20000) and reserved categories (57-60); reads never touch them. A read's
  correct answer therefore does not depend on how it interleaves with writes, which makes
  exact spot checks of responses produced *under load* possible.
* **Write-then-read sequences in the oracle.** The correctness oracle places an order and
  then reads every view the order should change (summary, recommendations, category top
  list, daily report), and reads some views *before and after* the write. A candidate that
  caches across requests, skips writes, or defers them to a background task fails here.
* **Holdout ≠ train.** The holdout workload uses different endpoint weights and different
  parameter distributions (uniform instead of skewed ids, longer queries with more typos,
  longer report windows). Promotion requires gains to persist on it (blueprint D2.6).
"""

from __future__ import annotations

import datetime
import json
import random
from dataclasses import dataclass, field
from typing import Any

import psycopg

ADJ = ["classic", "compact", "deluxe", "eco", "elegant", "ergonomic", "essential", "foldable", "heavy", "lightweight", "modern",
       "portable", "premium", "rugged", "sleek", "smart", "soft", "sturdy", "vintage", "wireless", "adjustable", "durable",
       "handmade", "insulated", "magnetic", "modular", "organic", "padded", "quiet", "rapid", "reusable", "slim", "stackable",
       "thermal", "tough", "ultra", "versatile", "waterproof", "woven", "zesty"]
MAT = ["bamboo", "brass", "canvas", "carbon", "ceramic", "cotton", "copper", "denim", "glass", "granite", "hemp", "leather",
       "linen", "maple", "marble", "mesh", "nylon", "oak", "paper", "plastic", "rubber", "silicone", "silk", "steel", "stone",
       "suede", "titanium", "walnut", "wool", "aluminum"]
NOUN = ["backpack", "basket", "blanket", "bottle", "bowl", "bracket", "brush", "cable", "camera", "candle", "chair", "charger",
        "clock", "cushion", "desk", "drone", "earbuds", "fan", "flask", "glove", "hammer", "headphones", "helmet", "jacket",
        "kettle", "keyboard", "knife", "lamp", "lantern", "mat", "mirror", "monitor", "mouse", "mug", "notebook", "organizer",
        "pan", "pen", "pillow", "planter", "rack", "router", "scarf", "scooter", "shelf", "shoes", "speaker", "spoon", "stand",
        "stool", "table", "tent", "thermos", "tripod", "tumbler", "umbrella", "vase", "wallet", "watch", "wrench"]
WRITE_CUSTOMERS = (19001, 20000)
WRITE_CATEGORIES = (57, 60)
READ_CATEGORIES = (11, 56)
REPORT_DATES = (datetime.date(2024, 1, 8), datetime.date(2025, 6, 29))

TRAIN_MIX = {"search": 0.20, "product": 0.30, "summary": 0.20, "reco": 0.05, "category_top": 0.05, "daily": 0.05, "order": 0.15}
HOLDOUT_MIX = {"search": 0.30, "product": 0.15, "summary": 0.25, "reco": 0.10, "category_top": 0.05, "daily": 0.05, "order": 0.10}


@dataclass
class Universe:
    """ID sets the generators draw from, read once from the template database."""

    customers_with_orders: list[int]
    reco_customers: list[int]
    write_products: list[int]
    product_count: int

    @staticmethod
    def load(conn: psycopg.Connection[Any]) -> Universe:
        with_orders = [r[0] for r in conn.execute(
            "SELECT DISTINCT customer_id FROM orders WHERE customer_id <= 19000 ORDER BY 1").fetchall()]
        reco = [r[0] for r in conn.execute(
            "SELECT o.customer_id FROM orders o JOIN order_items oi ON oi.order_id = o.id JOIN products p ON p.id = oi.product_id "
            "WHERE o.customer_id <= 19000 GROUP BY o.customer_id HAVING max(p.category_id) < %s ORDER BY 1",
            (WRITE_CATEGORIES[0],),
        ).fetchall()]
        writes = [r[0] for r in conn.execute(
            "SELECT id FROM products WHERE category_id BETWEEN %s AND %s ORDER BY id", WRITE_CATEGORIES).fetchall()]
        count = int(conn.execute("SELECT count(*) FROM products").fetchone()[0])  # type: ignore[index]
        return Universe(with_orders, reco, writes, count)


@dataclass
class Request:
    method: str
    path: str
    body: str = ""
    kind: str = ""
    write: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_wire(self, t_us: int, sample: bool) -> str:
        return json.dumps({"t": t_us, "m": self.method, "p": self.path, "b": self.body, "s": 1 if sample else 0}, separators=(",", ":"))


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.choice(["drop", "swap", "dup", "sub"])
    if op == "drop":
        return word[:i] + word[i + 1 :]
    if op == "swap":
        return word[: i - 1] + word[i] + word[i - 1] + word[i + 1 :]
    if op == "dup":
        return word[:i] + word[i] + word[i:]
    return word[:i] + rng.choice("abcdefghijklmnopqrstuvwxyz") + word[i + 1 :]


class Generator:
    def __init__(self, universe: Universe, rng: random.Random, *, holdout: bool = False) -> None:
        self.u = universe
        self.rng = rng
        self.holdout = holdout
        self.mix = HOLDOUT_MIX if holdout else TRAIN_MIX
        self._order_seq = 0

    # -- parameter distributions -------------------------------------------------------
    def _customer(self) -> int:
        pool = self.u.customers_with_orders
        if self.holdout:
            return self.rng.choice(pool)
        return pool[min(len(pool) - 1, int(len(pool) * self.rng.random() ** 1.6))]

    def _product(self) -> int:
        n = self.u.product_count
        if self.holdout:
            return self.rng.randint(1, n)
        return 1 + min(n - 1, int(n * self.rng.random() ** 1.4))

    def _query(self) -> str:
        k = self.rng.choice([2, 3, 3, 4]) if self.holdout else self.rng.choice([1, 2, 2, 3])
        words = []
        for _ in range(k):
            pool = self.rng.choice([ADJ, MAT, NOUN, NOUN])
            w = self.rng.choice(pool)
            if self.rng.random() < (0.35 if self.holdout else 0.2):
                w = _typo(w, self.rng)
            words.append(w)
        if self.rng.random() < 0.3:
            words.insert(self.rng.randrange(len(words) + 1), self.rng.choice(["for", "with", "the"]))
        return " ".join(words)

    def _date(self) -> datetime.date:
        span = (REPORT_DATES[1] - REPORT_DATES[0]).days
        return REPORT_DATES[0] + datetime.timedelta(days=self.rng.randrange(span))

    # -- requests --------------------------------------------------------------------
    def make(self, kind: str) -> Request:
        r = self.rng
        if kind == "search":
            q = self._query().replace(" ", "+")
            limit = r.choice([10, 10, 20]) if not self.holdout else r.choice([10, 25])
            return Request("GET", f"/products/search?q={q}&limit={limit}", kind=kind)
        if kind == "product":
            return Request("GET", f"/products/{self._product()}", kind=kind)
        if kind == "summary":
            return Request("GET", f"/customers/{self._customer()}/summary", kind=kind)
        if kind == "reco":
            return Request("GET", f"/customers/{r.choice(self.u.reco_customers)}/recommendations", kind=kind)
        if kind == "category_top":
            return Request("GET", f"/categories/{r.randint(*READ_CATEGORIES)}/top?limit={r.choice([5, 10, 20])}", kind=kind)
        if kind == "daily":
            days = r.randint(1, 7) if not self.holdout else r.randint(14, 30)
            return Request("GET", f"/reports/daily?as_of={self._date().isoformat()}&days={days}", kind=kind)
        if kind == "order":
            return self.order(r.randint(*WRITE_CUSTOMERS))
        raise ValueError(kind)

    def order(self, customer: int, products: list[int] | None = None, when: datetime.datetime | None = None) -> Request:
        r = self.rng
        self._order_seq += 1
        if products is None:
            products = r.sample(self.u.write_products, r.randint(1, 4))
        items = [{"product_id": p, "quantity": r.randint(1, 3)} for p in products]
        when = when or datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(minutes=r.randrange(500_000))
        body = json.dumps({"customer_id": customer, "placed_at": when.isoformat(), "items": items})
        return Request("POST", "/orders", body=body, kind="order", write=True, meta={"customer": customer, "products": products, "when": when})

    def mixed(self, n: int) -> list[Request]:
        kinds = list(self.mix)
        weights = [self.mix[k] for k in kinds]
        return [self.make(self.rng.choices(kinds, weights)[0]) for _ in range(n)]

    def restricted(self, n: int, kinds: list[str]) -> list[Request]:
        """Requests drawn only from ``kinds`` (renormalised weights) - used by L4 to exercise
        just the paths a gene touches."""
        weights = [self.mix[k] for k in kinds]
        return [self.make(self.rng.choices(kinds, weights)[0]) for _ in range(n)]


def poisson_schedule(n: int, rate: float, rng: random.Random, start_us: int = 0) -> list[int]:
    """Open-loop arrival times (µs) for ``n`` requests at ``rate`` per second."""
    t = float(start_us)
    out = []
    for _ in range(n):
        t += rng.expovariate(rate) * 1e6
        out.append(int(t))
    return out


def oracle_sequence(universe: Universe, rng: random.Random, *, size: str = "quick") -> list[Request]:
    """The correctness-oracle request sequence: edge cases, every endpoint, and
    write-then-read chains. ``size`` = quick (L2) or deep (L6)."""
    g = Generator(universe, rng)
    seq: list[Request] = []
    reps = 3 if size == "quick" else 12
    for _ in range(reps):
        for kind in ("search", "product", "summary", "reco", "category_top", "daily"):
            seq.append(g.make(kind))
    # Edge cases and validation errors (status codes and error bodies must match too).
    seq += [
        Request("GET", "/products/0", kind="edge"),
        Request("GET", f"/products/{universe.product_count + 1}", kind="edge"),
        Request("GET", "/products/abc", kind="edge"),
        Request("GET", "/customers/999999/summary", kind="edge"),
        Request("GET", "/customers/999999/recommendations", kind="edge"),
        Request("GET", "/categories/3/top", kind="edge"),
        Request("GET", "/categories/999/top", kind="edge"),
        Request("GET", "/categories/20/top?limit=0", kind="edge"),
        Request("GET", "/reports/daily?as_of=not-a-date", kind="edge"),
        Request("GET", "/reports/daily?as_of=2025-03-01&days=0", kind="edge"),
        Request("GET", "/products/search?q=", kind="edge"),
        Request("GET", "/products/search?q=the+and+of", kind="edge"),
        Request("GET", "/products/search?q=qqqzzzxxx", kind="edge"),
        Request("GET", "/products/search?q=lamp&limit=51", kind="edge"),
        Request("POST", "/orders", body="{not json", kind="edge"),
        Request("POST", "/orders", body=json.dumps({"customer_id": 19001, "placed_at": "2030-01-01T00:00:00+00:00"}), kind="edge"),
        Request("POST", "/orders", body=json.dumps({"customer_id": 19001, "placed_at": "2030-01-01T00:00:00", "items": [{"product_id": 1, "quantity": 1}]}), kind="edge"),
        Request("POST", "/orders", body=json.dumps({"customer_id": 999999, "placed_at": "2030-01-01T00:00:00+00:00", "items": [{"product_id": 1, "quantity": 1}]}), kind="edge"),
        Request("POST", "/orders", body=json.dumps({"customer_id": 19001, "placed_at": "2030-01-01T00:00:00+00:00", "items": [{"product_id": 9999999, "quantity": 1}]}), kind="edge"),
        Request("POST", "/orders", body=json.dumps({"customer_id": 19001, "placed_at": "2030-01-01T00:00:00+00:00", "items": [{"product_id": 1, "quantity": 0}]}), kind="edge"),
        Request("POST", "/orders", body=json.dumps({"customer_id": 19001, "placed_at": "2030-01-01T00:00:00+00:00", "items": [{"product_id": 5, "quantity": 1000}]}), kind="edge"),
    ]
    rng.shuffle(seq)
    # Write-then-read chains, each read once before and once after the write.
    chains = 3 if size == "quick" else 10
    for _ in range(chains):
        customer = rng.randint(*WRITE_CUSTOMERS)
        products = rng.sample(universe.write_products, rng.randint(1, 4))
        when = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(days=rng.randrange(300), minutes=rng.randrange(1440))
        reads = [
            Request("GET", f"/customers/{customer}/summary", kind="chain"),
            Request("GET", f"/customers/{customer}/recommendations", kind="chain"),
            Request("GET", f"/reports/daily?as_of={when.date().isoformat()}&days=1", kind="chain"),
        ]
        seq += reads
        seq.append(g.order(customer, products, when))
        seq += [Request(r.method, r.path, kind="chain") for r in reads]
        seq.append(Request("GET", f"/products/{products[0]}", kind="chain"))
        seq.append(g.order(customer, products[:1], when + datetime.timedelta(hours=1)))
        seq += [Request(r.method, r.path, kind="chain") for r in reads[:1]]
    # Repeat some earlier reads late in the sequence (stale-cache detection after writes).
    for r in rng.sample(seq[: len(seq) // 2], min(10, len(seq) // 2)):
        if r.method == "GET":
            seq.append(Request(r.method, r.path, kind="repeat"))
    return seq
