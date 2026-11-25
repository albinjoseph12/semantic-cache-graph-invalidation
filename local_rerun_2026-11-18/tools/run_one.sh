#!/usr/bin/env bash
# run_one.sh <variant: archived|fixed> <arm: stress|standard> <rep> [duration]
set -euo pipefail
export PATH=$PATH:/Applications/Docker.app/Contents/Resources/bin
V=${1:?}; ARM=${2:?}; REP=${3:?}; DUR=${4:-60}
R="$(cd "$(dirname "$0")/.." && pwd)"; T="$R/../corrected_semcache-testbed"; PY=${PY:?set PY to venv python}
OUT="$R/results/$ARM/$V/rep$REP"; mkdir -p "$OUT"
DSN="postgresql://acb:acb@127.0.0.1:55433/semcache"
docker rm -f semgw >/dev/null 2>&1 || true
# reset: pristine prices/stock, empty cache, epochs and versions at 1, empty audit history, empty redis
docker exec sempg psql -q -U acb -d semcache -c "SET session_replication_role = replica; UPDATE products p SET price_cents=q.price_cents, stock=q.stock, version=1, category=q.category FROM products_pristine q WHERE p.id=q.id; TRUNCATE semantic_cache, cache_deps, product_history, category_history; UPDATE categories SET epoch=1;" >/dev/null
docker exec semredis redis-cli flushall >/dev/null
if [ "$ARM" = stress ]; then TTFT=100; TPS=200; else TTFT=500; TPS=50; fi
docker run -d --name semgw --network semnet -p 127.0.0.1:58000:8000 -v "$R/variants/${V}_main.py:/app/main.py:ro" \
  -e CACHE_MODE=graph-tx -e TAU=0.70 -e SC_PG_URL=postgresql://acb:acb@postgres:5432/semcache -e REDIS_URL=redis://redis:6379/0 \
  -e LLM_TTFT_MS=$TTFT -e LLM_TPS=$TPS semcache-gw:local uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level warning >/dev/null
for i in $(seq 1 60); do curl -sf 127.0.0.1:58000/metrics >/dev/null && break; sleep 1; done
curl -s 127.0.0.1:58000/metrics | grep -q '"graph-tx"' || { echo "gateway not graph-tx"; exit 1; }
echo "[$(date +%T)] $ARM $V rep$REP dur=${DUR}s"
if [ "$ARM" = stress ]; then
  "$PY" "$R/tools/stress_driver.py" --gateway http://127.0.0.1:58000 --dsn "$DSN" --main-py "$R/variants/${V}_main.py" --duration "$DUR" \
     --readers ${READERS:-200} --write-rate ${WRATE:-40} --hot-cats ${HOTCATS:-0} --target-cheapest ${TARGET:-0} --seed "$REP" --out "$OUT" > "$OUT/driver.log" 2>&1
else
  "$PY" "$R/tools/standard_arm.py" --gateway http://127.0.0.1:58000 --dsn "$DSN" --main-py "$R/variants/${V}_main.py" --duration "$DUR" --rep "$REP" --out "$OUT" --testbed "$T" > "$OUT/driver.log" 2>&1
fi
docker logs semgw > "$OUT/gateway.log" 2>&1 || true
docker rm -f semgw >/dev/null 2>&1 || true
echo "[$(date +%T)] done"
