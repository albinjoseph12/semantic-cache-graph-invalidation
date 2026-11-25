#!/usr/bin/env bash
# repeat of the 10%-write cell on an otherwise idle host (reps 4-6), after one fixed-path run was disrupted
cd "/Users/albinjoseph/llm-caching-research/local_rerun_2026-11-18"
for rep in 4 5 6; do for v in archived fixed; do ./tools/run_openloop.sh $v 0.90 $rep; done; done
echo REPEAT_DONE
