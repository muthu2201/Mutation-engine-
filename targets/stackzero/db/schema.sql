-- StackZero "shop" schema.
--
-- A deliberately ordinary first-version schema: primary keys and foreign keys, but no
-- secondary indexes on foreign-key columns. PostgreSQL does not create those automatically,
-- and forgetting them is one of the most common performance problems in real applications.
-- Candidate indexes are exposed to Colloid as db.index knob genes instead of being baked in.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE categories (
    id        integer PRIMARY KEY,
    name      text    NOT NULL,
    parent_id integer REFERENCES categories (id)
);

CREATE TABLE customers (
    id         integer     PRIMARY KEY,
    name       text        NOT NULL,
    email      text        NOT NULL,
    country    char(2)     NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE products (
    id          integer     PRIMARY KEY,
    category_id integer     NOT NULL REFERENCES categories (id),
    name        text        NOT NULL,
    description text        NOT NULL,
    price_cents integer     NOT NULL CHECK (price_cents > 0),
    stock       integer     NOT NULL CHECK (stock >= 0),
    created_at  timestamptz NOT NULL
);

CREATE TABLE orders (
    id          bigserial   PRIMARY KEY,
    customer_id integer     NOT NULL REFERENCES customers (id),
    status      text        NOT NULL,
    placed_at   timestamptz NOT NULL,
    total_cents bigint      NOT NULL
);

CREATE TABLE order_items (
    order_id         bigint  NOT NULL REFERENCES orders (id),
    line_no          integer NOT NULL,
    product_id       integer NOT NULL REFERENCES products (id),
    quantity         integer NOT NULL CHECK (quantity > 0),
    unit_price_cents integer NOT NULL,
    PRIMARY KEY (order_id, line_no)
);

CREATE TABLE reviews (
    id          bigserial   PRIMARY KEY,
    product_id  integer     NOT NULL REFERENCES products (id),
    customer_id integer     NOT NULL REFERENCES customers (id),
    rating      smallint    NOT NULL CHECK (rating BETWEEN 1 AND 5),
    body        text        NOT NULL,
    created_at  timestamptz NOT NULL
);
