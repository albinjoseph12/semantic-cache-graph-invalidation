# Semantic-cache testbed — graph-based invalidation for LLM answers over transactional data

Reproducible testbed for *"LLM Semantic Caching with Graph-Based Invalidation in Transactional Microservices"*.

```
semcache-testbed/
├── docker-compose.yml        PostgreSQL 16 + pgvector, Redis 7, FastAPI gateway, CDC invalidator, Locust, monitor
├── run_benchmark.sh          native benchmark loop (what produced the paper's data)
├── app/main.py               gateway: /api/query (semantic read), /api/product/{id} (OLTP write), /metrics
├── app/queries.py            paraphrase templates + dictionary entity linker (shared with the load generator)
├── app/invalidator.py        asynchronous (CDC-style) graph invalidator for the graph-cdc mode
├── seed/seed_db.py           schema (products, semantic_cache with pgvector HNSW, cache_deps) + 5,000 products
├── load/locustfile.py        500 users, READ_SHARE semantic reads vs OLTP writes, per-request correctness log
├── telemetry/monitor.py      200 ms poller of gateway counters, cache size, Redis memory, PostgreSQL activity
└── analysis/analyze.py       tables + figures from the raw CSVs
```

Cache modes (`CACHE_MODE`): `none`, `ttl` (static semantic cache, TTL 60 s), `flush` (semantic cache, any write flushes the
key space), `graph-cdc` (dependency-graph invalidation by an asynchronous CDC consumer), `graph-tx` (proposed: entity-scoped
lookup, version-checked insertion under per-entity advisory locks, invalidation inside the writing transaction).

Embeddings: WordLlama `l2_supercat` 256-d (weights bundled in the wheel; no network needed). LLM: emulated
(TTFT ~ lognormal(500 ms), 50 tokens/s, answers grounded in the live rows; token cost at GPT-4o-mini list price).

## Revision (this version)

New experiments
* **GPTCache baseline** (`app/gptcache_gateway.py`, mode `gptcache`): the library itself — its `Cache`,
  its SQLite + FAISS data manager, its `SearchDistanceEvaluation` and its search/save paths — behind the
  same HTTP surface, the same embedding, the same emulated LLM and the same server-side staleness
  instrumentation as the proposed protocol. Results under `results/gptcache/`.
* **Embedding families** (`EMBEDDING=wl256|wl64|lsa256`): the distilled static model at 256 dimensions,
  the same weights truncated to 64 (Matryoshka prefix, renormalised and zero-padded, which leaves every
  cosine unchanged), and a lexical TF-IDF/SVD model fitted offline on the workload's own query
  distribution (`app/fit_lsa.py`, seed 20260906). Results under `results/<mode>__<family>/`.
* Additional repetitions of the headline cell so that every reported mean carries a confidence interval.

Harness fix in this revision
* `run_benchmark.sh` now frees the gateway port before starting (whichever application module the
  previous run used) and aborts the run if the gateway that answers `/metrics` does not report the
  mode being measured. Without this a gateway left over from a previous run served the next run's
  traffic; `analysis/validate_runs.py` detects that condition and every affected run was repeated.

Reproducing
    ./matrix.sh && ./matrix3.sh
    python3 analysis/analyze.py && python3 analysis/extended.py && python3 analysis/validate_runs.py
    python3 paper/render.py paper/paper_full.md.tmpl paper/paper_rendered.md

As with the companion paper, the manuscript is rendered from the measurements: every number in
`paper/paper_full.md.tmpl` is a placeholder resolved from the analysis output.

---

## Data availability

This repository holds the code, configuration, analysis scripts and figures. The complete raw measurements
(per-run logs and result files) are too large for GitHub and are archived separately on Zenodo with a DOI:
**DOI: to be added on deposit**. Download the archive and extract its `results/` directories into this folder to
re-run the analysis scripts exactly as used for the paper.

## How to cite

The paper is under review. Until it is published, please cite this repository using `CITATION.cff`
(GitHub's "Cite this repository" button).

## License

Code: MIT (see `LICENSE`). Data on Zenodo: CC BY 4.0.
