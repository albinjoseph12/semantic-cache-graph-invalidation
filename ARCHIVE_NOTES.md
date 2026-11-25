# Semantic-cache replication package — archive notes (rebuilt 2026-11-18)
- `app/main.py` is the code that produced all 184 archived runs (it contains the category read-snapshot race described in Section III-D of the paper).
- `patches/category-snapshot.patch` is the one-statement correction; `patches/postgres_snapshot_test.py` + `postgres_regression_result.txt` are the PostgreSQL regression and its result.
- `local_rerun_2026-11-18/` is the reduced local re-run (read `RESULTS.md` first): the archived and fixed read paths, a widened-window variant, the content auditor and the per-run data.
- `paper/` holds the final manuscript source (template + renderer), PDF and DOCX; `python3 paper/render.py paper/paper_full.md.tmpl out.md` regenerates the text from `results/`.
- `results/` is unchanged from the first archive.

Update (2026-11-19, submission version for Software: Practice and Experience): `paper/` holds the final manuscript. The PostgreSQL regression helper is now self-contained (run: PG_DSN=... python3 patches/postgres_snapshot_test.py .). local_rerun_2026-11-18/ adds Arm 3: the paper's own open-loop graph-tx cells with the archived and the single-snapshot read path (RESULTS.md).

Update (2026-11-20): `results/full_rerun_2026-11-19/` holds the analysis outputs of the full local re-run of all 178 emulated-LLM runs with the single-snapshot read path (paper Section V-K): `RESULTS.md` and `RERUN_README.md` first, then `cells.csv` (campaign vs re-run per cell and metric), `reps.csv`, `independent_check.json`, `answer_check_all.jsonl` (answer-text check per run) and `summary.json` (the values the manuscript cites; `paper/render.py` source `rerun`). The re-run's per-request logs, answer texts and tools are in the separate archive `semcache_full_rerun_2026-11-19.tar.gz`. `app/main.py` here is still the archived code; the re-run used it with `patches/category-snapshot.patch` applied.
