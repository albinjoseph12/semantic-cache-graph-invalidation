"""
Baseline gateway built on GPTCache (the library, not a reimplementation of it).

The purpose of this service is to answer one question: what does a widely used semantic-cache library
do to this workload, under the same offered load, the same embedding, the same emulated LLM and the
same server-side correctness instrumentation as the proposed system?

What is taken from GPTCache
  * `gptcache.Cache` with `get_data_manager(CacheBase("sqlite"), VectorBase("faiss", ...))` — the
    library's own scalar store, vector index and eviction;
  * `SearchDistanceEvaluation` — the library's own similarity evaluation and threshold semantics;
  * `data_manager.search / save / get_scalar_data` — the library's own read and write paths.

What is deliberately identical to the proposed system
  * the embedding function (EMBEDDING selects the same families as main.py);
  * the emulated LLM (same time-to-first-token distribution, same tokens-per-second, same tokenizer,
    so token accounting and cost are directly comparable);
  * the relational read that grounds an answer, and the version stamping that lets the server decide
    whether a hit was stale;
  * the HTTP surface (`POST /api/query`, `POST /api/product/{id}`, `GET /metrics`), so the same Locust
    workload and the same monitor drive both systems without modification.

What GPTCache does not have, and is the point of the comparison
  * any notion of which relational rows an answer depends on. A write to a product updates PostgreSQL
    and increments its version; GPTCache is not told, and cannot be told, that the cached answers
    derived from that row are now wrong. `GPTCACHE_TTL` optionally expires entries by age, which is
    the only invalidation the library offers for this situation.
"""
import asyncio, json, os, random, time
from contextlib import asynccontextmanager

import asyncpg
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from queries import EntityLinker, canonical
import main as REF                                   # context building, answer composition, tokenizer

TAU = float(os.environ.get("TAU", "0.70"))
GPTCACHE_TTL = float(os.environ.get("GPTCACHE_TTL", "0"))     # 0 = no expiry, the library default
EMBEDDING = os.environ.get("EMBEDDING", "wl256")
DSN = os.environ.get("SC_PG_URL", "postgresql://acb:acb@localhost:5432/semcache")
INDEX_DIR = os.environ.get("GPTCACHE_DIR", "/tmp/gptcache_run")

state = {}
M = {k: 0 for k in ["reads", "hits", "misses", "stale", "wrong_entity", "writes", "invalidated_entries",
                    "flushes", "llm_calls", "prompt_tokens", "completion_tokens", "hit_latency_ms_sum",
                    "miss_latency_ms_sum", "write_latency_ms_sum", "hit_lookup_ms_sum", "expired",
                    "insert_rejected"]}
# GPTCache's data manager is not designed for concurrent mutation, so writes into it are serialised;
# lookups are not, so that the read path is not artificially slowed by the integration.
LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    import shutil
    from gptcache import Cache
    from gptcache.manager import get_data_manager, CacheBase, VectorBase
    from gptcache.similarity_evaluation.distance import SearchDistanceEvaluation
    from wordllama import WordLlama

    shutil.rmtree(INDEX_DIR, ignore_errors=True)
    os.makedirs(INDEX_DIR, exist_ok=True)
    wl = WordLlama.load(disable_download=True)
    REF.state["wl"] = wl
    REF.state["tokenizer"] = wl.tokenizer
    state["encoder"] = REF.build_encoder(EMBEDDING, wl)
    state["tokenizer"] = wl.tokenizer

    dm = get_data_manager(
        CacheBase("sqlite", sql_url=f"sqlite:///{INDEX_DIR}/gptcache.db"),
        VectorBase("faiss", dimension=REF.VEC_DIM, index_path=f"{INDEX_DIR}/faiss.index"),
        max_size=100000,
    )
    cache = Cache()
    cache.init(data_manager=dm, similarity_evaluation=SearchDistanceEvaluation())
    state["cache"] = cache
    state["dm"] = dm

    state["pool"] = await asyncpg.create_pool(DSN, min_size=8, max_size=32)
    async with state["pool"].acquire() as c:
        prods = await c.fetch("SELECT id, name FROM products")
        cats = await c.fetch("SELECT DISTINCT category FROM products")
    state["linker"] = EntityLinker([(r["id"], r["name"]) for r in prods], [r["category"] for r in cats])
    print(f"[gptcache] embedding={EMBEDDING} tau={TAU} ttl={GPTCACHE_TTL} products={len(prods)}", flush=True)
    yield
    await state["pool"].close()


app = FastAPI(lifespan=lifespan)


def embed(text):
    return state["encoder"](text)


def tok(text):
    return len(state["tokenizer"].encode(text, add_special_tokens=False).ids)


@app.post("/api/query")
async def query(req: Request):
    t0 = time.perf_counter()
    body = await req.json()
    q = body.get("q", "")
    keys = state["linker"].resolve(q)
    eset = canonical(keys)
    v = embed(q)
    pool = state["pool"]
    hit = None
    lookup_ms = 0.0

    async with pool.acquire() as conn:
        truth = await REF.truth_versions(conn, keys)     # sampled before the lookup, as in main.py
        tl = time.perf_counter()
        # searches run concurrently; only mutation is serialised (see LOCK)
        res = await asyncio.to_thread(state["dm"].search, v.tolist(), top_k=1)
        lookup_ms = (time.perf_counter() - tl) * 1000
        # GPTCache reports squared L2 distance over the vectors it was given; for unit vectors
        # cos = 1 - d/2, so the library's threshold semantics and ours coincide exactly.
        if res:
            dist, vid = res[0][0], res[0][1]
            sim = 1.0 - float(dist) / 2.0
            if sim >= TAU:
                sd = await asyncio.to_thread(state["dm"].get_scalar_data, (dist, vid))
                if sd is not None:
                    payload = json.loads(sd.answers[0].answer)
                    if GPTCACHE_TTL > 0 and (time.time() - payload["stored_at"]) > GPTCACHE_TTL:
                        M["expired"] += 1
                    else:
                        hit = {"sim": sim, "payload": payload}
        if hit is None:
            ctx, versions, context_text = await REF.build_context(conn, keys)

    if hit is not None:
        ans = hit["payload"]
        stale = any(ans["versions"].get(k, -1) < truth.get(k, 0) for k in keys)
        wrong = ans["entity_set"] != eset
        M["reads"] += 1; M["hits"] += 1; M["stale"] += int(stale); M["wrong_entity"] += int(wrong)
        lat = (time.perf_counter() - t0) * 1000
        M["hit_latency_ms_sum"] += lat; M["hit_lookup_ms_sum"] += lookup_ms
        return JSONResponse({"answer": ans["text"], "hit": True, "stale": stale, "wrong_entity": wrong,
                             "sim": round(hit["sim"], 4), "entities": keys, "cache_entities": ans["entity_set"],
                             "answer_versions": ans["versions"], "truth_versions": truth,
                             "prompt_tokens": 0, "completion_tokens": 0,
                             "lookup_ms": round(lookup_ms, 2), "latency_ms": round(lat, 2)})

    prompt = f"{REF.SYSTEM_PROMPT}\n\nContext:\n{context_text}\n\nCustomer: {q}\nAssistant:"
    text = REF.compose_answer(q, ctx)
    p_tok, c_tok = tok(prompt), tok(text)
    await REF.emulated_llm(p_tok, c_tok)
    payload = {"text": text, "versions": versions, "entity_set": eset, "stored_at": time.time()}
    async with LOCK:
        await asyncio.to_thread(state["dm"].save, q, json.dumps(payload), v.tolist())
    M["reads"] += 1; M["misses"] += 1; M["llm_calls"] += 1
    M["prompt_tokens"] += p_tok; M["completion_tokens"] += c_tok
    lat = (time.perf_counter() - t0) * 1000
    M["miss_latency_ms_sum"] += lat
    return JSONResponse({"answer": text, "hit": False, "stale": False, "wrong_entity": False, "sim": None,
                         "entities": keys, "cache_id": None, "answer_versions": versions,
                         "truth_versions": versions, "prompt_tokens": p_tok, "completion_tokens": c_tok,
                         "lookup_ms": round(lookup_ms, 2), "latency_ms": round(lat, 2)})


@app.post("/api/product/{pid}")
async def update_product(pid: int, req: Request):
    """The write path. GPTCache is not informed: it has no interface through which the writer could
    say which cached answers this row supports."""
    t0 = time.perf_counter()
    body = await req.json()
    async with state["pool"].acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE products SET price_cents = coalesce($2, price_cents), stock = coalesce($3, stock), "
            "version = version + 1, updated_at = now() WHERE id = $1 "
            "RETURNING id, category, version, price_cents, stock", pid, body.get("price_cents"), body.get("stock"))
    if row is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    M["writes"] += 1
    lat = (time.perf_counter() - t0) * 1000
    M["write_latency_ms_sum"] += lat
    return JSONResponse({"id": row["id"], "version": row["version"], "invalidated": 0,
                         "latency_ms": round(lat, 2)})


@app.get("/metrics")
async def metrics():
    n = 0
    try:
        n = int(state["dm"].v._index.ntotal)
    except Exception:
        try:
            n = int(state["dm"].v.index.ntotal)
        except Exception:
            pass
    m = dict(M)
    m.update({"ts_ms": int(time.time() * 1000), "mode": "gptcache", "embedding": EMBEDDING, "tau": TAU,
              "cache_entries": n, "dep_edges": 0, "redis_used_memory": 0,
              "cost_usd": M["prompt_tokens"] / 1e6 * REF.PRICE_IN + M["completion_tokens"] / 1e6 * REF.PRICE_OUT})
    return JSONResponse(m)
