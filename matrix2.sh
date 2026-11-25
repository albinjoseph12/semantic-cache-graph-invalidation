#!/usr/bin/env bash
cd /home/claude/semcache-testbed
while ! grep -q "MATRIX COMPLETE" results/matrix.log; do sleep 30; done
for rep in 0 1 2; do for rs in 0.75 0.60 0.90; do ./run_benchmark.sh scoped-ttl $rs 0.70 $rep 120 2>&1 | grep -E "start|done"; done; done
for rep in 0 1; do for lag in 100 1000; do CDC_LAG_MS=$lag ./run_benchmark.sh graph-cdc 0.75 0.70 $rep 120 2>&1 | grep -E "start|done"; done; done
echo "MATRIX2 COMPLETE"
