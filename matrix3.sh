#!/usr/bin/env bash
# Revision experiments for the semantic-cache study.
#   A. GPTCache baseline: the library itself, same workload, same embedding, same emulated LLM.
#   B. Embedding families: WordLlama-256, WordLlama-64 (Matryoshka prefix) and a lexical TF-IDF+SVD
#      family, each over a threshold grid, for the proposed mode and the TTL baseline.
#   C. Additional repetitions of the headline cells so that every reported mean carries a CI.
set -uo pipefail
cd "$(dirname "$0")"
log() { echo "[$(date +%T)] $*" | tee -a matrix3.log; }

stage_gptcache() {
  for rep in 0 1 2; do
    for tau in 0.70 0.80; do
      [ -s "results/gptcache/r0.75_t${tau}/rep$rep/requests.csv" ] && continue
      ./run_benchmark.sh gptcache 0.75 $tau $rep 120 >> matrix3.log 2>&1
      log "gptcache tau=$tau rep=$rep done"
    done
  done
}

stage_embeddings() {
  for emb in wl64 lsa256; do
    for tau in 0.60 0.70 0.80; do
      for m in graph-tx ttl; do
        for rep in 0 1; do
          [ -s "results/${m}__${emb}/r0.75_t${tau}/rep$rep/requests.csv" ] && continue
          EMBEDDING=$emb ./run_benchmark.sh $m 0.75 $tau $rep 120 >> matrix3.log 2>&1
          log "emb=$emb $m tau=$tau rep=$rep done"
        done
      done
    done
  done
  # the wl256 family already has 0.60/0.70/0.80 cells for graph-tx and ttl from the main matrix
}

stage_reps() {
  for rep in 3 4; do
    for m in none ttl flush graph-cdc graph-tx; do
      [ -s "results/$m/r0.75_t0.70/rep$rep/requests.csv" ] && continue
      ./run_benchmark.sh $m 0.75 0.70 $rep 120 >> matrix3.log 2>&1
      log "reps $m rep=$rep done"
    done
  done
}

case "${1:-all}" in
  gptcache) stage_gptcache ;;
  emb) stage_embeddings ;;
  reps) stage_reps ;;
  *) stage_gptcache; stage_embeddings; stage_reps ;;
esac
log "MATRIX3 COMPLETE"
