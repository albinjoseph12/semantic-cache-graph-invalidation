"""Content-level audit of resident category cache entries (post-hoc local re-run, 2026-11-18).

Every poll opens ONE repeatable-read, read-only snapshot, lists the resident category-only entries, re-reads each category's
rows with the gateway's own SQL, rebuilds the answer with the gateway's own compose_answer(), and compares the PRICES in the
stored text with the prices in the rebuilt text. In graph-tx an entry is deleted inside the transaction of every write that
touches its category, so a resident entry whose prices differ from the same snapshot's rows is genuinely stale at that instant.
An entry is "stamp_current" if its stored category epoch equals the snapshot's epoch: those are exactly the stale entries that a
version-stamp comparison cannot see. Prices are compared (not names/stock) so that equal-price ties cannot cause false alarms.
"""
import ast, asyncio, json, re, time
from pathlib import Path

SQL_ROWS = "SELECT id, name, price_cents, stock, version FROM products WHERE category = $1 ORDER BY price_cents ASC LIMIT 8"
PRICE = re.compile(r"\$(\d+\.\d\d)")


def load_compose(main_py):
    tree = ast.parse(Path(main_py).read_text())
    sel = [n for n in tree.body if getattr(n, "name", None) == "compose_answer"]
    scope = {}
    exec(compile(ast.Module(sel, []), str(main_py), "exec"), scope)
    return scope["compose_answer"]


class Auditor:
    def __init__(self, conn, compose):
        self.conn, self.compose = conn, compose
        self.seen = {}      # entry id -> dict(first_ts, category, flagged, stamp_current, stored, expected)
        self.polls = 0
        self.last_id = 0    # only entries newer than this are inspected, each within ~one poll interval of insertion

    async def poll(self):
        async with self.conn.transaction(isolation="repeatable_read", readonly=True):
            entries = await self.conn.fetch("SELECT id, query, entity_set, answer FROM semantic_cache "
                                            "WHERE id > $1 AND entity_set LIKE 'category:%' AND position('|' in entity_set) = 0 ORDER BY id", self.last_id)
            self.max_id = await self.conn.fetchval("SELECT coalesce(max(id),0) FROM semantic_cache")
            cats = {e["entity_set"].split(":", 1)[1] for e in entries}
            rows, epochs = {}, {}
            for c in cats:
                rows[c] = [dict(r) for r in await self.conn.fetch(SQL_ROWS, c)]
                epochs[c] = await self.conn.fetchval("SELECT coalesce(epoch,0) FROM categories WHERE category=$1", c)
        self.polls += 1
        now = time.time()
        if entries:
            self.last_id = max(self.last_id, max(e["id"] for e in entries))
        for e in entries:
            key, cat = e["entity_set"], e["entity_set"].split(":", 1)[1]
            ans = json.loads(e["answer"]) if isinstance(e["answer"], str) else e["answer"]
            stored = PRICE.findall(ans["text"])
            expected = PRICE.findall(self.compose(e["query"], {key: rows[cat]}))
            flagged = stored != expected
            cur = self.seen.get(e["id"])
            if cur is None:
                self.seen[e["id"]] = dict(first_ts=now, category=cat, flagged=flagged,
                                          stamp_current=(ans["versions"].get(key) == epochs[cat]),
                                          stored=stored if flagged else None, expected=expected if flagged else None)
            elif flagged and not cur["flagged"]:
                cur.update(flagged=True, stamp_current=(ans["versions"].get(key) == epochs[cat]), stored=stored, expected=expected)

    def summary(self):
        fl = [v for v in self.seen.values() if v["flagged"]]
        return dict(polls=self.polls, distinct_category_entries_audited=len(self.seen),
                    note="each new category-only entry inspected shortly after insertion; entries deleted by a write before the next poll are not observed (undercount, same for both variants)",
                    content_stale_entries=len(fl),
                    content_stale_with_current_stamp=sum(1 for v in fl if v["stamp_current"]),
                    content_stale_with_visible_stale_stamp=sum(1 for v in fl if not v["stamp_current"]),
                    examples=[dict(category=v["category"], stored=v["stored"], expected=v["expected"], stamp_current=v["stamp_current"]) for v in fl[:5]])


async def run_audit(dsn, main_py, stop_event, interval=0.05):
    import asyncpg
    conn = await asyncpg.connect(dsn)
    a = Auditor(conn, load_compose(main_py))
    try:
        while not stop_event.is_set():
            await a.poll()
            try:
                await asyncio.wait_for(stop_event.wait(), interval)
            except asyncio.TimeoutError:
                pass
        await a.poll()          # final poll after load has stopped
    finally:
        await conn.close()
    return a.summary()
