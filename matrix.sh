#!/usr/bin/env bash
cd /home/claude/semcache-testbed
for rep in 0 1 2; do
  for rs in 0.75 0.60 0.90; do
    for m in none ttl flush graph-cdc graph-tx; do
      ./run_benchmark.sh $m $rs 0.70 $rep 120 2>&1 | grep -E "start|done"
    done
  done
done
# threshold sensitivity at 75 % reads
for rep in 0 1; do
  for tau in 0.50 0.60 0.80 0.90; do
    for m in ttl graph-tx; do
      ./run_benchmark.sh $m 0.75 $tau $rep 120 2>&1 | grep -E "start|done"
    done
  done
done
echo "MATRIX COMPLETE"
