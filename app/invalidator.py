#!/usr/bin/env python3
"""
CDC-style asynchronous graph invalidator (graph-cdc mode).

Listens on the PostgreSQL NOTIFY channel `entity_change` (emitted by the AFTER UPDATE trigger on products - the same
role a logical-replication / Debezium consumer plays in production) and, for each change, invalidates every cache entry
that has a dependency edge from the changed product or its category: deletes the pgvector rows (cache_deps cascade)
and the Redis answer payloads. Because this runs after the writer's commit, there is a window between commit and
invalidation during which a stale hit can be served; the gateway measures that window as stale reads.
"""
import asyncio, json, os, time
import asyncpg
import redis.asyncio as aioredis

DSN = os.environ.get("SC_PG_URL", "postgresql://acb:acb@localhost:5432/semcache")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
LOG = os.environ.get("INVALIDATOR_LOG", "invalidator.csv")
CDC_LAG_MS = float(os.environ.get("CDC_LAG_MS", "0"))   # emulated pipeline delay (e.g. Debezium -> Kafka -> consumer)


async def main():
    conn = await asyncpg.connect(DSN)
    work = await asyncpg.connect(DSN)
    r = aioredis.from_url(REDIS_URL)
    queue = asyncio.Queue()
    log = open(LOG, "w"); log.write("ts_ms,entity,version,invalidated,latency_ms\n")

    def on_notify(c, pid, channel, payload):
        queue.put_nowait((time.perf_counter(), json.loads(payload)))

    await conn.add_listener("entity_change", on_notify)
    print("[invalidator] listening on entity_change", flush=True)
    while True:
        t0, ev = await queue.get()
        if CDC_LAG_MS > 0:
            await asyncio.sleep(max(0.0, CDC_LAG_MS / 1000.0 - (time.perf_counter() - t0)))
        keys = [ev["entity"], ev["category"]]
        ids = [x["cache_id"] for x in await work.fetch("SELECT DISTINCT cache_id FROM cache_deps WHERE entity_key = ANY($1::text[])", keys)]
        n = 0
        if ids:
            await work.execute("DELETE FROM semantic_cache WHERE id = ANY($1::bigint[])", ids)
            pipe = r.pipeline()
            for i in ids:
                pipe.delete(f"answer:{i}")
            for k in keys:
                pipe.delete(f"entity:{k}:caches")
            await pipe.execute()
            n = len(ids)
        log.write(f"{int(time.time()*1000)},{ev['entity']},{ev['version']},{n},{(time.perf_counter()-t0)*1000:.2f}\n"); log.flush()


if __name__ == "__main__":
    asyncio.run(main())
