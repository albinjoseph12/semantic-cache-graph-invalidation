"""
Gateway microservice for the semantic-cache study (FastAPI + asyncpg + Redis + pgvector + WordLlama embeddings).

CACHE_MODE selects the caching policy:
  none      every read goes to the (emulated) LLM.
  ttl       static semantic cache: pgvector nearest neighbour over ALL entries, cosine >= TAU, entry valid for TTL_S seconds.
  flush     semantic cache with key-space flush: any entity write deletes the whole cache.
  graph-cdc dependency-graph invalidation performed asynchronously by a separate invalidator process that consumes
            PostgreSQL NOTIFY events (CDC-style); lookup is entity-scoped.
  graph-tx  dependency-graph invalidation executed inside the writing transaction; lookup is entity-scoped (proposed).
  scoped-ttl ablation: entity-scoped lookup (as in the graph modes) but TTL expiry only, no dependency edges / invalidation.

Read path (POST /api/query):
  1. embed the query (WordLlama l2_supercat, 256-d) and resolve entity references (dictionary linker).
  2. cache lookup: pgvector nearest neighbour; graph modes restrict the search to entries whose entity_set equals the
     resolved set (graph-scoped similarity); ttl/flush search the whole index. Hit if cosine >= TAU (and, for ttl, not expired).
  3. on a hit the answer payload is served from Redis (answer:<cache_id>), falling back to the pgvector row.
  4. on a miss the context rows are read from PostgreSQL, the LLM call is emulated (TTFT + tokens / TPS), the entry is
     stored (pgvector row + Redis payload) and dependency edges are recorded (cache_deps rows + Redis sets).
  Every response is stamped with the entity versions it reflects AND the versions currently committed in PostgreSQL,
  so that a stale read (answer_version < committed_version) or a wrong-entity hit is detected server-side.

Write path (POST /api/product/{id}): UPDATE products ... version = version + 1; then invalidate according to CACHE_MODE.
"""
import asyncio, json, os, random, time
from contextlib import asynccontextmanager

import asyncpg
import numpy as np
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from queries import EntityLinker, canonical

MODE = os.environ.get("CACHE_MODE", "graph-tx")
# Embedding family under test (Section VI-H). The pgvector column is 256-wide; a shorter embedding is
# zero-padded, which leaves cosine similarity exactly unchanged, so one schema serves every family.
EMBEDDING = os.environ.get("EMBEDDING", "wl256")
VEC_DIM = 256
TAU = float(os.environ.get("TAU", "0.70"))
TTL_S = float(os.environ.get("TTL_S", "60"))
# Bounded-cache behaviour (Phase 3): 0 (default) leaves the cache unbounded, preserving every
# existing experiment's configuration unchanged. A positive value caps semantic_cache at that many
# rows, evicting the oldest (by created_at) beyond the cap on every insert -- the simplest
# well-defined eviction policy, and the one GPTCache's own data manager also approximates.
CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "0"))
DSN = os.environ.get("SC_PG_URL", "postgresql://acb:acb@localhost:5432/semcache")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Emulated LLM: time-to-first-token ~ lognormal around LLM_TTFT_MS, then completion tokens at LLM_TPS tokens/s.
LLM_TTFT_MS = float(os.environ.get("LLM_TTFT_MS", "500"))
LLM_TPS = float(os.environ.get("LLM_TPS", "50"))
LLM_JITTER = float(os.environ.get("LLM_JITTER", "0.25"))
PRICE_IN, PRICE_OUT = 0.15, 0.60          # USD per 1M tokens (GPT-4o mini list price, used only for cost accounting)
# LLM_BACKEND=real (Section VI-?, external-validity check on the emulation): call a real model
# instead of emulating latency/tokens from a deterministic hand-composed answer. Real prompt/
# completion token counts and cost come from the API's own usage report, not the local tokenizer
# estimate used for the emulated path's PRICE_IN/PRICE_OUT accounting.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "emulated")
REAL_LLM_MODEL = os.environ.get("REAL_LLM_MODEL", "claude-haiku-4-5-20251001")
REAL_PRICE_IN, REAL_PRICE_OUT = 1.00, 5.00  # USD per 1M tokens, Claude Haiku 4.5 list price
SYSTEM_PROMPT = ("You are a helpful retail assistant for an online store. Answer the customer's question using only the "
                 "product records provided in the context. Quote prices in US dollars, state stock levels exactly as given, "
                 "and be concise. If the context does not contain the information, say so.")

state = {}
M = {k: 0 for k in ["reads", "hits", "misses", "stale", "wrong_entity", "writes", "invalidated_entries", "flushes", "llm_calls",
                    "prompt_tokens", "completion_tokens", "hit_latency_ms_sum", "miss_latency_ms_sum", "write_latency_ms_sum",
                    "hit_lookup_ms_sum", "expired", "insert_rejected"]}


def tok(text):
    return len(state["tokenizer"].encode(text, add_special_tokens=False).ids)


@asynccontextmanager
async def lifespan(app):
    from wordllama import WordLlama
    # the archive's disable_download=True assumed a wordllama version whose wheel exposed its
    # bundled weights directly to resolve_file(); the pip-installed version here (0.4.0.post1)
    # instead needs one cache-populating download from HuggingFace before disable_download=True
    # would find anything (confirmed: the weights ARE present under wordllama/weights/, just not
    # where resolve_file() looks without a prior fetch). Since this VM has real internet access,
    # download once (cached for every subsequent load) rather than forcing an offline mode this
    # dependency version doesn't actually support.
    wl = WordLlama.load()
    state["wl"] = wl
    state["tokenizer"] = wl.tokenizer
    state["encoder"] = build_encoder(EMBEDDING, wl)
    state["pool"] = await asyncpg.create_pool(DSN, min_size=8, max_size=32)
    state["redis"] = aioredis.from_url(REDIS_URL, decode_responses=False)
    async with state["pool"].acquire() as c:
        prods = await c.fetch("SELECT id, name FROM products")
        cats = await c.fetch("SELECT DISTINCT category FROM products")
    state["linker"] = EntityLinker([(r["id"], r["name"]) for r in prods], [r["category"] for r in cats])
    if LLM_BACKEND == "real":
        import anthropic
        state["anthropic"] = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from the environment
    print(f"[gateway] mode={MODE} embedding={EMBEDDING} tau={TAU} ttl={TTL_S}s products={len(prods)} llm_backend={LLM_BACKEND}", flush=True)
    yield
    await state["pool"].close()


app = FastAPI(lifespan=lifespan)


TRANSFORMER_MODELS = {
    "st-minilm": "sentence-transformers/all-MiniLM-L6-v2",   # 384-d, contemporary sentence-transformer
    "st-bge": "BAAI/bge-small-en-v1.5",                       # 384-d, contemporary sentence-transformer
}


def _random_projection(seed, in_dim, out_dim):
    """A fixed Gaussian random projection in_dim -> out_dim (Johnson-Lindenstrauss: pairwise cosine
    similarity is approximately preserved). Used only for the contemporary transformer encoders,
    which are not Matryoshka-trained -- unlike wlNN's prefix truncation, naively truncating an
    ordinary transformer's dimensions would not be a valid dimensionality reduction, so a projection
    fit once (fixed seed, no data) is used instead, the same role fit_lsa.py's offline SVD plays for
    the lexical family."""
    rng = np.random.RandomState(seed)
    m = rng.normal(size=(in_dim, out_dim)).astype(np.float32)
    return m / np.linalg.norm(m, axis=0, keepdims=True)


def build_encoder(name, wl):
    """Return a function text -> unit vector of length VEC_DIM.

    wl256      WordLlama l2_supercat, the full 256 dimensions (the configuration of the main experiments)
    wl64       the same weights truncated to the first 64 dimensions. The model is trained with a
               Matryoshka objective, so a prefix is itself a usable embedding; the vector is renormalised
               and zero-padded back to 256, which does not change any cosine.
    lsa256     a lexical family: TF-IDF over word 1-2 grams reduced to 256 dimensions by truncated SVD,
               fitted offline on a sample of the workload's own query distribution (fit_lsa.py) and loaded
               here. It shares no training data, no vocabulary treatment and no architecture with WordLlama.
    st-minilm  sentence-transformers/all-MiniLM-L6-v2 (384-d), a contemporary transformer sentence
               encoder, reduced to 256-d by a fixed random (Johnson-Lindenstrauss) projection -- not
               truncation, since this model is not Matryoshka-trained.
    st-bge     BAAI/bge-small-en-v1.5 (384-d), same treatment as st-minilm.
    """
    if name.startswith("wl"):
        k = int(name[2:]) if name[2:] else VEC_DIM
        def enc(text):
            v = wl.embed([text])[0].astype(np.float32)[:k]
            n = np.linalg.norm(v)
            v = v / n if n > 0 else v
            if k < VEC_DIM:
                v = np.concatenate([v, np.zeros(VEC_DIM - k, dtype=np.float32)])
            return v
        return enc
    if name == "lsa256":
        import joblib
        pipe = joblib.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lsa_model.joblib"))
        def enc(text):
            v = pipe.transform([text])[0].astype(np.float32)
            n = np.linalg.norm(v)
            v = v / n if n > 0 else v
            if v.shape[0] < VEC_DIM:
                v = np.concatenate([v, np.zeros(VEC_DIM - v.shape[0], dtype=np.float32)])
            return v[:VEC_DIM]
        return enc
    if name in TRANSFORMER_MODELS:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(TRANSFORMER_MODELS[name])
        native_dim = model.get_sentence_embedding_dimension()
        proj = _random_projection(seed=20260906, in_dim=native_dim, out_dim=VEC_DIM)
        def enc(text):
            v = model.encode(text, normalize_embeddings=False).astype(np.float32)
            v = v @ proj
            n = np.linalg.norm(v)
            return v / n if n > 0 else v
        return enc
    raise SystemExit(f"unknown EMBEDDING {name}")


def embed(text):
    return state["encoder"](text)


def vec_literal(v):
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


# ------------------------------------------------------------------------------------------------ context + emulated LLM
async def build_context(conn, keys):
    """Fetch the relational rows the answer depends on; returns (rows_by_key, versions, context_text)."""
    ctx, versions = {}, {}
    for k in keys:
        kind, val = k.split(":", 1)
        if kind == "product":
            r = await conn.fetchrow("SELECT id, name, category, brand, price_cents, stock, description, version FROM products WHERE id = $1", int(val))
            if r:
                ctx[k] = dict(r); versions[k] = r["version"]
        else:
            rows = await conn.fetch("SELECT id, name, price_cents, stock, version FROM products WHERE category = $1 ORDER BY price_cents ASC LIMIT 8", val)
            ctx[k] = [dict(r) for r in rows]
            await asyncio.sleep(0.005)  # LOCAL-RERUN ONLY: widens the gap between the rows read and the epoch read to 5 ms
            # a category answer depends on every product in the category, including which products
            # are members -- MAX(version) over member rows is blind to membership changes (a
            # deletion can lower the observed max; an insertion below the current max changes
            # nothing observable), so the category's own epoch counter is the freshness stamp
            # instead (bumped by every insert/update/delete that touches the category; see
            # seed/seed_db.py's products_bump_category_epoch trigger)
            epoch = await conn.fetchval("SELECT coalesce(epoch, 0) FROM categories WHERE category = $1", val)
            versions[k] = epoch or 0
    lines = []
    for k, v in ctx.items():
        if isinstance(v, list):
            lines.append(f"Category {k.split(':',1)[1]}: " + "; ".join(f"{p['name']} (id {p['id']}) ${p['price_cents']/100:.2f}, {p['stock']} in stock" for p in v))
        else:
            lines.append(f"Product {v['id']}: {v['name']}, category {v['category']}, brand {v['brand']}, price ${v['price_cents']/100:.2f}, stock {v['stock']}. {v['description']}")
    return ctx, versions, "\n".join(lines)


def compose_answer(query, ctx):
    """Deterministic, context-grounded answer text standing in for the LLM completion."""
    parts = []
    for k, v in ctx.items():
        if isinstance(v, list):
            if not v:
                parts.append(f"I could not find any items in the {k.split(':',1)[1]} category.")
            else:
                cheapest = v[0]
                instock = [p for p in v if p["stock"] > 0]
                parts.append(f"In {k.split(':',1)[1]}, the least expensive option is {cheapest['name']} at ${cheapest['price_cents']/100:.2f}"
                             + (f" ({cheapest['stock']} in stock)." if cheapest["stock"] > 0 else ", although it is currently out of stock.")
                             + (f" Other in-stock choices include " + ", ".join(f"{p['name']} (${p['price_cents']/100:.2f})" for p in instock[1:4]) + "." if len(instock) > 1 else ""))
        else:
            avail = f"{v['stock']} units in stock" if v["stock"] > 0 else "currently out of stock"
            parts.append(f"{v['name']} (product {v['id']}) is priced at ${v['price_cents']/100:.2f} and is {avail}. {v['description']}")
    if len([v for v in ctx.values() if not isinstance(v, list)]) == 2:
        a, b = [v for v in ctx.values() if not isinstance(v, list)]
        cheaper = a if a["price_cents"] <= b["price_cents"] else b
        parts.append(f"Overall, {cheaper['name']} is the cheaper of the two by ${abs(a['price_cents']-b['price_cents'])/100:.2f}.")
    if not parts:
        parts.append("I'm sorry, I could not identify the product you are asking about. Could you give me its name or product number?")
    return " ".join(parts)


async def emulated_llm(prompt_tokens, completion_tokens):
    ttft = random.lognormvariate(np.log(LLM_TTFT_MS), LLM_JITTER)
    gen = completion_tokens / LLM_TPS * 1000 * random.lognormvariate(0, LLM_JITTER / 2)
    await asyncio.sleep((ttft + gen) / 1000.0)


async def real_llm(system_prompt, user_prompt):
    """Real external-validity check on the emulation (LLM_BACKEND=real): the same prompt content
    the emulated path builds, sent to an actual model. Returns (text, prompt_tokens,
    completion_tokens) with token counts from the API's own usage report, not the local tokenizer
    estimate used for the emulated path's cost accounting."""
    resp = await state["anthropic"].messages.create(
        model=REAL_LLM_MODEL, max_tokens=300,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}])
    text = resp.content[0].text if resp.content else ""
    return text, resp.usage.input_tokens, resp.usage.output_tokens


async def truth_versions(conn, keys):
    out = {}
    for k in keys:
        kind, val = k.split(":", 1)
        if kind == "product":
            out[k] = await conn.fetchval("SELECT version FROM products WHERE id = $1", int(val)) or 0
        else:
            # see build_context: category ground truth is the epoch counter, not MAX(version)
            out[k] = await conn.fetchval("SELECT coalesce(epoch, 0) FROM categories WHERE category = $1", val) or 0
    return out


# ------------------------------------------------------------------------------------------------ read path
@app.post("/api/query")
async def query(req: Request):
    t0 = time.perf_counter()
    body = await req.json()
    q = body.get("q", "")
    keys = state["linker"].resolve(q)
    eset = canonical(keys)
    v = embed(q)
    pool, r = state["pool"], state["redis"]
    hit = None
    lookup_ms = 0.0
    async with pool.acquire() as conn:
        # ground truth is sampled BEFORE the lookup: a hit is stale iff it reflects an entity version older than one that
        # was already committed when the read began (the classic stale-read anomaly; concurrent writes do not count)
        truth = await truth_versions(conn, keys)
        if MODE != "none":
            tl = time.perf_counter()
            if MODE.startswith("graph") or MODE == "scoped-ttl":
                row = await conn.fetchrow("SELECT id, entity_set, answer, 1 - (vec <=> $1::vector) AS sim, created_at FROM semantic_cache "
                                          "WHERE entity_set = $2 ORDER BY vec <=> $1::vector LIMIT 1", vec_literal(v), eset)
            else:
                row = await conn.fetchrow("SELECT id, entity_set, answer, 1 - (vec <=> $1::vector) AS sim, created_at FROM semantic_cache "
                                          "ORDER BY vec <=> $1::vector LIMIT 1", vec_literal(v))
            lookup_ms = (time.perf_counter() - tl) * 1000
            if row and row["sim"] >= TAU:
                if MODE in ("ttl", "scoped-ttl") and (time.time() - row["created_at"].timestamp()) > TTL_S:
                    M["expired"] += 1
                    await conn.execute("DELETE FROM semantic_cache WHERE id = $1", row["id"])
                else:
                    hit = row
        if hit is None:
            ctx, versions, context_text = await build_context(conn, keys)
    if hit is not None:
        payload = await r.get(f"answer:{hit['id']}")
        ans = json.loads(payload) if payload else json.loads(hit["answer"])
        stale = any(ans["versions"].get(k, -1) < truth.get(k, 0) for k in keys)
        wrong = hit["entity_set"] != eset
        M["reads"] += 1; M["hits"] += 1; M["stale"] += int(stale); M["wrong_entity"] += int(wrong)
        lat = (time.perf_counter() - t0) * 1000
        M["hit_latency_ms_sum"] += lat; M["hit_lookup_ms_sum"] += lookup_ms
        return JSONResponse({"answer": ans["text"], "hit": True, "stale": stale, "wrong_entity": wrong, "sim": round(float(hit["sim"]), 4),
                             "entities": keys, "cache_entities": hit["entity_set"], "answer_versions": ans["versions"], "truth_versions": truth,
                             "prompt_tokens": 0, "completion_tokens": 0, "lookup_ms": round(lookup_ms, 2), "latency_ms": round(lat, 2)})
    # ---- miss: the context was read above; call the LLM (real or emulated) WITHOUT holding a pooled connection, then store entry + edges
    user_prompt = f"Context:\n{context_text}\n\nCustomer: {q}\nAssistant:"
    prompt = f"{SYSTEM_PROMPT}\n\n{user_prompt}"  # kept for the emulated path's tokenizer-based cost estimate
    if LLM_BACKEND == "real":
        text, p_tok, c_tok = await real_llm(SYSTEM_PROMPT, user_prompt)
    else:
        text = compose_answer(q, ctx)
        p_tok, c_tok = tok(prompt), tok(text)
        await emulated_llm(p_tok, c_tok)
    ans = {"text": text, "versions": versions}
    cache_id = None
    if MODE != "none":
        async with pool.acquire() as conn:
            async with conn.transaction():
                store = True
                if MODE.startswith("graph"):
                    # Serialise dependency registration with invalidating writes: take the per-entity advisory locks (in
                    # key order) so that a concurrent writer either commits before our version check (we then reject the
                    # entry) or after our insert (its invalidation then sees the entry). Without this, a write that commits
                    # between the check and the insert leaves a permanently stale entry.
                    for k in keys:
                        await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", k)
                    # version-checked insertion: an answer generated from state that has since been overwritten must not
                    # enter the cache (the write's invalidation already ran and would never see this entry)
                    now = await truth_versions(conn, keys)
                    if any(now.get(k, 0) != versions.get(k, 0) for k in keys):
                        M["insert_rejected"] += 1
                        store = False
                if store:
                    cache_id = await conn.fetchval("INSERT INTO semantic_cache(query, vec, entity_set, answer, prompt_tokens, completion_tokens) "
                                                   "VALUES ($1, $2::vector, $3, $4::jsonb, $5, $6) RETURNING id", q, vec_literal(v), eset, json.dumps(ans), p_tok, c_tok)
                    if MODE.startswith("graph") and keys:
                        await conn.executemany("INSERT INTO cache_deps(entity_key, cache_id) VALUES ($1, $2)", [(k, cache_id) for k in keys])
                    if CACHE_MAX_ENTRIES > 0:
                        evicted = await conn.fetch(
                            "DELETE FROM semantic_cache WHERE id IN ("
                            "  SELECT id FROM semantic_cache ORDER BY created_at ASC "
                            "  OFFSET $1"
                            ") RETURNING id", CACHE_MAX_ENTRIES)
                        if evicted:
                            M["evicted"] = M.get("evicted", 0) + len(evicted)
                            pipe = r.pipeline()
                            for row in evicted:
                                pipe.delete(f"answer:{row['id']}")
                            await pipe.execute()
    if cache_id is not None:
        pipe = r.pipeline()
        pipe.set(f"answer:{cache_id}", json.dumps(ans))
        if MODE.startswith("graph"):
            for k in keys:
                pipe.sadd(f"entity:{k}:caches", cache_id)
        await pipe.execute()
    M["reads"] += 1; M["misses"] += 1; M["llm_calls"] += 1; M["prompt_tokens"] += p_tok; M["completion_tokens"] += c_tok
    lat = (time.perf_counter() - t0) * 1000
    M["miss_latency_ms_sum"] += lat
    return JSONResponse({"answer": text, "hit": False, "stale": False, "wrong_entity": False, "sim": None, "entities": keys, "cache_id": cache_id,
                         "answer_versions": versions, "truth_versions": versions, "prompt_tokens": p_tok, "completion_tokens": c_tok,
                         "lookup_ms": round(lookup_ms, 2), "latency_ms": round(lat, 2)})


# ------------------------------------------------------------------------------------------------ write path
@app.post("/api/product/{pid}")
async def update_product(pid: int, req: Request):
    t0 = time.perf_counter()
    body = await req.json()
    pool, r = state["pool"], state["redis"]
    invalidated = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            if MODE.startswith("graph"):
                cat = await conn.fetchval("SELECT category FROM products WHERE id = $1", pid)
                for k in sorted([f"product:{pid}", f"category:{cat}"]):
                    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", k)
            row = await conn.fetchrow("UPDATE products SET price_cents = coalesce($2, price_cents), stock = coalesce($3, stock), "
                                      "version = version + 1, updated_at = now() WHERE id = $1 RETURNING id, category, version, price_cents, stock",
                                      pid, body.get("price_cents"), body.get("stock"))
            if row is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            if MODE == "graph-tx":
                # Invalidate(e) = { c | (e, c) in E } for e in {product:id, category:cat}, inside the writing transaction
                keys = [f"product:{pid}", f"category:{row['category']}"]
                ids = [x["cache_id"] for x in await conn.fetch("SELECT DISTINCT cache_id FROM cache_deps WHERE entity_key = ANY($1::text[])", keys)]
                if ids:
                    await conn.execute("DELETE FROM semantic_cache WHERE id = ANY($1::bigint[])", ids)
                    invalidated = len(ids)
            elif MODE == "flush":
                invalidated = await conn.fetchval("SELECT count(*) FROM semantic_cache")
                await conn.execute("TRUNCATE semantic_cache, cache_deps")
                M["flushes"] += 1
        # commit happened here. Redis payload removal (best effort; pgvector row is the source of truth for hits)
        if MODE == "graph-tx" and invalidated:
            pipe = r.pipeline()
            for i in ids:
                pipe.delete(f"answer:{i}")
            for k in keys:
                pipe.delete(f"entity:{k}:caches")
            await pipe.execute()
        elif MODE == "flush":
            await r.flushdb()
    M["writes"] += 1; M["invalidated_entries"] += invalidated
    lat = (time.perf_counter() - t0) * 1000
    M["write_latency_ms_sum"] += lat
    return JSONResponse({"id": pid, "version": row["version"], "price_cents": row["price_cents"], "stock": row["stock"], "invalidated": invalidated, "latency_ms": round(lat, 2)})


@app.get("/metrics")
async def metrics():
    async with state["pool"].acquire() as conn:
        n = await conn.fetchval("SELECT count(*) FROM semantic_cache")
        d = await conn.fetchval("SELECT count(*) FROM cache_deps")
    info = await state["redis"].info("memory")
    price_in, price_out = (REAL_PRICE_IN, REAL_PRICE_OUT) if LLM_BACKEND == "real" else (PRICE_IN, PRICE_OUT)
    out = dict(M); out.update({"ts_ms": int(time.time() * 1000), "mode": MODE, "tau": TAU, "cache_entries": n, "dep_edges": d,
                               "redis_used_memory": info.get("used_memory", 0), "llm_backend": LLM_BACKEND,
                               "cost_usd": M["prompt_tokens"] / 1e6 * price_in + M["completion_tokens"] / 1e6 * price_out})
    return out
