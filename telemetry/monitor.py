#!/usr/bin/env python3
"""
200 ms monitor: polls the gateway /metrics (cumulative counters, cache size, token cost), Redis memory and
PostgreSQL activity, and writes one row per tick to a CSV. Rates are derived in analysis from counter deltas.
"""
import argparse, csv, json, os, time, urllib.request
import psycopg2

KEYS = ["reads", "hits", "misses", "stale", "wrong_entity", "writes", "invalidated_entries", "flushes", "llm_calls", "prompt_tokens",
        "completion_tokens", "hit_latency_ms_sum", "miss_latency_ms_sum", "write_latency_ms_sum", "hit_lookup_ms_sum", "expired", "insert_rejected",
        "cache_entries", "dep_edges", "redis_used_memory", "cost_usd"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True); ap.add_argument("--duration", type=float, default=125); ap.add_argument("--period", type=float, default=0.2)
    ap.add_argument("--gateway", default="http://localhost:8000")
    ap.add_argument("--dsn", default=os.environ.get("SC_DSN", "dbname=semcache user=acb password=acb host=localhost port=5432"))
    a = ap.parse_args()
    conn = psycopg2.connect(a.dsn); conn.autocommit = True; cur = conn.cursor(); cur.execute("SET statement_timeout = 150")
    f = open(a.out, "w", newline="", buffering=1 << 16); w = csv.writer(f)
    w.writerow(["ts_ms", "t_s"] + KEYS + ["pg_active", "pg_backends", "poll_ms"])
    t0 = time.time(); nxt = t0
    while time.time() - t0 < a.duration:
        tick = time.time()
        try:
            with urllib.request.urlopen(a.gateway + "/metrics", timeout=0.18) as r:
                m = json.loads(r.read())
        except Exception:
            m = {}
        try:
            cur.execute("SELECT count(*) FILTER (WHERE state='active' AND pid <> pg_backend_pid()), count(*) FROM pg_stat_activity WHERE datname = current_database() AND backend_type='client backend'")
            pa, pb = cur.fetchone()
        except Exception:
            pa = pb = ""
            try: conn.rollback()
            except Exception: pass
        w.writerow([int(tick * 1000), f"{tick - t0:.3f}"] + [m.get(k, "") for k in KEYS] + [pa, pb, f"{(time.time() - tick) * 1000:.1f}"])
        nxt += a.period
        s = nxt - time.time()
        if s > 0: time.sleep(s)
        else: nxt = time.time()
    f.close()


if __name__ == "__main__":
    main()
