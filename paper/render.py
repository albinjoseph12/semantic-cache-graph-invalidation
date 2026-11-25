#!/usr/bin/env python3
"""
Render the manuscript from a template whose numbers are references into the analysis output.

A placeholder has the form

    {{ source | key | format }}

  source  agg      results/analysis/summary_metrics.csv, keyed "scenario/policy/metric"
          ci       results/analysis/extended_numbers.json -> chain_ci, "scenario/policy/metric"
          split    extended_numbers.json -> split,        "scenario/policy/metric"
          sens     extended_numbers.json -> sensitivity,  "config/metric"
          det      results/analysis/detection.json,       "scenario/policy/metric"
          n        number of repetitions,                 "scenario/policy"
          reallm      LLM_BACKEND=real reps, "mode/metric" (hit_rate/stale_rate/wrong_rate/
                       lat_mean/lat_p50/lat_p99/cost), read live from results/<mode>__real/
          reallmind   independent_checker.py re-graded over the reallm reps, "mode/hits|disagree"
  format  mean     mean only, 3 significant figures unless a precision is appended (mean:1)
          ci       mean ± 95% CI half-width  (ci:1 for one decimal)
          sd       mean ± sample standard deviation
          int      mean rounded to an integer
          pct      mean as a percentage with one decimal

Because every number in the rendered manuscript is a lookup, the text cannot disagree with the
measurements; changing the data and re-rendering changes the paper.
"""
import json, math, os, re, sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AN = os.path.join(ROOT, "results", "analysis")

_ext = None
_per = None


def per():
    """Every analysed run of the semantic-cache matrix, from extended.py's per-run tables."""
    global _per
    if _per is None:
        frames = []
        for f in ("gptcache_per_run.csv", "embedding_per_run.csv", "ci_per_run.csv"):
            p = os.path.join(AN, f)
            if os.path.exists(p):
                frames.append(pd.read_csv(p))
        _per = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if len(_per):
            # the three per-run tables overlap (the same run appears in the baseline, the embedding
            # and the confidence-interval collections); one run must be counted once
            _per = _per.drop_duplicates(subset=["dir", "read_share", "tau", "rep"], keep="first")
    return _per


def ext():
    global _ext
    if _ext is None:
        _ext = json.load(open(os.path.join(AN, "extended_numbers.json")))
    return _ext



def _tcrit(n):
    """Two-sided 95% Student-t critical value for n observations (n-1 degrees of freedom)."""
    if n is None or n < 2:
        return float("nan")
    try:
        from scipy.stats import t as _t
        return float(_t.ppf(0.975, n - 1))
    except Exception:
        pass
    tbl = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
           8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
           15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
           24: 2.064, 29: 2.045, 39: 2.023, 59: 2.000, 99: 1.984}
    df = n - 1
    for k in sorted(tbl):
        if df <= k:
            return tbl[k]
    return 1.96


def ci95(xs):
    xs = [float(x) for x in xs if x == x]
    if not xs:
        return (float("nan"), float("nan"), 0)
    if len(xs) == 1:
        return (xs[0], 0.0, 1)
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    n = len(xs)
    return (m, _tcrit(n) * sd / math.sqrt(n), n)


def lookup(source, key):
    """Return (mean, half_width, sd, n)."""
    parts = key.split("/")
    if source in ("cell", "n"):
        # one condition of the main matrix: "mode/read_share/tau/metric"
        mode, rs, tau, metric = parts
        rows = per()
        sel = rows[(rows["dir"] == mode)
                   & (pd.to_numeric(rows.read_share, errors="coerce") == float(rs))
                   & (pd.to_numeric(rows.tau, errors="coerce") == float(tau))]
        if source == "n":
            return (len(sel), 0.0, 0.0, len(sel))
        xs = list(pd.to_numeric(sel[metric], errors="coerce").dropna())
        m, h, n = ci95(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
        return (m, h, sd, n)
    if source == "stalepct":
        # stale reads as a percentage of hits, per run, then averaged: "mode/read_share/tau"
        mode, rs, tau = parts
        rows = per()
        sel = rows[(rows["dir"] == mode) & (pd.to_numeric(rows.read_share, errors="coerce") == float(rs))
                   & (pd.to_numeric(rows.tau, errors="coerce") == float(tau))]
        xs = [100.0 * st / (1000.0 * hr) for st, hr in zip(pd.to_numeric(sel["stale_per_1k_reads"], errors="coerce"),
                                                            pd.to_numeric(sel["hit_rate"], errors="coerce")) if hr and hr > 0]
        m, h, n = ci95(xs)
        return (m, h, 0.0, n)
    if source == "cellred":
        # percentage reduction of one policy against another in the same cell: "modeA/modeB/read_share/tau/metric"
        a, b, rs, tau, metric = parts
        va = lookup("cell", f"{a}/{rs}/{tau}/{metric}")[0]; vb = lookup("cell", f"{b}/{rs}/{tau}/{metric}")[0]
        return (100.0 * (1.0 - va / vb), 0.0, 0.0, 0)
    if source == "qhit":
        # hit rate of one question class in the steady window (40-120 s after the first request, as analysis/analyze.py),
        # mean over repetitions: "mode/read_share/tau/qclass"
        import glob
        mode, rs, tau, q = parts
        xs = []
        for f in sorted(glob.glob(os.path.join(ROOT, "results", mode, f"r{rs}_t{tau}", "rep*", "requests.csv"))):
            r = pd.read_csv(f)
            ts = pd.to_numeric(r["issued_ts_ms"], errors="coerce"); t = (ts - ts.min()) / 1000
            w = r[(t >= 40) & (t < 120) & (r["kind"] == "read") & (pd.to_numeric(r["status"], errors="coerce") == 200) & (r["qclass"] == q)]
            if len(w):
                xs.append(float((pd.to_numeric(w["hit"], errors="coerce") == 1).mean()))
        m, h, n = ci95(xs)
        return (m, h, 0.0, n)
    if source in ("gpt", "emb", "ci"):
        e = ext()
        node = e[{"gpt": "gptcache", "emb": "embeddings", "ci": "ci"}[source]]
        for p in parts[:-1]:
            node = node[p]
        m, h, n = node[parts[-1]]
        if n and n > 1:
            h = h * _tcrit(n) / 1.96
        return (m, h, 0.0, n)
    if source in ("campaign", "campaignnew"):
        import glob as _g
        # The split-host open-loop generator (load/open_loop_gen.py) writes requests.csv directly;
        # it never produces locust_stats.csv, which was this check's original target from an earlier,
        # local-Locust-only version of the harness. Checking for that file here found zero runs.
        runs = [d for d in _g.glob(os.path.join(ROOT, "results", "*", "r*", "rep[0-9]*"))
                if os.path.exists(os.path.join(d, "requests.csv"))]
        if source == "campaign":
            return (len(runs), 0.0, 0.0, len(runs))
        new = [d for d in runs if ("gptcache" in d or "__wl64" in d or "__lsa256" in d
                                   or os.path.basename(d) in ("rep3", "rep4"))]
        return (len(new), 0.0, 0.0, len(new))
    if source == "gtx":
        # every transactional-graph run in the campaign, across mixes, thresholds and embeddings.
        # hits are counted from requests.csv directly (kind==read, hit==1), NOT from the gateway's
        # own self-reported final_metrics.json "hits" counter -- an external review found the two
        # disagree by ~15 hits out of ~19,500 (the self-reported figure is the higher one), which is
        # exactly the kind of gap the project's own independent-verification standard exists to
        # catch rather than paper over by trusting the more convenient number.
        import csv as _csv, glob as _g
        rep_dirs = sorted(set(d for d in _g.glob(os.path.join(ROOT, "results", "graph-tx*", "r*", "rep[0-9]*"))
                              if os.path.exists(os.path.join(d, "requests.csv"))))
        hits = 0
        for d in rep_dirs:
            with open(os.path.join(d, "requests.csv"), newline="") as f:
                hits += sum(1 for row in _csv.DictReader(f) if row.get("kind") == "read" and row.get("hit") == "1")
        if key == "runs":
            return (len(rep_dirs), 0.0, 0.0, len(rep_dirs))
        if key == "hits":
            return (hits, 0.0, 0.0, len(rep_dirs))
        raise KeyError(key)
    if source == "scalar":
        e = ext()
        v = e[parts[0]]
        if isinstance(v, list):
            return (v[0], v[1], 0.0, 0)
        return (float(v), 0.0, 0.0, 1)
    if source == "reallm":
        # LLM_BACKEND=real external-validity check (Section V-J): per-rep numbers computed directly
        # from results/<mode>__real/r0.75_t0.70/rep*/requests.csv + final_metrics.json, the same raw
        # logs independent_checker.py grades, so this table cannot disagree with that verification.
        import csv as _csv, glob as _g, json as _j
        mode, metric = parts
        reps = sorted(_g.glob(os.path.join(ROOT, "results", f"{mode}__real", "r0.75_t0.70", "rep*")))
        if metric == "cost":
            xs = [_j.load(open(os.path.join(d, "final_metrics.json")))["cost_usd"] for d in reps]
            m, h, n = ci95(xs)
            return (m, h, 0.0, n)
        xs = []
        for d in reps:
            lat, hits, stale, wrong, reads = [], 0, 0, 0, 0
            with open(os.path.join(d, "requests.csv"), newline="") as f:
                for row in _csv.DictReader(f):
                    if row["kind"] != "read":
                        continue
                    reads += 1
                    if row.get("latency_ms"):
                        lat.append(float(row["latency_ms"]))
                    if row.get("hit") == "1":
                        hits += 1
                        stale += row.get("stale") == "1"
                        wrong += row.get("wrong_entity") == "1"
            lat.sort()
            if metric == "hit_rate":
                xs.append(hits / reads if reads else 0.0)
            elif metric == "stale_rate":
                xs.append(stale / hits if hits else 0.0)
            elif metric == "wrong_rate":
                xs.append(wrong / hits if hits else 0.0)
            elif metric in ("lat_mean", "lat_p50", "lat_p99"):
                if not lat:
                    xs.append(float("nan"))
                elif metric == "lat_mean":
                    xs.append(sum(lat) / len(lat))
                else:
                    p = 0.5 if metric == "lat_p50" else 0.99
                    xs.append(lat[min(len(lat) - 1, int(len(lat) * p))])
            else:
                raise KeyError(metric)
        m, h, n = ci95(xs)
        return (m, h, 0.0, n)
    if source == "reallmind":
        # independent_checker.py re-graded across the reallm reps (paired requests.csv + the same
        # run's own product_history.csv/category_history.csv); "hits"/"disagree" are exact counts.
        import glob as _g
        mode = parts[0]
        root = os.path.join(ROOT, "results", f"{mode}__real")
        sys.path.insert(0, os.path.join(ROOT, "analysis"))
        import independent_checker as _ic
        reps = sorted(_g.glob(os.path.join(root, "r0.75_t0.70", "rep*")))
        tot_hits = tot_dis = 0
        for d in reps:
            r = _ic.check_run(d, verbose=False)
            if r:
                tot_hits += r["hits_checked"]; tot_dis += r["disagree_stale"] + r["disagree_wrong_entity"]
        if parts[1] == "hits":
            return (tot_hits, 0.0, 0.0, len(reps))
        if parts[1] == "disagree":
            return (tot_dis, 0.0, 0.0, len(reps))
        raise KeyError(parts[1])
    if source == "rerun":
        # results/full_rerun_2026-11-19/summary.json (the full local re-run, Section V-K), produced by that
        # re-run's tools/summary_for_paper.py from analyze.py, independent_checker.py and answer_check.py output.
        # The key is a ":"-separated path, e.g. "cell:graph-tx/r0.75_t0.70:hit_rate:local".
        v = json.load(open(os.path.join(ROOT, "results", "full_rerun_2026-11-19", "summary.json")))
        for p in key.split(":"):
            v = v[p]
        return (float(v), 0.0, 0.0, 0)
    raise KeyError(source)


def sig(x, digits=3):
    if x != x:
        return "—"
    if x == 0:
        return "0"
    d = max(0, digits - 1 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{d}f}"


def fmt(val, spec):
    m, h, sd, n = val
    name, _, prec = spec.partition(":")
    p = int(prec) if prec else None
    if name == "mean":
        return sig(m) if p is None else f"{m:.{p}f}"
    if name == "int":
        return f"{round(m):d}"
    if name == "intc":
        return f"{round(m):,d}"
    if name == "pct":
        return f"{m:.1f}" if p is None else f"{m:.{p}f}"
    if name == "ci":
        s = (lambda x: sig(x)) if p is None else (lambda x: f"{x:.{p}f}")
        return f"{s(m)} ± {s(h)}"
    if name == "sd":
        s = (lambda x: sig(x)) if p is None else (lambda x: f"{x:.{p}f}")
        return f"{s(m)} ± {s(sd)}"
    if name == "n":
        return str(n)
    if name == "pctshare":
        return f"{100 * m:.0f}"
    if name == "pct100":
        return f"{100 * m:.0f}"
    if name == "range":
        s_ = (lambda x: sig(x)) if p is None else (lambda x: f"{x:.{p}f}")
        return f"{s_(m)}–{s_(h)}"
    raise KeyError(spec)


PAT = re.compile(r"\{\{\s*([a-z]+)\s*\|\s*(.+?)\s*\|\s*([a-z0-9]+(?::\d+)?)\s*\}\}")


def render(text):
    misses = []

    def sub(m):
        src, key, spec = m.group(1), m.group(2), m.group(3)
        try:
            return fmt(lookup(src, key), spec)
        except Exception as e:
            misses.append(f"{m.group(0)}  -> {type(e).__name__}: {e}")
            return m.group(0)

    out = PAT.sub(sub, text)
    return out, misses


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    out, misses = render(open(src).read())
    open(dst, "w").write(out)
    print(f"rendered {src} -> {dst}; {len(PAT.findall(open(src).read()))} placeholders, {len(misses)} unresolved")
    for m in misses:
        print("  UNRESOLVED", m)
    sys.exit(1 if misses else 0)
