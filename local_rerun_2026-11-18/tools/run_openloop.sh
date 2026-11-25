#!/usr/bin/env bash
# run_openloop.sh <variant> <read_share> <rep>  -- the paper's open-loop cell (20 arrivals/s, 120 s, seed 20260906, tau 0.70, graph-tx), locally
set -euo pipefail
export PATH=$PATH:/Applications/Docker.app/Contents/Resources/bin
V=$1; RS=$2; REP=$3; R="/Users/albinjoseph/llm-caching-research/local_rerun_2026-11-18"; T="$R/../corrected_semcache-testbed"; PY="/private/tmp/claude-501/-Users-albinjoseph-Library-Application-Support-Claude-scratch-workspaces-07558cd8-2caa-4fa6-b724-2593cbef3e26-e3f46b2f-c2de-4dcb-b1d2-8ee8e35ebc83-scratch-2026-09-12-45c71b/741eca03-af50-40f7-9e55-b5dbb7601846/scratchpad/venv/bin/python"
OUT="$R/results/openloop/$V/r${RS}_t0.70/rep$REP"; mkdir -p "$OUT"; DSN="postgresql://acb:acb@127.0.0.1:55433/semcache"
docker rm -f semgw >/dev/null 2>&1 || true
docker exec sempg psql -q -U acb -d semcache -c "SET session_replication_role = replica; UPDATE products p SET price_cents=q.price_cents, stock=q.stock, version=1, category=q.category FROM products_pristine q WHERE p.id=q.id; TRUNCATE semantic_cache, cache_deps, product_history, category_history; UPDATE categories SET epoch=1;" >/dev/null
docker exec semredis redis-cli flushall >/dev/null
docker run -d --name semgw --network semnet -p 127.0.0.1:58000:8000 -v "$R/variants/${V}_main.py:/app/main.py:ro" \
  -e CACHE_MODE=graph-tx -e TAU=0.70 -e SC_PG_URL=postgresql://acb:acb@postgres:5432/semcache -e REDIS_URL=redis://redis:6379/0 \
  -e LLM_TTFT_MS=500 -e LLM_TPS=50 semcache-gw:local uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level warning >/dev/null
for i in $(seq 1 60); do curl -sf 127.0.0.1:58000/metrics >/dev/null && break; sleep 1; done
curl -s 127.0.0.1:58000/metrics | grep -q '"graph-tx"' || { echo "gateway not graph-tx"; exit 1; }
echo "[$(date +%T)] $V rs=$RS rep$REP"
"$PY" "$T/load/warmup.py" --seconds 10 --base http://127.0.0.1:58000 >/dev/null 2>&1 || true
rm -f "$OUT/stop"; "$PY" "$R/tools/audit_cli.py" "$DSN" "$R/variants/${V}_main.py" "$OUT/stop" "$OUT/audit.json" & AUD=$!
"$PY" "$T/load/open_loop_gen.py" --host http://127.0.0.1:58000 --rate 20 --duration 120 --seed 20260906 --read-share $RS --out "$OUT/requests.csv" > "$OUT/gen.log" 2>&1 || echo "gen failed"
sleep 5; touch "$OUT/stop"; wait $AUD || true
curl -s 127.0.0.1:58000/metrics > "$OUT/final_metrics.json" || true
docker logs semgw > "$OUT/gateway.log" 2>&1 || true; docker rm -f semgw >/dev/null 2>&1 || true
