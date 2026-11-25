#!/usr/bin/env python3
"""
Independent correctness checker (Phase 2).

Re-grades every cache-hit read in a run's requests.csv from raw committed database history --
product_history.csv / category_history.csv, dumped straight from the audit tables that
seed/seed_db.py's audit_product_history / audit_category_history triggers populate -- and never
reads the gateway's own self-reported `stale` / `wrong_entity` / `truth_versions_selfreported`
columns as ground truth. Those columns are read only for the side-by-side comparison this script
reports, so any disagreement between the gateway's self-grading and this independent grading is
visible rather than silently assumed away.

Usage:
    python3 analysis/independent_checker.py <rep_dir>              # one run
    python3 analysis/independent_checker.py --all results/graph-tx  # every rep dir under a mode
"""
import csv, json, os, sys
from bisect import bisect_right


def load_history(path, key_col, val_col):
    """{key: ([sorted ts_ms], [val at that ts])} -- a step function of committed values over time."""
    hist = {}
    if not os.path.exists(path):
        return hist
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            k = row[key_col]
            hist.setdefault(k, ([], []))
            hist[k][0].append(float(row["ts_ms"]))
            hist[k][1].append(int(row[val_col]))
    for k in hist:
        ts, vals = hist[k]
        order = sorted(range(len(ts)), key=lambda i: ts[i])
        hist[k] = ([ts[i] for i in order], [vals[i] for i in order])
    return hist


def truth_at(hist, key, ts_ms):
    """The last committed value for `key` at or before ts_ms; 0 if nothing had committed yet
    (matches the gateway's own coalesce(...,0) convention for an as-yet-untouched entity)."""
    if key not in hist:
        return 0
    ts, vals = hist[key]
    i = bisect_right(ts, ts_ms) - 1
    return vals[i] if i >= 0 else 0


def check_run(rep_dir, verbose=True):
    req_path = os.path.join(rep_dir, "requests.csv")
    if not os.path.exists(req_path):
        return None
    prod_hist = load_history(os.path.join(rep_dir, "product_history.csv"), "product_id", "version")
    cat_hist = load_history(os.path.join(rep_dir, "category_history.csv"), "category", "epoch")

    n_hits = 0
    disagree_stale = 0
    disagree_wrong = 0
    independent_stale = 0
    independent_wrong = 0
    selfreported_stale = 0
    selfreported_wrong = 0
    with open(req_path, newline="") as f:
        for row in csv.DictReader(f):
            if row["kind"] != "read" or row.get("hit") != "1":
                continue
            entities = [e for e in (row.get("entities") or "").split(";") if e]
            if not entities:
                continue
            n_hits += 1
            # locustfile.py's requests.csv uses ts_ms; open_loop_gen.py's open_loop.csv (Phase 3)
            # uses issued_ts_ms for the same quantity -- accept either.
            ts_ms = float(row["ts_ms"] if "ts_ms" in row and row["ts_ms"] else row["issued_ts_ms"])
            try:
                answer_versions = json.loads(row.get("answer_versions") or "{}")
            except json.JSONDecodeError:
                answer_versions = {}
            truth = {}
            for k in entities:
                kind, val = k.split(":", 1)
                truth[k] = truth_at(prod_hist, val, ts_ms) if kind == "product" else truth_at(cat_hist, val, ts_ms)
            ind_stale = any(answer_versions.get(k, -1) < truth.get(k, 0) for k in entities)
            cache_entities = set((row.get("cache_entities") or "").split(";")) if row.get("cache_entities") else set()
            ind_wrong = bool(cache_entities) and cache_entities != set(entities)

            self_stale = row.get("stale") == "1"
            self_wrong = row.get("wrong_entity") == "1"

            independent_stale += int(ind_stale); independent_wrong += int(ind_wrong)
            selfreported_stale += int(self_stale); selfreported_wrong += int(self_wrong)
            if ind_stale != self_stale:
                disagree_stale += 1
            if ind_wrong != self_wrong:
                disagree_wrong += 1

    result = {"rep_dir": rep_dir, "hits_checked": n_hits,
              "independent_stale": independent_stale, "selfreported_stale": selfreported_stale,
              "disagree_stale": disagree_stale,
              "independent_wrong_entity": independent_wrong, "selfreported_wrong_entity": selfreported_wrong,
              "disagree_wrong_entity": disagree_wrong}
    if verbose:
        print(json.dumps(result, indent=1))
    return result


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--all":
        root = sys.argv[2]
        results = []
        for dirpath, dirnames, filenames in os.walk(root):
            if "requests.csv" in filenames:
                r = check_run(dirpath, verbose=False)
                if r:
                    results.append(r)
        tot_hits = sum(r["hits_checked"] for r in results)
        tot_dis_stale = sum(r["disagree_stale"] for r in results)
        tot_dis_wrong = sum(r["disagree_wrong_entity"] for r in results)
        print(json.dumps({"runs": len(results), "hits_checked": tot_hits,
                          "disagree_stale": tot_dis_stale, "disagree_wrong_entity": tot_dis_wrong}, indent=1))
        for r in results:
            if r["disagree_stale"] or r["disagree_wrong_entity"]:
                print("DISAGREEMENT:", json.dumps(r))
    else:
        if len(sys.argv) < 2:
            print(__doc__); sys.exit(1)
        check_run(sys.argv[1])


if __name__ == "__main__":
    main()
