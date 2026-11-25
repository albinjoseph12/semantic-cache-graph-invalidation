#!/usr/bin/env bash
# End-to-end benchmark loop for the semantic-cache study.
#   ./run_benchmark.sh <mode> <read_share> <tau> <rep> [duration_s]
#     mode       : none | ttl | flush | graph-cdc | graph-tx
#     read_share : 0.60 | 0.75 | 0.90   (the remainder are OLTP writes)
#     tau        : cosine similarity threshold, e.g. 0.70
# Each run: fresh gateway process (and invalidator for graph-cdc), empty cache and Redis, product versions reset,
# 10 s warm-up, then Locust (500 users) + 200 ms monitor for <duration_s>. Artefacts -> results/<mode>/r<read_share>_t<tau>/rep<rep>/
set -euo pipefail
MODE=${1:?mode}; RS=${2:?read_share}; TAU=${3:?tau}; REP=${4:?rep}; DUR=${5:-120}; TTL=${TTL_S:-60}
ROOT="$(cd "$(dirname "$0")" && pwd)"
EMB=${EMBEDDING:-wl256}
SUB="$MODE${CDC_LAG_MS:+-lag$CDC_LAG_MS}"
[ "$EMB" != "wl256" ] && SUB="${SUB}__${EMB}"
[ "${LLM_BACKEND:-emulated}" != "emulated" ] && SUB="${SUB}__${LLM_BACKEND}"
OUT="$ROOT/results/$SUB/r${RS}_t${TAU}/rep$REP"; mkdir -p "$OUT"
export SC_DSN="dbname=semcache user=acb password=acb host=localhost port=5432"
cleanup() {
  # kill whatever holds the gateway port, whichever application module it was started from, and wait
  # for the port to be released; a lingering gateway would otherwise serve the next run's traffic
  pkill -f 'uvicorn main:app' >/dev/null 2>&1 || true
  pkill -f 'uvicorn gptcache_gateway:app' >/dev/null 2>&1 || true
  pkill -f 'app/invalidator.py' >/dev/null 2>&1 || true
  fuser -k -TERM 8000/tcp >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    fuser 8000/tcp >/dev/null 2>&1 || break
    sleep 0.5
  done
}
trap cleanup EXIT
cleanup; sleep 1
# reset state: empty cache + deps, reset product versions and category epochs to a clean baseline,
# clear the independent audit history so this rep's history starts fresh, flush redis
psql -q -U acb -h localhost -d semcache -c "TRUNCATE semantic_cache, cache_deps, product_history, category_history; UPDATE products SET version = 1; UPDATE categories SET epoch = 1;" >/dev/null
redis-cli flushall >/dev/null
echo "[$(date +%T)] start $MODE emb=$EMB rs=$RS tau=$TAU rep=$REP -> $OUT"
# the GPTCache baseline is a different application module; everything else about the run is identical
APP_MODULE="main:app"; [ "$MODE" = "gptcache" ] && APP_MODULE="gptcache_gateway:app"
( cd "$ROOT/app" && CACHE_MODE=$MODE EMBEDDING=$EMB TAU=$TAU TTL_S=$TTL GPTCACHE_TTL=${GPTCACHE_TTL:-0} \
    LLM_BACKEND=${LLM_BACKEND:-emulated} REAL_LLM_MODEL=${REAL_LLM_MODEL:-claude-haiku-4-5-20251001} ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-} \
    uvicorn $APP_MODULE --host "${SC_BIND:-127.0.0.1}" --port 8000 --workers 1 --log-level warning > "$OUT/gateway.log" 2>&1 & )
GW_OK=0
for i in $(seq 1 40); do curl -sf localhost:8000/metrics >/dev/null && { GW_OK=1; break; }; sleep 1; done
if [ "$GW_OK" != "1" ] || ! curl -sf localhost:8000/metrics | grep -q "\"mode\": *\"$MODE\""; then
  echo "[$(date +%T)] FAILED to start gateway for mode=$MODE (see $OUT/gateway.log)"; exit 1
fi
if [ "$MODE" = "graph-cdc" ]; then
  ( INVALIDATOR_LOG="$OUT/invalidator.csv" CDC_LAG_MS=${CDC_LAG_MS:-0} taskset -c 1 python3 "$ROOT/app/invalidator.py" > "$OUT/invalidator.log" 2>&1 & )
  sleep 2
fi
python3 "$ROOT/load/warmup.py" --seconds 10 >/dev/null
taskset -c 0 python3 "$ROOT/telemetry/monitor.py" --out "$OUT/telemetry.csv" --duration $((DUR+5)) > "$OUT/monitor.log" 2>&1 &
MON=$!
# SKIP_LOCAL_LOAD=1 (Phase 3/4 split-host architecture): keep the load generator off the measured
# service host. The server side still starts every process and holds open for the measured
# duration; a remote driver (orchestrate_split_experiment.sh, run from the load-generator host)
# points its own open_loop_gen.py at this host's private IP for the same window and copies
# requests.csv back into $OUT afterward.
if [ -n "${SKIP_LOCAL_LOAD:-}" ]; then
  echo "[$(date +%T)] SKIP_LOCAL_LOAD set: waiting ${DUR}s for the remote load generator" >> "$OUT/locust.log"
  sleep "$DUR"
else
  READ_SHARE=$RS SC_DURATION=$DUR SC_USERS=500 SC_REQ_LOG="$OUT/requests.csv" taskset -c 0 locust -f "$ROOT/load/locustfile.py" --headless -H http://localhost:8000 \
    --csv "$OUT/locust" --only-summary > "$OUT/locust.log" 2>&1 || true
fi
wait $MON || true
curl -s localhost:8000/metrics > "$OUT/final_metrics.json" || true
# independent audit history (Phase 2), dumped so analysis/independent_checker.py can re-grade this
# run from raw committed history without touching the gateway process at all
psql -q -U acb -h localhost -d semcache -c "\copy (SELECT product_id, version, extract(epoch from committed_at)*1000 AS ts_ms FROM product_history ORDER BY committed_at) TO '$OUT/product_history.csv' WITH CSV HEADER" >/dev/null
psql -q -U acb -h localhost -d semcache -c "\copy (SELECT category, epoch, extract(epoch from committed_at)*1000 AS ts_ms FROM category_history ORDER BY committed_at) TO '$OUT/category_history.csv' WITH CSV HEADER" >/dev/null
echo "[$(date +%T)] done  $MODE emb=$EMB rs=$RS tau=$TAU rep=$REP"
