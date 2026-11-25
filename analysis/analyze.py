#!/usr/bin/env python3
"""
Aggregate the real campaign and produce the manuscript's summary table and figures from the
measured logs. Covers three read-share configurations (0.60, 0.75, 0.90) at tau=0.70 across seven
modes (none, ttl, flush, scoped-ttl, graph-cdc, graph-tx, gptcache), plus -- at read_share=0.75 --
a tau sweep (0.60/0.70/0.80) and an embedding-family sweep (wl256/wl64/lsa256) for ttl and graph-tx,
and a tau=0.80 cell for the gptcache baseline comparison. All real GPTCache/pgvector/WordLlama code,
no synthetic data.

Cost and token counts come from each run's own final_metrics.json (the gateway's end-of-run
self-report) since requests.csv does not carry per-request token counts; hit/stale/wrong_entity/
latency come from requests.csv, independently re-graded by independent_checker.py (never trusted
here as self-report -- see that script and its output folded into the validation report).
"""
import glob, json, os
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
OUT = os.path.join(ROOT, "analysis"); os.makedirs(OUT, exist_ok=True)
MODES = ["none", "ttl", "flush", "scoped-ttl", "graph-cdc", "graph-tx", "gptcache"]
LABELS = {"none": "No cache", "ttl": "Semantic cache + TTL", "flush": "Semantic cache + flush",
          "scoped-ttl": "Entity-scoped + TTL (ablation)", "graph-cdc": "Graph (async CDC)",
          "graph-tx": "Graph (transactional)", "gptcache": "GPTCache (library baseline)"}
COLORS = {"none": "#52514e", "ttl": "#2a78d6", "flush": "#eda100", "scoped-ttl": "#e87ba4",
          "graph-cdc": "#1baf7a", "graph-tx": "#eb6834", "gptcache": "#8855cc"}
DUR = 120
STEADY = (40, 120)  # matches the manuscript's documented convention: skip the first 40s while the
                    # cold cache fills (10s warm-up before the measured window starts, plus 30s grace)


def load(d):
    try:
        r = pd.read_csv(f"{d}/requests.csv")
        m = json.load(open(f"{d}/final_metrics.json"))
    except Exception:
        return None
    if len(r) < 50:
        return None
    r["ts_ms"] = pd.to_numeric(r["issued_ts_ms"], errors="coerce")
    t0 = r.ts_ms.min()
    r["t"] = (r.ts_ms - t0) / 1000
    for col in ("hit", "stale", "wrong_entity", "status"):
        r[col] = pd.to_numeric(r[col], errors="coerce")
    tel = None
    tel_path = f"{d}/telemetry.csv"
    if os.path.exists(tel_path):
        try:
            tel = pd.read_csv(tel_path)
            tel["t"] = pd.to_numeric(tel["t_s"], errors="coerce")
        except Exception:
            tel = None
    return r, m, tel


def metrics(r, m, a, b):
    """Every metric name here is what analysis/extended.py's HEAD list and paper/render.py's
    'cell' lookups expect verbatim -- do not rename without updating both."""
    w = r[(r.t >= a) & (r.t < b)]
    rd = w[w.kind == "read"]; wr = w[w.kind == "write"]
    ok = rd[rd.status == 200]
    hits = ok[ok.hit == 1]; miss = ok[ok.hit == 0]
    ok = ok.assign(stale_only=(ok.stale == 1) & (ok.wrong_entity == 0), incorrect=(ok.stale == 1) | (ok.wrong_entity == 1))
    dur = b - a
    n_ok = max(1, len(ok))
    cost_usd = m.get("cost_usd", np.nan)
    llm_calls = m.get("llm_calls", np.nan)
    # cost_usd/llm_calls are cumulative FULL-RUN totals from final_metrics.json (no per-request
    # token/call log exists to window them precisely -- an external review caught that dividing a
    # full-run numerator by the WINDOWED (steady-state, a..b) n_ok above systematically inflated
    # every calls/cost-per-1k-reads figure, since the numerator's window (the whole run) is wider
    # than the denominator's (a..b). The fix without new instrumentation is to make the denominator
    # match the numerator's actual scope: count successful reads over the FULL run, same as the
    # gateway's own counters do, for these two fields only -- every other metric here already uses
    # the correctly-windowed n_ok because its own numerator (e.g. ok.stale_only.sum()) is computed
    # from the same windowed slice.
    ok_full = r[(r.kind == "read") & (r.status == 200)]
    n_ok_full = max(1, len(ok_full))
    return {
        "reads": len(rd), "read_rps": len(rd) / dur, "write_rps": len(wr) / dur, "errors": int((rd.status != 200).sum()),
        "hit_rate": hits.shape[0] / n_ok,
        "stale_reads": int(ok.stale_only.sum()),
        "stale_per_1k_reads": 1000 * ok.stale_only.sum() / n_ok,
        "wrong_entity_per_hit_pct": 100 * ok.wrong_entity.sum() / max(1, len(hits)),
        "incorrect_per_1k_reads": 1000 * ok.incorrect.sum() / n_ok,
        "correct_hit_rate": (hits.shape[0] - ok.incorrect.sum()) / n_ok,
        "read_p50": ok.latency_ms.median(), "read_p95": ok.latency_ms.quantile(.95), "read_p99": ok.latency_ms.quantile(.99),
        "read_mean": ok.latency_ms.mean(),
        "hit_p50": hits.latency_ms.median() if len(hits) else np.nan,
        "miss_p50": miss.latency_ms.median() if len(miss) else np.nan,
        "write_p50": wr.latency_ms.median() if len(wr) else np.nan,
        "write_p99": wr.latency_ms.quantile(.99) if len(wr) else np.nan,
        # cost/tokens/llm_calls are the gateway's own end-of-run aggregate (final_metrics.json),
        # not derivable from requests.csv (no per-request token counts logged by the load generator);
        # normalised per 1,000 reads over the same window so cells at different read shares compare
        "llm_calls_per_1k_reads": 1000 * llm_calls / n_ok_full if llm_calls == llm_calls else np.nan,
        "cost_per_1k_reads_usd": 1000 * cost_usd / n_ok_full if cost_usd == cost_usd else np.nan,
        "cost_usd": cost_usd, "llm_calls": llm_calls,
        "prompt_tokens": m.get("prompt_tokens", np.nan), "completion_tokens": m.get("completion_tokens", np.nan),
        "cache_entries_mean": m.get("cache_entries", np.nan),
        "invalidated_per_write": (pd.to_numeric(pd.Series([m.get("invalidated_entries", np.nan)]), errors="coerce").iloc[0]
                                  / max(1, m.get("writes", 1)) if m.get("writes") else np.nan),
    }


def main():
    rows = []; series = {}
    # every (mode[__family], read_share, tau) cell that exists on disk
    for d in sorted(glob.glob(f"{ROOT}/*/r*_t*/rep[0-9]*")):
        parts = d.split("/")
        subdir, cfg, rep = parts[-3], parts[-2], parts[-1]
        try:
            rs = float(cfg.split("_")[0][1:]); tau = float(cfg.split("_t")[1])
        except (IndexError, ValueError):
            continue
        lr = load(d)
        if lr is None:
            print("skip", d); continue
        r, m, tel = lr
        row = {"dir": subdir, "read_share": rs, "tau": tau, "rep": rep}
        row.update({f"all_{k}": v for k, v in metrics(r, m, 0, DUR).items()})
        row.update({f"st_{k}": v for k, v in metrics(r, m, *STEADY).items()})
        rows.append(row)
        series.setdefault((subdir, rs, tau), []).append((r, tel))
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/per_run_metrics.csv", index=False)
    num = df.drop(columns=["rep"]).groupby(["dir", "read_share", "tau"]).agg(["mean", "std", "count"])
    num.columns = ["_".join(c) for c in num.columns]
    num.reset_index().to_csv(f"{OUT}/summary_metrics.csv", index=False)

    main_df = df[(df.tau == 0.70) & (df.dir.isin(MODES))]
    cols = ["st_hit_rate", "st_correct_hit_rate", "st_stale_per_1k_reads", "st_wrong_entity_per_hit_pct",
            "st_incorrect_per_1k_reads", "st_read_p50", "st_read_p99", "st_hit_p50", "st_miss_p50",
            "all_cost_per_1k_reads_usd", "all_llm_calls_per_1k_reads"]
    with open(f"{OUT}/summary_table.md", "w") as f:
        for rs in sorted(main_df.read_share.unique()):
            f.write(f"\n### read share {rs}\n\n| mode | n | " + " | ".join(c.replace("st_", "").replace("all_", "") for c in cols) + " |\n|" + "---|" * (len(cols) + 2) + "\n")
            for mode in MODES:
                g = main_df[(main_df["dir"] == mode) & (main_df.read_share == rs)]
                if len(g) == 0:
                    continue
                vals = []
                for c in cols:
                    mean, std = g[c].mean(), g[c].std()
                    std = 0 if np.isnan(std) else std
                    vals.append(f"{mean:.3f} ± {std:.3f}" if abs(mean) < 10 else f"{mean:.1f} ± {std:.1f}")
                f.write(f"| {mode} | {len(g)} | " + " | ".join(vals) + " |\n")
    print(open(f"{OUT}/summary_table.md").read())

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.color": "#e6e6e3", "grid.linewidth": 0.6, "axes.edgecolor": "#9a9a97",
                         "figure.dpi": 200, "savefig.dpi": 300, "legend.frameon": False})

    # Fig A: read latency CDF per mode at read_share=0.75, tau=0.70, steady window, pooled reps
    fig, ax = plt.subplots(figsize=(5.5, 3.6))
    for mode in MODES:
        lat = []
        for r, _tel in series.get((mode, 0.75, 0.70), []):
            w = r[(r.kind == "read") & (r.status == 200) & (r.t >= STEADY[0]) & (r.t < STEADY[1])]
            lat.append(w.latency_ms.dropna().values)
        if not lat or not any(len(x) for x in lat):
            continue
        lat = np.sort(np.concatenate(lat))
        ax.plot(lat, np.linspace(0, 1, len(lat)), color=COLORS[mode], lw=1.4, label=LABELS[mode])
    ax.set_xscale("log"); ax.set_xlabel("read latency (ms)"); ax.set_ylabel("CDF of reads")
    ax.legend(fontsize=7, loc="upper left")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_latency_cdf.png"); plt.close(fig)

    # Fig B: hit rate vs correctness (stale/wrong-entity share of hits) and cost, one bar chart per metric, rs=0.75
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.2))
    bar_metrics = [("st_hit_rate", "Cache hit rate"), ("st_incorrect_per_1k_reads", "Incorrect reads per 1,000 reads"),
                   ("all_cost_per_1k_reads_usd", "LLM cost per 1,000 reads (USD)")]
    x = np.arange(len(MODES))
    sub = main_df[main_df.read_share == 0.75]
    for ax, (col, lab) in zip(axes, bar_metrics):
        means = [sub[sub["dir"] == mode][col].mean() for mode in MODES]
        stds = [sub[sub["dir"] == mode][col].std() for mode in MODES]
        stds = [0 if np.isnan(s) else s for s in stds]
        ax.bar(x, means, yerr=stds, color=[COLORS[m] for m in MODES], capsize=3)
        ax.set_xticks(x); ax.set_xticklabels(MODES, rotation=45, ha="right", fontsize=7)
        ax.set_title(lab, fontsize=9, loc="left")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_main_comparison.png"); plt.close(fig)

    # Fig C: hit rate / stale-per-1k / cost vs read share, one line per mode
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))
    for ax, (col, lab) in zip(axes, [("st_hit_rate", "Cache hit rate"), ("st_incorrect_per_1k_reads", "Incorrect reads per 1,000 reads"),
                                     ("all_cost_per_1k_reads_usd", "LLM cost per 1,000 reads (USD)")]):
        for mode in MODES:
            g = main_df[main_df["dir"] == mode].groupby("read_share")[col].agg(["mean", "std"]).sort_index()
            if len(g) == 0:
                continue
            x_ = 100 * (1 - g.index.values)
            ax.errorbar(x_, g["mean"], yerr=g["std"].fillna(0), color=COLORS[mode], marker="o", ms=4, lw=1.4, capsize=2, label=LABELS[mode])
        ax.set_xlabel("write share of traffic (%)"); ax.set_title(lab, fontsize=9, loc="left"); ax.set_ylim(bottom=0)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=4, fontsize=7, bbox_to_anchor=(0.5, -0.05))
    fig.tight_layout(rect=(0, 0.08, 1, 1)); fig.savefig(f"{OUT}/fig_vs_write_share.png", bbox_inches="tight"); plt.close(fig)

    # Fig D: cold-start timelines, one run per policy at read_share=0.75, tau=0.70
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))
    for mode in MODES:
        runs = series.get((mode, 0.75, 0.70), [])
        if not runs:
            continue
        r, tel = runs[0]
        rd = r[(r.kind == "read") & (r.status == 200)].assign(s=lambda x: x.t.astype(int))
        hr = rd.groupby("s").hit.mean().reindex(range(118)).rolling(5, min_periods=1).mean()
        axes[0].plot(hr.index, hr.values, color=COLORS[mode], lw=1.3, label=LABELS[mode])
        if tel is not None and "cache_entries" in tel.columns:
            ce = pd.to_numeric(tel.cache_entries, errors="coerce")
            axes[1].plot(tel.t, ce, color=COLORS[mode], lw=1.2)
        st = rd.assign(so=(rd.stale == 1) & (rd.wrong_entity == 0)).groupby("s").so.sum().reindex(range(118)).fillna(0)
        axes[2].plot(st.index, st.values, color=COLORS[mode], lw=1.2)
    axes[0].set_ylabel("hit rate (5 s rolling)"); axes[0].set_xlabel("time (s)"); axes[0].legend(fontsize=6.5, loc="center right")
    axes[1].set_ylabel("cache entries"); axes[1].set_xlabel("time (s)")
    axes[2].set_ylabel("stale reads per second"); axes[2].set_xlabel("time (s)")
    for ax in axes: ax.set_xlim(0, 118)
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_timelines.png"); plt.close(fig)

    # Fig E: threshold sweep, read_share=0.75, ttl and graph-tx
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.0))
    sw = df[(df.read_share == 0.75)]
    for mode, label_key in [("ttl", "ttl"), ("graph-tx", "graph-tx")]:
        g = sw[sw["dir"] == mode].groupby("tau")[["st_hit_rate", "st_wrong_entity_per_hit_pct", "st_read_p50"]].agg(["mean", "std"]).sort_index()
        if len(g) == 0:
            continue
        for ax, col in zip(axes, ["st_hit_rate", "st_wrong_entity_per_hit_pct", "st_read_p50"]):
            ax.errorbar(g.index, g[(col, "mean")], yerr=g[(col, "std")].fillna(0), color=COLORS[mode], marker="o", ms=4, lw=1.4, capsize=2, label=LABELS[mode])
    for ax, lab in zip(axes, ["Hit rate", "Wrong-entity hits (% of hits)", "Median read latency (ms)"]):
        ax.set_xlabel("similarity threshold τ"); ax.set_title(lab, fontsize=9, loc="left"); ax.set_ylim(bottom=0)
    axes[0].legend(fontsize=7)
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_tau_sweep.png"); plt.close(fig)
    print("figures written")


if __name__ == "__main__":
    main()
