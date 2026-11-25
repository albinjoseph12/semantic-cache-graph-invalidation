"""Race-stress arm: category-question pressure with a natural write rate (price-only writes), plus the content auditor.
Usage: stress_driver.py --gateway URL --dsn DSN --main-py FILE --duration S --readers N --write-rate W --seed K --out DIR"""
import argparse, asyncio, json, random, statistics, sys, time
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).parent)); sys.path.insert(0, str(Path(__file__).parents[2] / "corrected_semcache-testbed" / "app"))
from audit import run_audit
from queries import CATEGORY_TEMPLATES

NAMES = json.load(open(Path(__file__).parents[2] / "corrected_semcache-testbed" / "load" / "product_names.json"))
CATS = sorted({v["category"] for v in NAMES.values()})

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway"); ap.add_argument("--dsn"); ap.add_argument("--main-py")
    ap.add_argument("--duration", type=float, default=60); ap.add_argument("--readers", type=int, default=200)
    ap.add_argument("--write-rate", type=float, default=40); ap.add_argument("--seed", type=int, default=1); ap.add_argument("--hot-cats", type=int, default=0); ap.add_argument("--target-cheapest", type=float, default=0.0); ap.add_argument("--out")
    a = ap.parse_args(); rnd = random.Random(a.seed); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    hot = CATS[:a.hot_cats] if a.hot_cats else CATS
    hot_pids = [int(k) for k, v in NAMES.items() if v["category"] in set(hot)]
    cheapest_pids = []
    if a.target_cheapest > 0:       # ids of the 8 cheapest products per hot category (those that decide a category answer)
        import asyncpg
        c = await asyncpg.connect(a.dsn)
        for cat in hot:
            cheapest_pids += [r["id"] for r in await c.fetch("SELECT id FROM products WHERE category=$1 ORDER BY price_cents ASC LIMIT 8", cat)]
        await c.close()
    stop = asyncio.Event(); log = {"reads": 0, "hits": 0, "flag_stale": 0, "flag_wrong": 0, "errors": 0, "writes": 0, "write_errors": 0}
    lat = []; kinds = {}
    limits = httpx.Limits(max_connections=a.readers + 50, max_keepalive_connections=a.readers + 50)
    async with httpx.AsyncClient(base_url=a.gateway, timeout=30, limits=limits) as cl:
        async def reader(i):
            r = random.Random(a.seed * 1000 + i)
            while not stop.is_set():
                q = r.choice(CATEGORY_TEMPLATES).format(cat=r.choice(hot)); t0 = time.perf_counter()
                try:
                    d = (await cl.post("/api/query", json={"q": q})).json()
                except Exception as e:
                    log["errors"] += 1; kinds[type(e).__name__ + ":" + str(e)[:60]] = kinds.get(type(e).__name__ + ":" + str(e)[:60], 0) + 1; continue
                lat.append((time.perf_counter() - t0) * 1000); log["reads"] += 1
                log["hits"] += int(bool(d.get("hit"))); log["flag_stale"] += int(bool(d.get("stale"))); log["flag_wrong"] += int(bool(d.get("wrong_entity")))
        async def one_write(r):
            pid = r.choice(cheapest_pids) if (cheapest_pids and r.random() < a.target_cheapest) else r.choice(hot_pids); base = NAMES[str(pid)]["price_cents"]
            try:
                resp = await cl.post(f"/api/product/{pid}", json={"price_cents": max(100, int(base * r.uniform(0.85, 1.15)))})
                log["writes"] += int(resp.status_code == 200); log["write_errors"] += int(resp.status_code != 200)
            except Exception:
                log["write_errors"] += 1
        async def writer():
            # open-loop Poisson arrivals: a slow gateway must not throttle the offered write rate
            r = random.Random(a.seed * 7 + 1); inflight = set()
            while not stop.is_set():
                await asyncio.sleep(r.expovariate(a.write_rate))
                t = asyncio.create_task(one_write(r)); inflight.add(t); t.add_done_callback(inflight.discard)
            await asyncio.gather(*inflight, return_exceptions=True)
        auditor = asyncio.create_task(run_audit(a.dsn, a.main_py, stop))
        tasks = [asyncio.create_task(reader(i)) for i in range(a.readers)] + [asyncio.create_task(writer())]
        await asyncio.sleep(a.duration); stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        audit = await auditor
        metrics = (await cl.get("/metrics")).json()
    qs = statistics.quantiles(lat, n=100, method="inclusive") if len(lat) > 100 else [0] * 99
    res = dict(arm="stress", duration=a.duration, readers=a.readers, write_rate=a.write_rate, hot_cats=a.hot_cats, target_cheapest=a.target_cheapest, seed=a.seed, client=log,
               error_kinds=kinds, hit_rate=log["hits"] / max(1, log["reads"]), read_p50_ms=qs[49], read_p99_ms=qs[98], audit=audit,
               gateway_metrics={k: metrics.get(k) for k in ("mode", "reads", "hits", "misses", "stale", "wrong_entity", "insert_rejected", "writes", "invalidated_entries", "llm_calls")})
    (out / "summary.json").write_text(json.dumps(res, indent=1)); print(json.dumps({k: res[k] for k in ("hit_rate", "audit", "gateway_metrics")}, indent=1))

asyncio.run(main())
