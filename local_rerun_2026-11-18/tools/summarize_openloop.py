"""Summarise the local open-loop re-run (archived vs fixed read path, graph-tx, 20 req/s, tau 0.70) with the testbed's own
analysis.analyze.load/metrics (steady window 40-120 s), plus the content audit and the gateway's rejected inserts.
Usage: python3 summarize_openloop.py  -> results/openloop_summary.csv and results/openloop_summary.md"""
import glob, json, os, sys
import pandas as pd
HERE = os.path.dirname(os.path.abspath(__file__)); R = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(R, "..", "corrected_semcache-testbed", "analysis"))
from analyze import load, metrics, STEADY
rows = []
for d in sorted(glob.glob(os.path.join(R, "results", "openloop", "*", "r*_t0.70", "rep*"))):
    v = d.split(os.sep)[-3]; rs = d.split(os.sep)[-2][1:5]; rep = d.split(os.sep)[-1]
    lr = load(d)
    if lr is None: continue
    r, m = lr[0], lr[1]
    x = metrics(r, m, *STEADY)
    a = json.load(open(os.path.join(d, "audit.json"))) if os.path.exists(os.path.join(d, "audit.json")) else {}
    fm = json.load(open(os.path.join(d, "final_metrics.json")))
    gl = open(os.path.join(d, "gen.log")).read().strip().splitlines()[-1]
    timed_out = json.loads(gl).get("timed_out", 0) if gl.startswith("{") else -1
    rows.append(dict(variant=v, read_share=float(rs), rep=rep, hit_rate=x["hit_rate"], correct_hit_rate=x["correct_hit_rate"],
                     stale_per_1k_reads=x["stale_per_1k_reads"], wrong_entity_per_hit_pct=x["wrong_entity_per_hit_pct"],
                     read_p50=x["read_p50"], read_mean=x["read_mean"], llm_calls_per_1k_reads=x["llm_calls_per_1k_reads"],
                     write_rps=x["write_rps"], insert_rejected=fm.get("insert_rejected"), audited=a.get("distinct_category_entries_audited"),
                     content_stale=a.get("content_stale_entries"), content_stale_current_stamp=a.get("content_stale_with_current_stamp"), timed_out=timed_out))
df = pd.DataFrame(rows); df.to_csv(os.path.join(R, "results", "openloop_summary.csv"), index=False)
bad = df[df.timed_out != 0]          # disrupted runs (client timeouts) are reported separately, not averaged in
ok = df[df.timed_out == 0]
g = ok.groupby(["read_share", "variant"]).agg(n=("rep", "count"), hit_rate=("hit_rate", "mean"), hit_sd=("hit_rate", "std"),
        stale=("stale_per_1k_reads", "mean"), wrong=("wrong_entity_per_hit_pct", "mean"), p50=("read_p50", "mean"), rmean=("read_mean", "mean"),
        llm=("llm_calls_per_1k_reads", "mean"), wps=("write_rps", "mean"), rej=("insert_rejected", "mean"),
        audited=("audited", "sum"), cstale=("content_stale", "sum"), cstale_cur=("content_stale_current_stamp", "sum")).reset_index()
lines = ["| Writes | Read path | n | Hit rate | Stale /1k (stamp) | Wrong-entity (%) | Read p50 (ms) | Read mean (ms) | LLM /1k | Category entries audited | Content-stale |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
for _, x in g.sort_values(["read_share", "variant"], ascending=[False, True]).iterrows():
    lines.append(f"| {round(100*(1-x.read_share))}% | {x.variant} | {x.n} | {x.hit_rate:.3f} ± {x.hit_sd:.3f} | {x.stale:.1f} | {x.wrong:.1f} | {x.p50:.0f} | {x.rmean:.0f} | {x.llm:.0f} | {int(x.audited)} | {int(x.cstale)} ({int(x.cstale_cur)} with current stamp) |")
lines.append("")
for _, x in bad.iterrows():
    lines.append(f"Excluded from the means (disrupted run): {x.variant}, {round(100*(1-x.read_share))}% writes, {x.rep}: {x.timed_out} of 2,400 requests timed out (hit rate {x.hit_rate:.3f}); content-stale entries {int(x.content_stale)} of {int(x.audited)} audited.")
open(os.path.join(R, "results", "openloop_summary.md"), "w").write("\n".join(lines) + "\n")
print("\n".join(lines))
