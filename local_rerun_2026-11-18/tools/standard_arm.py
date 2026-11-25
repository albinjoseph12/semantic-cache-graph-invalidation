"""Standard-mix arm: the testbed's own Locust workload (reduced scale) + the content auditor running alongside.
Usage: standard_arm.py --gateway URL --dsn DSN --main-py FILE --duration S --rep K --out DIR --testbed DIR"""
import argparse, asyncio, csv, json, os, statistics, subprocess, sys
from pathlib import Path
import httpx
sys.path.insert(0, str(Path(__file__).parent))
from audit import run_audit

async def main():
    ap = argparse.ArgumentParser()
    for k in ("gateway", "dsn", "main-py", "out", "testbed"): ap.add_argument("--" + k)
    ap.add_argument("--duration", type=int, default=60); ap.add_argument("--rep", type=int, default=1); ap.add_argument("--users", type=int, default=200)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, READ_SHARE="0.60", SC_USERS=str(a.users), SC_DURATION=str(a.duration), SC_REQ_LOG=str(out / "requests.csv"))
    locust = str(Path(sys.executable).parent / "locust")
    stop = asyncio.Event()
    auditor = asyncio.create_task(run_audit(a.dsn, a.main_py, stop))
    proc = await asyncio.create_subprocess_exec(locust, "-f", str(Path(a.testbed) / "load" / "locustfile.py"), "--headless", "-H", a.gateway,
                                                "-u", str(a.users), "-r", "100", "-t", f"{a.duration}s", "--csv", str(out / "locust"), "--only-summary",
                                                env=env, stdout=open(out / "locust.log", "w"), stderr=subprocess.STDOUT)
    await proc.wait(); stop.set(); audit = await auditor
    async with httpx.AsyncClient(base_url=a.gateway, timeout=30) as cl:
        m = (await cl.get("/metrics")).json()
    reads = [r for r in csv.DictReader(open(out / "requests.csv")) if r["kind"] == "read" and r["status"] == "200"]
    lat = sorted(float(r["latency_ms"]) for r in reads)
    hits = [r for r in reads if r["hit"] == "1"]
    pct = lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None
    res = dict(arm="standard", duration=a.duration, users=a.users, read_share=0.60, rep=a.rep, reads=len(reads), hits=len(hits),
               hit_rate=len(hits) / max(1, len(reads)), read_p50_ms=pct(0.50), read_p99_ms=pct(0.99),
               stale_flag_hits=sum(r["stale"] == "1" for r in hits), wrong_entity_hits=sum(r["wrong_entity"] == "1" for r in hits),
               category_reads=sum(r["qclass"] == "category" for r in reads), audit=audit,
               gateway_metrics={k: m.get(k) for k in ("mode", "reads", "hits", "misses", "stale", "wrong_entity", "insert_rejected", "writes", "invalidated_entries", "llm_calls")})
    (out / "summary.json").write_text(json.dumps(res, indent=1)); print(json.dumps({k: res[k] for k in ("reads", "hit_rate", "read_p50_ms", "stale_flag_hits", "audit")}, indent=1))

asyncio.run(main())
