#!/usr/bin/env python3
"""
Extended analysis for the revised semantic-cache paper:

  A. the GPTCache baseline (the library itself) against the proposed transactional graph invalidation;
  B. three embedding families over a similarity-threshold grid, which asks whether the correctness
     result depends on the embedding or only on the invalidation;
  C. confidence intervals for the headline cell over the enlarged repetition count.

Metrics are computed by analyze.py's own `metrics` function, so nothing is defined twice.
"""
import glob, json, math, os, re, sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import ROOT, OUT, MODES, LABELS, STEADY, load, metrics

HEAD = ["hit_rate", "correct_hit_rate", "stale_per_1k_reads", "wrong_entity_per_hit_pct",
        "incorrect_per_1k_reads", "read_p50", "read_p99", "write_p99",
        "llm_calls_per_1k_reads", "cost_per_1k_reads_usd", "cache_entries_mean"]


def ci95(xs):
    xs = [float(x) for x in xs if x == x]
    if not xs:
        return (float("nan"), float("nan"), 0)
    if len(xs) == 1:
        return (xs[0], 0.0, 1)
    m = float(np.mean(xs)); sd = float(np.std(xs, ddof=1))
    return (m, 1.96 * sd / math.sqrt(len(xs)), len(xs))


def cell_rows(subdir, rs, tau):
    """Every repetition of one (mode-directory, read share, threshold) cell."""
    rows = []
    for d in sorted(glob.glob(f"{ROOT}/{subdir}/r{rs}_t{tau}/rep[0-9]*")):
        lr = load(d)
        if lr is None:
            continue
        r, m, t = lr
        m = metrics(r, m, *STEADY)
        m.update({"dir": subdir, "read_share": rs, "tau": tau, "rep": os.path.basename(d)})
        rows.append(m)
    return rows


def fmt(m, h, digits=2):
    if m != m:
        return "—"
    return f"{m:.{digits}f} ± {h:.{digits}f}" if h > 0 else f"{m:.{digits}f}"


def table(df, group, cols, path, title, digits=2):
    lines = [f"\n### {title}\n", "| " + group + " | n | " + " | ".join(cols) + " |",
             "|" + "---|" * (len(cols) + 2)]
    for key, g in df.groupby(group, sort=False):
        cells = [fmt(*ci95(list(g[c]))[:2], digits=digits) for c in cols]
        lines.append(f"| {key} | {len(g)} | " + " | ".join(cells) + " |")
    with open(path, "a") as f:
        f.write("\n".join(lines) + "\n")


def main():
    numbers = {}

    # ------------------------------------------------------------------ A. GPTCache baseline
    rows = []
    for tau in ("0.70", "0.80"):
        rows += cell_rows("gptcache", "0.75", tau)
        rows += cell_rows("graph-tx", "0.75", tau)
        rows += cell_rows("ttl", "0.75", tau)
    if rows:
        b = pd.DataFrame(rows)
        b["system"] = b["dir"].map({"gptcache": "GPTCache (library)", "graph-tx": "Graph (transactional)",
                                    "ttl": "Semantic cache + TTL"}).fillna(b["dir"])
        b.to_csv(f"{OUT}/gptcache_per_run.csv", index=False)
        open(f"{OUT}/gptcache_baseline.md", "w").write(
            "# GPTCache baseline, 75% reads, steady window\n")
        for tau in ("0.70", "0.80"):
            sub = b[b.tau == tau]
            if len(sub):
                table(sub, "system", HEAD, f"{OUT}/gptcache_baseline.md", f"tau = {tau}", digits=3)
        numbers["gptcache"] = {
            tau: {s: {c: ci95(list(g[c])) for c in HEAD}
                  for s, g in b[b.tau == tau].groupby("system")}
            for tau in ("0.70", "0.80")}

    # ------------------------------------------------------------------ B. embedding families
    fam_rows = []
    families = {"wl256": ("graph-tx", "ttl"), "wl64": ("graph-tx__wl64", "ttl__wl64"),
                "lsa256": ("graph-tx__lsa256", "ttl__lsa256")}
    for fam, (gd, td) in families.items():
        for tau in ("0.60", "0.70", "0.80"):
            for mode, sub in (("graph-tx", gd), ("ttl", td)):
                for m in cell_rows(sub, "0.75", tau):
                    m.update({"family": fam, "mode": mode})
                    fam_rows.append(m)
    if fam_rows:
        f = pd.DataFrame(fam_rows)
        f.to_csv(f"{OUT}/embedding_per_run.csv", index=False)
        f["cell"] = f["family"] + " / " + f["mode"] + " / tau=" + f["tau"].astype(str)
        open(f"{OUT}/embeddings.md", "w").write("# Embedding families, 75% reads, steady window\n")
        for fam in families:
            sub = f[f.family == fam]
            if len(sub):
                table(sub.sort_values(["mode", "tau"]), "cell", HEAD, f"{OUT}/embeddings.md", fam, digits=3)
        numbers["embeddings"] = {
            fam: {f"{mode}@{tau}": {c: ci95(list(g[c])) for c in HEAD}
                  for (mode, tau), g in f[f.family == fam].groupby(["mode", "tau"])}
            for fam in families}
        # the invariant the paper claims: no stale read under transactional graph invalidation,
        # in any family, at any threshold
        gt = f[f["mode"] == "graph-tx"]
        numbers["graph_tx_stale_total"] = int(gt["stale_reads"].sum()) if len(gt) else None
        numbers["graph_tx_runs"] = int(len(gt))
        tt = f[f["mode"] == "ttl"]
        numbers["ttl_stale_per_1k_range"] = [float(tt["stale_per_1k_reads"].min()),
                                             float(tt["stale_per_1k_reads"].max())] if len(tt) else None

    # ------------------------------------------------------------------ C. confidence intervals
    # the whole main matrix, so that every cell the paper quotes carries an interval and a count
    ci_rows = []
    for m in MODES + ["graph-cdc-lag100", "graph-cdc-lag1000"]:
        for rs in ("0.60", "0.75", "0.90"):
            for tau in ("0.50", "0.60", "0.70", "0.80", "0.90"):
                ci_rows += [dict(r, mode=m) for r in cell_rows(m, rs, tau)]
    if ci_rows:
        c = pd.DataFrame(ci_rows)
        c["mode"] = pd.Categorical(c["mode"], [m for m in MODES if m in set(c["mode"])], ordered=True)
        c = c.sort_values("mode")
        c.to_csv(f"{OUT}/ci_per_run.csv", index=False)
        open(f"{OUT}/ci_table.md", "w").write("# Main matrix, mean ± 95% CI half-width\n")
        for rs in ("0.90", "0.75", "0.60"):
            sub = c[(c.read_share.astype(str) == rs) & (c.tau.astype(str) == "0.70")]
            if len(sub):
                table(sub, "mode", HEAD, f"{OUT}/ci_table.md", f"{rs} reads, tau = 0.70", digits=3)
        numbers["ci"] = {str(m): {k: ci95(list(g[k])) for k in HEAD}
                         for m, g in c[(c.read_share.astype(str) == "0.75") &
                                       (c.tau.astype(str) == "0.70")].groupby("mode", observed=True)}

    json.dump(numbers, open(f"{OUT}/extended_numbers.json", "w"), indent=1, default=float)
    for fn in ("gptcache_baseline.md", "embeddings.md", "ci_table.md"):
        p = f"{OUT}/{fn}"
        if os.path.exists(p):
            print("\n" + "=" * 70 + f"\n{fn}\n" + open(p).read())
    print(json.dumps({k: v for k, v in numbers.items() if not isinstance(v, dict)}, indent=1))


if __name__ == "__main__":
    main()
