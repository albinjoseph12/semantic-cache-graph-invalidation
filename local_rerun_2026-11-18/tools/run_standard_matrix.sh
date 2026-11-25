#!/usr/bin/env bash
cd "/Users/albinjoseph/llm-caching-research/local_rerun_2026-11-18"
for rep in 1 2; do for v in archived fixed; do
  PY=/private/tmp/claude-501/-Users-albinjoseph-Library-Application-Support-Claude-scratch-workspaces-07558cd8-2caa-4fa6-b724-2593cbef3e26-e3f46b2f-c2de-4dcb-b1d2-8ee8e35ebc83-scratch-2026-09-12-45c71b/741eca03-af50-40f7-9e55-b5dbb7601846/scratchpad/venv/bin/python ./tools/run_one.sh $v standard $rep 60
done; done
echo MATRIX_DONE
