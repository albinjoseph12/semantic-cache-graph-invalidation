# Reduced local re-run of the category read-snapshot fix (2026-11-18)

**Scope: a reduced, local, free check -- not a re-run of the 184-run campaign.** Docker on one Apple-silicon Mac (14 CPUs, 7.7 GB), PostgreSQL 16 + pgvector, Redis 7.2, the testbed's own gateway code in `graph-tx` mode, emulated LLM, 5,000 products / 40 categories (same seed as the archive). Gateway image built from the testbed's Dockerfile plus `g++` because `wordllama` ships no linux-aarch64 wheel (so the embedding library was compiled locally; archive used x86 on GCP). `gw/Dockerfile`.

**Variants.** `archived` = the code the 184 archived runs used (`app/main.py` before the patch). `fixed` = with `category-snapshot.patch`. `*_widened` = the same two files plus ONE injected line, `await asyncio.sleep(0.005)`, between the category rows read and the category epoch read, to widen the race window from ~1 ms to ~5 ms. No other change.

**Content audit** (`tools/audit.py`): each new category-only cache entry is inspected within ~50 ms of insertion, inside one repeatable-read snapshot: the category's rows are re-read with the gateway's own SQL, the answer is rebuilt with the gateway's own `compose_answer`, and the *prices* in the stored text are compared with the rebuilt text. A flagged entry is *stale with current stamp* if its stored category epoch equals the snapshot's epoch -- i.e. invisible to any version-stamp comparison. Positive control: one deliberately stale entry planted beside one correct entry was flagged (stale $79.72 vs expected $1.95) and the correct one was not. Entries deleted by a write before they are inspected are not observed, so counts are lower bounds, equally for every variant.

## Arm 1 -- race stress (3 repetitions x 60 s per variant, interleaved)

80 concurrent readers asking only category questions over 8 'hot' categories; open-loop Poisson price writes (40/s offered) over the same 8 categories, 80% aimed at each category's 8 cheapest products (the items that decide a category answer); LLM emulation shortened (TTFT 100 ms, 200 tok/s). These are deliberate stress choices, not the paper's workload. The parameters were tuned on 60 s pilot runs of the archived code (kept in `results/pilot_discarded/`, not counted) until the effect was measurable.

| variant | reps | category entries audited | content-stale entries | ...with current stamp | gateway's own `stale` flag | gateway `wrong_entity` |
|---|---|---|---|---|---|---|
| archived | 3 | 1464 | 0 (0, 0, 0 per rep) | 0 | 0 | 0 |
| fixed | 3 | 1569 | 0 (0, 0, 0 per rep) | 0 | 0 | 0 |
| archived_widened | 3 | 1646 | 23 (5, 9, 9 per rep) | 23 | 0 | 0 |
| fixed_widened | 3 | 1493 | 0 (0, 0, 0 per rep) | 0 | 0 | 0 |

Widened window: 23/1646 entries content-stale with the archived read path vs 0/1493 with the snapshot read (one-sided Fisher exact p = 3.3e-07). Natural ~1 ms window: 0/1464 archived vs 0/1569 fixed -- the race was **not observed** at natural timing in 3 x 60 s, so these runs give no estimate of how often it occurs in practice. In every flagged case the gateway's own stale flag was 0, confirming the archived freshness label cannot see this failure.

Client-side request errors: the fixed variants showed more than the archived ones in the matrix (fixed 13, fixed_widened 11; archived 3, archived_widened 2; each over ~12-15k reads, i.e. <0.1%). The exception type was not recorded in the matrix runs; in two repeat runs of `fixed` with it recorded (`results/errcheck/`, not part of the matrix) the 4 errors in one run were all `RemoteProtocolError: Server disconnected without sending a response` (an HTTP keep-alive race against uvicorn's idle timeout) and the other run had none. The gateway logs contain no exceptions. **I did not establish why the fixed variants show more of them**; it may be timing or a side effect of the transaction, and it is flagged here rather than dismissed.

## Arm 2 -- standard mix, reduced (2 repetitions x 60 s per variant)

The testbed's own Locust workload (`load/locustfile.py`), READ_SHARE 0.60, 200 users (archive: 500), 60 s, no warm-up, same content audit running alongside. One gateway worker saturates on this machine (read p50 ~2 s), so latency and hit-rate figures are NOT comparable with the paper; the arm is a parity check between the two code variants only.

| variant | rep | reads | hit rate | read p50 ms | read p99 ms | stale-flag hits | wrong-entity hits | category entries audited | content-stale | inserts rejected |
|---|---|---|---|---|---|---|---|---|---|---|
| archived | 1 | 2324 | 0.241 | 2012 | 4698 | 0 | 0 | 91 | 0 | 300 |
| archived | 2 | 2347 | 0.258 | 1994 | 4823 | 0 | 0 | 83 | 0 | 285 |
| fixed | 1 | 2329 | 0.260 | 1978 | 4698 | 0 | 0 | 67 | 0 | 281 |
| fixed | 2 | 2339 | 0.247 | 2009 | 4778 | 0 | 0 | 81 | 0 | 291 |

No difference in hit rate, rejected inserts or flags between variants, and no content-stale entries in either (91, 83, 67 and 81 category entries audited).

## What this does and does not show

- **Shows:** end-to-end through the real gateway, Redis, pgvector and PostgreSQL, the archived read path admits stale category answers carrying a current stamp when the gap between its two reads is non-trivial, the archived freshness flag reports none of them, and the single-snapshot read admits none. The fix did not change hit rate or insert-rejection behaviour in the standard mix.
- **Does not show:** how often the race occurs at natural timing (zero events at ~1 ms in 3 x 60 s; the widened arm is an injected delay); anything about the 184 archived runs (they cannot be re-graded at content level -- the audit history stores versions, not prices); behaviour at the paper's scale, with a real LLM, or on x86.
- **Also:** price-only comparison (stock not checked); undercounting auditor; 60 s runs; stress parameters were tuned on pilots of the archived variant; the gateway is saturated at these loads.

Files: `variants/`, `gw/Dockerfile`, `tools/`, `results/{stress,standard,errcheck,pilot_discarded}/` (per-run `summary.json`, `requests.csv`, `driver.log`).

## Arm 3 -- the paper's own open-loop cells, paired (added 2026-11-19)

The main `graph-tx` cells re-run with the campaign's own workload: `load/open_loop_gen.py`, 20 arrivals/s, 120 s, seed 20260906, tau 0.70, LLM emulation as in the campaign (TTFT 500 ms, 50 tok/s), 10 s warm-up, all three read mixes; `archived` vs `fixed` read path paired and interleaved, 3 repetitions each, plus 3 more of each at 10% writes (`tools/run_openloop.sh`, `tools/run_openloop_matrix.sh`, `tools/run_openloop_repeat.sh`). Metrics are computed with the testbed's own `analysis/analyze.py` (steady window 40-120 s) by `tools/summarize_openloop.py`; the content auditor ran alongside every run. Same single Mac as the other arms, so latency is not comparable with the cloud campaign; hit rates are.

| Writes | Read path | n | Hit rate | Stale /1k (stamp) | Wrong-entity (%) | Read p50 (ms) | Read mean (ms) | LLM /1k | Category entries audited | Content-stale |
|---|---|---|---|---|---|---|---|---|---|---|
| 10% | archived | 6 | 0.315 ± 0.000 | 0.0 | 0.0 | 1935 | 1717 | 744 | 1451 | 0 (0 with current stamp) |
| 10% | fixed | 5 | 0.314 ± 0.001 | 0.0 | 0.0 | 1941 | 1725 | 745 | 1211 | 0 (0 with current stamp) |
| 25% | archived | 3 | 0.277 ± 0.001 | 0.0 | 0.0 | 1982 | 1797 | 785 | 565 | 0 (0 with current stamp) |
| 25% | fixed | 3 | 0.277 ± 0.001 | 0.0 | 0.0 | 1981 | 1794 | 785 | 562 | 0 (0 with current stamp) |
| 40% | archived | 3 | 0.221 ± 0.001 | 0.0 | 0.0 | 2026 | 1898 | 827 | 405 | 0 (0 with current stamp) |
| 40% | fixed | 3 | 0.220 ± 0.001 | 0.0 | 0.0 | 2026 | 1900 | 828 | 408 | 0 (0 with current stamp) |

Excluded from the means (disrupted run): fixed, 10% writes, rep1: 1099 of 2,400 requests timed out (hit rate 0.220); content-stale entries 0 of 136 audited.

Campaign values (GCP, archived read path) for comparison: hit rate 0.316 / 0.277 / 0.220 at 10 / 25 / 40% writes.

**Reading.** With the paper's workload the two read paths are indistinguishable (hit rate, latency, LLM calls within 1%), and the archived hit rates reproduce on different hardware. No content-stale entry appeared with either read path at natural timing, consistent with Arms 1-2: the race needs a non-trivial gap between the two reads to show up at this load. The disrupted run: during it the load generator (same machine) stopped issuing requests for about 50 s (issue timestamps have a gap from 50 s to 100 s), after which a backlog of requests timed out; the gateway log has no errors. It coincided with other work on the machine (document builds); its cause was not established, so it is listed, not averaged, and the cell was repeated three more times for both read paths with normal results.
