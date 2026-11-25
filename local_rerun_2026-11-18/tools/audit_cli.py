"""Run the content auditor until a stop-file appears, then write its summary. Usage: audit_cli.py DSN MAIN_PY STOPFILE OUT_JSON"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit import run_audit
async def main():
    dsn, main_py, stopf, out = sys.argv[1:5]
    ev = asyncio.Event()
    async def watch():
        while not os.path.exists(stopf): await asyncio.sleep(0.2)
        ev.set()
    w = asyncio.create_task(watch())
    res = await run_audit(dsn, main_py, ev)
    json.dump(res, open(out, "w"), indent=1)
asyncio.run(main())
