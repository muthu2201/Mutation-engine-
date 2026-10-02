-- Deterministic synthetic data for StackZero (no production data is ever used: blueprint D4).
--
-- setseed() makes random() reproducible within this single session, and INSERT ... SELECT
-- never runs in parallel, so every build of the template database is the same logical data
-- set. (Random indexes are computed in a derived table and then joined: a volatile random()
-- inside a scalar subquery's WHERE clause would be re-evaluated per scanned row.) Totals: 60 categories, 20k customers, 20k products, 120k orders, ~360k order
-- lines and 60k reviews.
--
-- Partitioning for write-independent benchmarks: customers 19001..20000 and products in
-- categories 57..60 are reserved for the *write* workload (POST /orders). Read requests in
-- the benchmark workloads never touch them, so the response to every read request is
-- independent of how writes interleave with it - which lets the evaluator spot-check
-- responses produced under load against reference responses exactly.

SELECT setseed(0.4242);

CREATE TEMP TABLE vocab_adj (i int PRIMARY KEY, w text);
INSERT INTO vocab_adj SELECT row_number() OVER (), w FROM unnest(ARRAY[
 'classic','compact','deluxe','eco','elegant','ergonomic','essential','foldable','heavy','lightweight',
 'modern','portable','premium','rugged','sleek','smart','soft','sturdy','vintage','wireless',
 'adjustable','durable','handmade','insulated','magnetic','modular','organic','padded','quiet','rapid',
 'reusable','slim','stackable','thermal','tough','ultra','versatile','waterproof','woven','zesty']) AS w;

CREATE TEMP TABLE vocab_mat (i int PRIMARY KEY, w text);
INSERT INTO vocab_mat SELECT row_number() OVER (), w FROM unnest(ARRAY[
 'bamboo','brass','canvas','carbon','ceramic','cotton','copper','denim','glass','granite',
 'hemp','leather','linen','maple','marble','mesh','nylon','oak','paper','plastic',
 'rubber','silicone','silk','steel','stone','suede','titanium','walnut','wool','aluminum']) AS w;

CREATE TEMP TABLE vocab_noun (i int PRIMARY KEY, w text);
INSERT INTO vocab_noun SELECT row_number() OVER (), w FROM unnest(ARRAY[
 'backpack','basket','blanket','bottle','bowl','bracket','brush','cable','camera','candle',
 'chair','charger','clock','cushion','desk','drone','earbuds','fan','flask','glove',
 'hammer','headphones','helmet','jacket','kettle','keyboard','knife','lamp','lantern','mat',
 'mirror','monitor','mouse','mug','notebook','organizer','pan','pen','pillow','planter',
 'rack','router','scarf','scooter','shelf','shoes','speaker','spoon','stand','stool',
 'table','tent','thermos','tripod','tumbler','umbrella','vase','wallet','watch','wrench']) AS w;

CREATE TEMP TABLE vocab_desc (i int PRIMARY KEY, w text);
INSERT INTO vocab_desc SELECT row_number() OVER (), w FROM unnest(ARRAY[
 'perfect','for','daily','use','with','built','from','high','quality','materials','designed','to',
 'last','easy','clean','and','store','ideal','travel','home','office','outdoor','camping','gift',
 'includes','warranty','comfortable','grip','fits','most','bags','strong','yet','light','weather',
 'resistant','finish','looks','great','any','room','fast','charging','long','battery','life',
 'noise','cancelling','crisp','sound','sharp','blade','non','stick','coating','even','heat',
 'stylish','minimalist','design','space','saving','folds','flat','secure','lock','soft','touch',
 'premium','stitching','breathable','fabric','all','season','eco','friendly','recycled','packaging',
 'handcrafted','by','artisans','smooth','rolling','wheels','extra','storage','pockets','reflective',
 'details','adjustable','straps','dishwasher','safe','bpa','free','spill','proof','lid','double']) AS w;

-- Categories: 10 roots, 50 leaves.
INSERT INTO categories (id, name, parent_id)
SELECT g, 'Department ' || g, NULL FROM generate_series(1, 10) g;
INSERT INTO categories (id, name, parent_id)
SELECT g, initcap((SELECT w FROM vocab_noun WHERE i = 1 + (g * 7) % 60)) || ' & ' ||
          initcap((SELECT w FROM vocab_mat WHERE i = 1 + (g * 11) % 30)) || ' ' || g,
       1 + (g - 11) / 5
FROM generate_series(11, 60) g;

INSERT INTO customers (id, name, email, country, created_at)
SELECT g,
       (ARRAY['Ava','Ben','Chloe','Dev','Ema','Finn','Gita','Hugo','Ines','Jon','Kira','Leo','Mia','Nia','Omar','Pia','Quinn','Ravi','Sara','Tom'])[1 + floor(random() * 20)::int]
         || ' ' ||
       (ARRAY['Silva','Kim','Novak','Patel','Garcia','Muller','Rossi','Tanaka','Okafor','Larsen','Nguyen','Cohen','Dubois','Ivanov','Singh','Smith'])[1 + floor(random() * 16)::int],
       'customer' || g || '@example.com',
       (ARRAY['US','DE','IN','BR','JP','FR','GB','NG','CA','AU'])[1 + floor(random() * 10)::int],
       timestamptz '2022-01-01 00:00:00+00' + (random() * 1000) * interval '1 day'
FROM generate_series(1, 20000) g;

INSERT INTO products (id, category_id, name, description, price_cents, stock, created_at)
SELECT g,
       11 + floor(random() * 50)::int,
       initcap((SELECT w FROM vocab_adj WHERE i = 1 + floor(r1 * 40)::int)) || ' ' ||
       initcap((SELECT w FROM vocab_mat WHERE i = 1 + floor(r2 * 30)::int)) || ' ' ||
       initcap((SELECT w FROM vocab_noun WHERE i = 1 + floor(r3 * 60)::int)) || ' ' || (100 + g % 900),
       (SELECT string_agg(v.w, ' ' ORDER BY d.k)
          FROM (SELECT k, 1 + floor(random() * 100)::int + g * 0 AS idx FROM generate_series(1, 12 + (g % 13)) k) d
          JOIN vocab_desc v ON v.i = d.idx),
       199 + floor(random() * 49800)::int,
       1000 + floor(random() * 4000)::int,
       timestamptz '2023-01-01 00:00:00+00' + (random() * 700) * interval '1 day'
FROM (SELECT g, random() AS r1, random() AS r2, random() AS r3 FROM generate_series(1, 20000) g) s;

-- Orders: customers 1..19000 with a skewed (quadratic) popularity distribution.
INSERT INTO orders (customer_id, status, placed_at, total_cents)
SELECT 1 + floor(19000 * power(random(), 2))::int,
       (ARRAY['placed','shipped','delivered','delivered','delivered','returned'])[1 + floor(random() * 6)::int],
       timestamptz '2024-01-01 00:00:00+00' + (random() * 546) * interval '1 day',
       0
FROM generate_series(1, 120000);

INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price_cents)
SELECT o.id, l.line_no, p.id, 1 + floor(random() * 3)::int, p.price_cents
FROM orders o
CROSS JOIN LATERAL generate_series(1, 1 + (floor(random() * 5)::int + (o.id * 0)::int)) AS l(line_no)
CROSS JOIN LATERAL (SELECT 1 + floor(20000 * power(random(), 1.5))::int + (o.id * 0 + l.line_no * 0)::int AS pid) pick
JOIN products p ON p.id = pick.pid;

UPDATE orders o SET total_cents = t.total
FROM (SELECT order_id, sum(quantity * unit_price_cents) AS total FROM order_items GROUP BY order_id) t
WHERE t.order_id = o.id;

INSERT INTO reviews (product_id, customer_id, rating, body, created_at)
SELECT 1 + floor(20000 * power(random(), 1.3))::int,
       1 + floor(random() * 19000)::int,
       (ARRAY[1,2,3,3,4,4,4,5,5,5])[1 + floor(random() * 10)::int],
       (SELECT string_agg(v.w, ' ' ORDER BY d.k)
          FROM (SELECT k, 1 + floor(random() * 100)::int + g * 0 AS idx FROM generate_series(1, 6 + (g % 9)) k) d
          JOIN vocab_desc v ON v.i = d.idx),
       timestamptz '2023-06-01 00:00:00+00' + (random() * 760) * interval '1 day'
FROM generate_series(1, 60000) g;

-- (VACUUM ANALYZE is run by the loader after this script: it cannot run inside the script's transaction.)
