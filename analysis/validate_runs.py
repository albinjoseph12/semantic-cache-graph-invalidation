#!/usr/bin/env python3
"""
Flag runs that measured the harness rather than the policy.

A run is rejected when a large share of its reads returned 5xx (which happens when a gateway from a
previous run was still bound to the port and served this run's traffic), when it produced no
successful read, or when its recorded mode does not match the directory it was written to.
"""
import glob, json, os, sys
import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
bad = []
for d in sorted(glob.glob(f"{ROOT}/*/r*_t*/rep[0-9]*")):
    f = os.path.join(d, "requests.csv")
    if not os.path.exists(f) or os.path.getsize(f) < 1000:
        bad.append((d, "missing or empty requests.csv")); continue
    try:
        r = pd.read_csv(f)
    except Exception as e:
        bad.append((d, f"unreadable: {e}")); continue
    rd = r[r.kind == "read"]
    if len(rd) == 0:
        bad.append((d, "no reads")); continue
    err = (rd.status != 200).mean()
    if err > 0.02:
        bad.append((d, f"{100*err:.1f}% of reads returned an error status"))
        continue
    fm = os.path.join(d, "final_metrics.json")
    if os.path.exists(fm):
        try:
            mode = json.load(open(fm)).get("mode")
            want = os.path.basename(os.path.dirname(os.path.dirname(d))).split("__")[0].split("-lag")[0]
            if mode and mode != want:
                bad.append((d, f"gateway reported mode={mode} but the run is filed under {want}"))
        except Exception:
            pass
print(f"{len(bad)} suspect run(s)")
for d, why in bad:
    print(" ", os.path.relpath(d, ROOT), "->", why)
json.dump([os.path.relpath(d, ROOT) for d, _ in bad], open(f"{ROOT}/analysis/suspect_runs.json", "w"), indent=1)
