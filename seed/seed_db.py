#!/usr/bin/env python3
"""
Seed the relational schema for the semantic-cache testbed.

  products(id, name, category, brand, price_cents, stock, description, version, updated_at)
  semantic_cache(id, query, vec vector(256), entity_set, answer, prompt_tokens, completion_tokens, created_at)
  cache_deps(entity_key, cache_id)   -- dependency edges E ⊆ V_E x V_C (entity -> cache entry)

Products are generated deterministically from name templates over 40 categories so that entity names are realistic
enough for dictionary-based entity linking ("Nimbus Trail Runner 4", "Acme Ceramic Kettle 17", ...). The trigger
`products_change_notify` emits a NOTIFY on every UPDATE for the CDC-style invalidator (used by the graph-cdc mode only).
"""
import argparse, os, random
import psycopg2

CATEGORIES = ["running shoes", "hiking boots", "backpacks", "tents", "sleeping bags", "headlamps", "water bottles",
              "kettles", "coffee makers", "blenders", "toasters", "cookware", "knives", "cutting boards", "office chairs",
              "standing desks", "monitors", "keyboards", "mice", "headphones", "speakers", "smart watches", "fitness trackers",
              "yoga mats", "dumbbells", "bicycles", "bike helmets", "car seats", "strollers", "toys", "board games",
              "notebooks", "pens", "desk lamps", "air purifiers", "vacuum cleaners", "fans", "heaters", "luggage", "umbrellas"]
BRANDS = ["Acme", "Nimbus", "Orion", "Vertex", "Halcyon", "Kestrel", "Lumen", "Solace", "Tundra", "Zephyr", "Aster", "Boreal"]
ADJ = ["Trail", "Ultra", "Classic", "Pro", "Lite", "Compact", "Premium", "Eco", "Sport", "Urban", "Alpine", "Studio"]
NOUN = {c: c.rstrip("s").title() for c in CATEGORIES}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--products", type=int, default=5000)
    ap.add_argument("--dsn", default=os.environ.get("SC_DSN", "dbname=semcache user=acb password=acb host=localhost port=5432"))
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    conn = psycopg2.connect(a.dsn); conn.autocommit = True; cur = conn.cursor()
    cur.execute("""
        CREATE EXTENSION IF NOT EXISTS vector;
        DROP TABLE IF EXISTS cache_deps; DROP TABLE IF EXISTS semantic_cache; DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS categories;
        DROP TABLE IF EXISTS product_history; DROP TABLE IF EXISTS category_history;
        CREATE TABLE products (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, brand TEXT NOT NULL,
            price_cents INTEGER NOT NULL, stock INTEGER NOT NULL, description TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
        CREATE INDEX products_category_idx ON products(category);
        -- A category's freshness stamp cannot be MAX(version) over its member rows: that
        -- aggregate is blind to membership changes (a deleted row can lower the observed max; an
        -- inserted row with a version below the current max changes nothing observable). epoch is
        -- a monotonic counter bumped by every insert, update or delete that touches the category,
        -- so a category answer's cached epoch is stale iff it is less than the current one --
        -- membership changes are no longer invisible to the freshness check.
        CREATE TABLE categories (category TEXT PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE semantic_cache (
            id BIGSERIAL PRIMARY KEY, query TEXT NOT NULL, vec vector(256) NOT NULL, entity_set TEXT NOT NULL,
            answer JSONB NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now());
        CREATE INDEX semantic_cache_entity_idx ON semantic_cache(entity_set);
        CREATE INDEX semantic_cache_vec_idx ON semantic_cache USING hnsw (vec vector_cosine_ops);
        CREATE TABLE cache_deps (entity_key TEXT NOT NULL, cache_id BIGINT NOT NULL REFERENCES semantic_cache(id) ON DELETE CASCADE);
        CREATE INDEX cache_deps_entity_idx ON cache_deps(entity_key);
        CREATE INDEX cache_deps_cache_idx ON cache_deps(cache_id);
        CREATE OR REPLACE FUNCTION products_bump_category_epoch() RETURNS trigger AS $$
        DECLARE cat TEXT; new_epoch INTEGER;
        BEGIN
            cat := COALESCE(NEW.category, OLD.category);
            INSERT INTO categories(category, epoch) VALUES (cat, 1)
                ON CONFLICT (category) DO UPDATE SET epoch = categories.epoch + 1
                RETURNING epoch INTO new_epoch;
            IF TG_OP = 'DELETE' THEN
                PERFORM pg_notify('entity_change', json_build_object('entity', 'product:' || OLD.id, 'category', 'category:' || cat, 'version', new_epoch, 'op', TG_OP)::text);
                RETURN OLD;
            ELSE
                PERFORM pg_notify('entity_change', json_build_object('entity', 'product:' || NEW.id, 'category', 'category:' || cat, 'version', new_epoch, 'op', TG_OP)::text);
                -- membership can also change on UPDATE (a product re-categorised): bump the old
                -- category too, since rows leaving it change what a cached answer for it should say
                IF TG_OP = 'UPDATE' AND OLD.category <> NEW.category THEN
                    INSERT INTO categories(category, epoch) VALUES (OLD.category, 1)
                        ON CONFLICT (category) DO UPDATE SET epoch = categories.epoch + 1;
                    PERFORM pg_notify('entity_change', json_build_object('entity', 'product:' || NEW.id, 'category', 'category:' || OLD.category, 'version', 0, 'op', 'RECATEGORISED')::text);
                END IF;
                RETURN NEW;
            END IF;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER products_bump_category_epoch_iud AFTER INSERT OR UPDATE OR DELETE ON products
            FOR EACH ROW EXECUTE FUNCTION products_bump_category_epoch();

        -- Independent audit trail (Phase 2): every committed version/epoch change, with its commit
        -- timestamp, recorded by a trigger that has nothing to do with the gateway's own
        -- version-checking logic above. analysis/independent_checker.py reads only this table (and
        -- categories/products for the current commit that ANALYZE etc need) to establish ground
        -- truth for every served answer -- it never reads the gateway's self-reported
        -- stale/wrong_entity/truth_versions fields.
        CREATE TABLE product_history (
            id BIGSERIAL PRIMARY KEY, product_id INTEGER NOT NULL, version INTEGER NOT NULL,
            committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
        CREATE INDEX product_history_pid_idx ON product_history(product_id, committed_at);
        CREATE TABLE category_history (
            id BIGSERIAL PRIMARY KEY, category TEXT NOT NULL, epoch INTEGER NOT NULL,
            committed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());
        CREATE INDEX category_history_cat_idx ON category_history(category, committed_at);
        CREATE OR REPLACE FUNCTION audit_product_history() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                INSERT INTO product_history(product_id, version) VALUES (OLD.id, -1);  -- -1: deleted, never a valid version
                RETURN OLD;
            END IF;
            INSERT INTO product_history(product_id, version) VALUES (NEW.id, NEW.version);
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_product_history_iud AFTER INSERT OR UPDATE OR DELETE ON products
            FOR EACH ROW EXECUTE FUNCTION audit_product_history();
        CREATE OR REPLACE FUNCTION audit_category_history() RETURNS trigger AS $$
        BEGIN
            INSERT INTO category_history(category, epoch) VALUES (NEW.category, NEW.epoch);
            RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER audit_category_history_iu AFTER INSERT OR UPDATE ON categories
            FOR EACH ROW EXECUTE FUNCTION audit_category_history();
    """)
    rows = []
    for i in range(1, a.products + 1):
        cat = CATEGORIES[(i - 1) % len(CATEGORIES)]
        brand = rng.choice(BRANDS); adj = rng.choice(ADJ)
        name = f"{brand} {adj} {NOUN[cat]} {i}"
        price = int(rng.lognormvariate(8.0, 0.7))            # median ~ $29.80
        stock = rng.choice([0, 0, 3, 12, 25, 40, 75, 120, 250])
        desc = f"The {name} is a {adj.lower()} {cat.rstrip('s')} from {brand}, designed for everyday use with a balance of durability and value."
        rows.append((name, cat, brand, price, stock, desc))
    cur.executemany("INSERT INTO products(name, category, brand, price_cents, stock, description) VALUES (%s,%s,%s,%s,%s,%s)", rows)
    cur.execute("ANALYZE products;")
    # the per-row insert trigger bumped every category's epoch once per seeded product; reset to a
    # uniform baseline (1) so every experiment starts from the same category freshness stamp,
    # exactly as every product starts from version 1
    cur.execute("UPDATE categories SET epoch = 1")
    cur.execute("SELECT count(*), count(DISTINCT category) FROM products")
    print("seeded products:", cur.fetchone())


if __name__ == "__main__":
    main()
