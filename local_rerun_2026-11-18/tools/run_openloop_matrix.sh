#!/usr/bin/env bash
cd "/Users/albinjoseph/llm-caching-research/local_rerun_2026-11-18"
for rep in 1 2 3; do for rs in 0.60 0.75 0.90; do for v in archived fixed; do ./tools/run_openloop.sh $v $rs $rep; done; done; done
echo MATRIX_DONE
