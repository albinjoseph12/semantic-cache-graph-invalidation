#!/usr/bin/env python3
"""
Open-loop, fixed-rate request generator (Phase 3). Replaces locustfile.py's closed-loop
wait-time model for the comparator campaign: locustfile.py couples arrival rate to the
service's own response time (Phase 1 audit finding, and the manuscript itself should already
disclose this the way TACL's did -- check before claiming novelty here). Every policy under
comparison is driven by the *same* pre-generated arrival trace (fixed seed), issued from this
process regardless of how long each request takes to answer, so a slow policy shows up as
missed/rejected/failed arrivals rather than as a silently lower offered rate.

    python3 load/open_loop_gen.py --rate 40 --duration 120 --seed 20260906 \
        --host http://<benchmark-host>:8000 --out results/<mode>/.../rep<k>/open_loop.csv

Run this FROM the load-generator host (Stage 3-equivalent "keep the load generator off the
measured service host"), pointed at the benchmark host's private IP -- exactly the TACL
pattern (orchestrate_split_experiment.sh's role, not yet written for this project).

Per-request outcomes recorded: scheduled_ts_ms, issued_ts_ms, completed_ts_ms (or empty if
never completed), status, outcome (ok / client_dropped / rejected / timed_out / failed),
latency_ms, schedule_lag_ms (issued - scheduled: how far actual dispatch fell behind plan).
"""
import argparse, asyncio, csv, json, os, random, sys, time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from queries import PRODUCT_TEMPLATES, COMPARE_TEMPLATES, CATEGORY_TEMPLATES  # noqa: E402


def zipf_id(rng, n, alpha=1.07):
    return max(1, min(n, int(n ** (rng.random() ** alpha))))


def build_trace(seed, rate_per_s, duration_s, n_products, names, read_share):
    """A fixed, reproducible list of (scheduled_offset_s, kind, payload) -- identical for every
    policy run with the same seed/rate/duration, so no comparator sees an easier or harder trace."""
    rng = random.Random(seed)
    cats = sorted({v["category"] for v in names.values()})

    def ref(pid):
        r = rng.random()
        if r < 0.4:
            return f"product {pid}"
        if r < 0.6:
            return f"item #{pid}"
        return names[str(pid)]["name"]

    def make_query():
        r = rng.random()
        if r < 0.70:
            return "product", rng.choice(PRODUCT_TEMPLATES).format(ref=ref(zipf_id(rng, n_products)))
        if r < 0.85:
            a = zipf_id(rng, n_products); b = zipf_id(rng, n_products)
            while b == a:
                b = zipf_id(rng, n_products)
            return "compare", rng.choice(COMPARE_TEMPLATES).format(ref1=ref(a), ref2=ref(b))
        return "category", rng.choice(CATEGORY_TEMPLATES).format(cat=rng.choice(cats))

    n_arrivals = int(rate_per_s * duration_s)
    # Poisson process: inter-arrival times are exponential with mean 1/rate -- the standard
    # fixed-rate open-loop arrival model, not a uniform/synchronous one
    trace = []
    t = 0.0
    for _ in range(n_arrivals):
        t += rng.expovariate(rate_per_s)
        if t > duration_s:
            break
        if rng.random() < read_share:
            qclass, q = make_query()
            trace.append((t, "read", qclass, {"q": q}))
        else:
            pid = rng.randint(1, n_products)
            body = {"price_cents": max(100, int(names[str(pid)]["price_cents"] * rng.uniform(0.85, 1.15)))} \
                if rng.random() < 0.7 else {"stock": rng.choice([0, 5, 20, 50, 100, 200])}
            trace.append((t, "write", "update", body, pid))
    return trace


async def dispatch_one(client, host, item, out_rows, timeout_s):
    scheduled_offset, kind, qclass = item[0], item[1], item[2]
    issued_ts = time.time()
    try:
        if kind == "read":
            r = await client.post(f"{host}/api/query", json=item[3], timeout=timeout_s)
        else:
            pid = item[4]
            r = await client.post(f"{host}/api/product/{pid}", json=item[3], timeout=timeout_s)
        completed_ts = time.time()
        d = {}
        try:
            d = r.json()
        except Exception:
            pass
        outcome = "ok" if r.status_code == 200 else ("rejected" if r.status_code in (429, 503) else "failed")
        out_rows.append({"scheduled_offset_s": round(scheduled_offset, 3), "kind": kind, "qclass": qclass,
                          "issued_ts_ms": int(issued_ts * 1000), "completed_ts_ms": int(completed_ts * 1000),
                          "status": r.status_code, "outcome": outcome,
                          "latency_ms": round((completed_ts - issued_ts) * 1000, 2),
                          "hit": int(bool(d.get("hit"))), "stale": int(bool(d.get("stale"))),
                          "wrong_entity": int(bool(d.get("wrong_entity"))),
                          "entities": ";".join(d.get("entities", [])) if kind == "read" else f"product:{item[4]}",
                          "answer_versions": json.dumps(d.get("answer_versions", {})) if kind == "read" else "",
                          "cache_entities": (";".join(d.get("cache_entities", "").split("|"))
                                             if kind == "read" and d.get("cache_entities") not in (None, "", "-") else "")})
    except (httpx.TimeoutException, httpx.ConnectError) as e:
        completed_ts = time.time()
        outcome = "timed_out" if isinstance(e, httpx.TimeoutException) else "client_dropped"
        out_rows.append({"scheduled_offset_s": round(scheduled_offset, 3), "kind": kind, "qclass": qclass,
                          "issued_ts_ms": int(issued_ts * 1000), "completed_ts_ms": "",
                          "status": 0, "outcome": outcome, "latency_ms": round((completed_ts - issued_ts) * 1000, 2),
                          "hit": "", "stale": "", "wrong_entity": "", "entities": "", "answer_versions": "", "cache_entities": ""})


async def wait_ready(host, timeout_s=60):
    """Poll until the gateway answers, or raise -- firing a fixed-rate trace at a host that isn't
    listening yet would silently record every arrival as client_dropped (found exactly this way
    during the first real split-host timing run: the gateway was bound to 127.0.0.1 and never
    reachable at all, which a fixed sleep before starting would not have distinguished from "not
    warmed up yet")."""
    async with httpx.AsyncClient() as client:
        for _ in range(timeout_s):
            try:
                r = await client.get(f"{host}/metrics", timeout=2.0)
                if r.status_code == 200:
                    return
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1)
    raise RuntimeError(f"{host} never became ready within {timeout_s}s")


async def run(args):
    await wait_ready(args.host)
    trace = build_trace(args.seed, args.rate, args.duration, args.products, json.load(open(args.names)), args.read_share)
    scheduled = len(trace)
    out_rows = []
    t_start = time.time()
    tasks = []
    async with httpx.AsyncClient() as client:
        for item in trace:
            wait = (t_start + item[0]) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            # fire-and-continue: this request's own latency does not delay the next scheduled
            # dispatch -- the defining property of an open-loop generator
            tasks.append(asyncio.create_task(dispatch_one(client, args.host, item, out_rows, args.timeout)))
        await asyncio.gather(*tasks, return_exceptions=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["scheduled_offset_s", "kind", "qclass", "issued_ts_ms", "completed_ts_ms",
                                          "status", "outcome", "latency_ms", "hit", "stale", "wrong_entity",
                                          "entities", "answer_versions", "cache_entities"])
        w.writeheader()
        for row in sorted(out_rows, key=lambda r: r["scheduled_offset_s"]):
            w.writerow(row)

    issued = len(out_rows)
    completed = sum(1 for r in out_rows if r["completed_ts_ms"] != "")
    print(json.dumps({"scheduled": scheduled, "issued": issued, "completed": completed,
                      "rejected": sum(1 for r in out_rows if r["outcome"] == "rejected"),
                      "failed": sum(1 for r in out_rows if r["outcome"] == "failed"),
                      "client_dropped": sum(1 for r in out_rows if r["outcome"] == "client_dropped"),
                      "timed_out": sum(1 for r in out_rows if r["outcome"] == "timed_out")}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="e.g. http://10.128.0.2:8000")
    ap.add_argument("--rate", type=float, required=True, help="arrivals/second")
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--seed", type=int, default=20260906)
    ap.add_argument("--products", type=int, default=int(os.environ.get("SC_PRODUCTS", "5000")))
    ap.add_argument("--read-share", type=float, default=float(os.environ.get("READ_SHARE", "0.60")))
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--names", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "product_names.json"))
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
