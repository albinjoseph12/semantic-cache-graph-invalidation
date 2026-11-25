#!/usr/bin/env python3
"""Short warm-up (JIT-free Python, but pool connections, pgvector index pages and the embedding model) before measurement."""
import argparse, json, random, threading, time, urllib.request

def worker(stop, base):
    while not stop.is_set():
        try:
            pid = random.randint(1, 5000)
            req = urllib.request.Request(base + "/api/query", data=json.dumps({"q": f"What is the price of product {pid}?"}).encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:
            time.sleep(0.05)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--seconds", type=int, default=10); ap.add_argument("--base", default="http://localhost:8000"); a = ap.parse_args()
    stop = threading.Event(); ts = [threading.Thread(target=worker, args=(stop, a.base), daemon=True) for _ in range(8)]
    [t.start() for t in ts]; time.sleep(a.seconds); stop.set(); time.sleep(0.3)
