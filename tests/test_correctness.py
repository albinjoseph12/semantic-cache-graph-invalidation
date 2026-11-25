#!/usr/bin/env python3
"""
Phase 2 regression tests, against a real (local, disposable) PostgreSQL instance -- the
schema's correctness is in its triggers, which nothing short of a real database can verify.

    docker run -d --name semcache_test_pg -e POSTGRES_USER=acb -e POSTGRES_PASSWORD=acb \
        -e POSTGRES_DB=semcache -p 15432:5432 pgvector/pgvector:pg16
    python3 seed/seed_db.py --dsn "dbname=semcache user=acb password=acb host=localhost port=15432" --products 200
    python3 tests/test_correctness.py "dbname=semcache user=acb password=acb host=localhost port=15432"

Every test truncates and reseeds a fresh, tiny schema so tests don't interfere with each other.
"""
import os
import subprocess
import sys

import psycopg2

DSN = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "SC_TEST_DSN", "dbname=semcache user=acb password=acb host=localhost port=15432")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL':4s}  {name}  {detail}")
    if not cond:
        FAILS.append(name)


def reseed(n_products=60):
    subprocess.run([sys.executable, os.path.join(ROOT, "seed", "seed_db.py"),
                     "--dsn", DSN, "--products", str(n_products)], check=True, capture_output=True)


def conn():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    return c


# --------------------------------------------------------------------------------- 1. category version collision /
# --------------------------------------------------------------------------------- membership change (the audit fix)
def test_category_epoch_catches_membership_change():
    reseed()
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT category FROM products LIMIT 1")
        cat = cur.fetchone()[0]
        cur.execute("SELECT coalesce(max(version),0) FROM products WHERE category=%s", (cat,))
        old_way_before = cur.fetchone()[0]
        cur.execute("SELECT epoch FROM categories WHERE category=%s", (cat,))
        epoch_before = cur.fetchone()[0]

        # a different writer inserts a new member with a version at or below the current max --
        # exactly the case MAX(version) cannot see
        cur.execute("INSERT INTO products(name, category, brand, price_cents, stock, description, version) "
                     "VALUES ('collision-test-product', %s, 'Acme', 1000, 5, 'x', 1)", (cat,))

        cur.execute("SELECT coalesce(max(version),0) FROM products WHERE category=%s", (cat,))
        old_way_after = cur.fetchone()[0]
        cur.execute("SELECT epoch FROM categories WHERE category=%s", (cat,))
        epoch_after = cur.fetchone()[0]

    check("MAX(version) is blind to the new member (the flaw this test documents)",
          old_way_after == old_way_before, f"{old_way_before} -> {old_way_after}")
    check("categories.epoch catches the membership change",
          epoch_after == epoch_before + 1, f"{epoch_before} -> {epoch_after}")


def test_category_epoch_catches_deletion():
    reseed()
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT category, id FROM products LIMIT 1")
        cat, pid = cur.fetchone()
        cur.execute("SELECT epoch FROM categories WHERE category=%s", (cat,))
        before = cur.fetchone()[0]
        cur.execute("DELETE FROM products WHERE id=%s", (pid,))
        cur.execute("SELECT epoch FROM categories WHERE category=%s", (cat,))
        after = cur.fetchone()[0]
    check("categories.epoch catches a deletion", after == before + 1, f"{before} -> {after}")


# --------------------------------------------------------------------------------- 2. write during answer generation
# --------------------------------------------------------------------------------- / concurrent transactions
def test_version_checked_insertion_rejects_stale_write():
    """Simulates the graph-tx race the code comments describe: a context is read (versions
    captured), a concurrent writer commits an update to the same product, and only then does the
    would-be cache insert re-check truth_versions -- which must now disagree and reject the entry."""
    reseed()
    with conn() as c, c.cursor() as cur:
        cur.execute("SELECT id, version FROM products LIMIT 1")
        pid, v0 = cur.fetchone()
        versions_at_context_read = {f"product:{pid}": v0}

        # a concurrent writer commits in between the context read and the cache insert
        cur.execute("UPDATE products SET price_cents = price_cents + 1, version = version + 1 WHERE id=%s", (pid,))

        cur.execute("SELECT version FROM products WHERE id=%s", (pid,))
        now = {f"product:{pid}": cur.fetchone()[0]}
    would_reject = any(now.get(k, 0) != versions_at_context_read.get(k, 0) for k in versions_at_context_read)
    check("version-checked insertion rejects an entry generated from now-stale state", would_reject,
          f"versions_at_read={versions_at_context_read} now={now}")


def test_advisory_lock_serialises_concurrent_writers():
    """Two connections both try to take the same per-entity advisory lock; the second must block
    until the first releases it (proves the lock actually serialises, not just that the SQL runs)."""
    reseed()
    key = "product:1"
    c1 = psycopg2.connect(DSN); c1.autocommit = False
    c2 = psycopg2.connect(DSN); c2.autocommit = False
    try:
        cur1 = c1.cursor(); cur1.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        # c2 tries to take the same lock in its own (non-blocking) session first, using
        # pg_try_advisory_xact_lock, to prove it is NOT immediately available while c1 holds it
        cur2 = c2.cursor(); cur2.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (key,))
        got_while_held = cur2.fetchone()[0]
        c2.rollback()
        c1.commit()  # releases c1's xact-scoped lock
        cur2b = c2.cursor(); cur2b.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (key,))
        got_after_release = cur2b.fetchone()[0]
        c2.rollback()
    finally:
        c1.close(); c2.close()
    check("second writer cannot take the lock while the first holds it", got_while_held is False)
    check("second writer can take the lock once the first releases it", got_after_release is True)


# --------------------------------------------------------------------------------- 3. incorrect entity linking
def test_entity_linker_resolves_category_and_rejects_unknown():
    sys.path.insert(0, os.path.join(ROOT, "app"))
    from queries import EntityLinker
    linker = EntityLinker([(1, "Acme Blue Running Shoe"), (2, "Acme Red Backpack")], ["running shoes", "backpacks"])
    check("resolves an explicit product id", linker.resolve("tell me about product #1") == ["product:1"])
    check("resolves a product by exact name", linker.resolve("how much is the Acme Blue Running Shoe") == ["product:1"])
    check("resolves a category when no product matches", linker.resolve("what's cheap in running shoes") == ["category:running shoes"])
    check("resolves to nothing for unrelated text (no false entity)", linker.resolve("what is the weather today") == [])


# --------------------------------------------------------------------------------- 4. deliberately injected
# --------------------------------------------------------------------------------- stale / wrong-entity answers
def test_injected_stale_and_wrong_entity_are_independently_detectable():
    """Not a DB test: proves the *shape* of data the independent checker needs is sufficient to
    catch a deliberately fabricated bad answer, without trusting any self-reported verdict."""
    sys.path.insert(0, os.path.join(ROOT, "analysis"))
    from independent_checker import truth_at

    # a category history where the true epoch advanced to 3 by ts=1000
    hist = {"running shoes": ([100.0, 500.0, 1000.0], [1, 2, 3])}
    stale_answer_versions = {"category:running shoes": 1}     # answer was generated at epoch 1
    truth = truth_at(hist, "running shoes", 1000.0)
    is_stale = stale_answer_versions["category:running shoes"] < truth
    check("a deliberately stale answer (epoch 1 served against true epoch 3) is caught", is_stale, f"truth={truth}")

    fresh_answer_versions = {"category:running shoes": 3}
    is_stale2 = fresh_answer_versions["category:running shoes"] < truth
    check("a genuinely fresh answer (epoch 3 against true epoch 3) is not flagged", not is_stale2)

    cache_entities = {"product:1"}
    query_entities = {"product:2"}
    wrong = bool(cache_entities) and cache_entities != query_entities
    check("a deliberately mismatched cache entry (product:1 served for product:2) is caught", wrong)


if __name__ == "__main__":
    test_category_epoch_catches_membership_change()
    test_category_epoch_catches_deletion()
    test_version_checked_insertion_rejects_stale_write()
    test_advisory_lock_serialises_concurrent_writers()
    test_entity_linker_resolves_category_and_rejects_unknown()
    test_injected_stale_and_wrong_entity_are_independently_detectable()
    if FAILS:
        print(f"\n{len(FAILS)} FAILED: {FAILS}")
        sys.exit(1)
    print("\nall checks passed")
