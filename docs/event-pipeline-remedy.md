# Venue event overflow remedy — repository implementation

1. **Outcome and scope**

   This patch removes the measured per-tick bottleneck on IBKR and the repeated full-book copying cost shared by both venues. It is a local repository change. Production code, services, credentials, deployment configuration, and the existing 1 GiB container memory limit were not changed. No TWS or live Polymarket connection was used for validation.

   The evidence supports a large reduction in overflow risk from ordinary market-data processing. It does not certify production throughput or guarantee that an arbitrarily large burst, stalled disk, or slow venue operation cannot overflow a finite buffer.

2. **IBKR and coordinator changes**

   Price and size callbacks still acquire the execution coordinator lock. They skip ledger queries, reconciliation scans, portfolio reconstruction, and execution-state publication inside that lock. Execution, order, position, and reconciliation callbacks retain their accounting path. Reconciliation also runs from the periodic execution cycle at most once per second, in addition to relevant event triggers.

   Numeric quote ticks that the state reducer does not consume are filtered before admission. Required price ticks 1/2/4/66/67/68 and size ticks 0/3/69/70 are retained. New subscription request IDs are never reused within the adapter lifetime. Retired subscription callbacks are rejected at admission, buffer drain, and consumer dispatch.

   The network thread now appends to a bounded FIFO and schedules one drain notification per pending burst. Each drain admits at most 64 callbacks and waits for shared-queue capacity. This removes the unbounded accumulation of per-callback event-loop notifications. Execution, order, account, and P&L events are neither coalesced nor selectively discarded to make room for quotes.

   `EVENT_QUEUE_MAXSIZE` remains 10,000. It now bounds each of two queues: the shared asyncio queue and the IBKR thread handoff buffer. Thus these two layers can hold up to 20,000 events in total, excluding SDK/socket buffers. Buffer capacity is supporting burst protection; the measured service-time reduction is the primary fix. A genuine admission loss still sets a sticky halt flag and increments the loss counter. New IBKR entries check this flag directly, before the supervisor has necessarily published the halt. Cancelling existing orders remains available.

3. **State, Polymarket, and dashboard changes**

   Quote, account, P&L, book, and venue-health reductions use atomic updates that replace only changed fields. Frozen `BookLevel` tuples are shared across deep copies of book models. Mutable snapshot dictionaries and returned/subscribed snapshots remain isolated. Margin-preview polling reads only the preview; size ticks no longer trigger unnecessary preview refreshes.

   Complete Polymarket books remain authoritative. A batched delta builds and sorts each changed side once, reuses unchanged levels, and validates the final batch before publishing it. The browser receives the best five bids and best five asks at a cadence no faster than four updates per second per connection, further bounded by the configured publication interval. Deeper levels remain available for level-six promotion, depth checks, and hedge calculations.

   An unsynchronized book cannot become qualified merely by receiving a delta or tick-size change. Integrity failures request a fresh stream. Reconnects carry a generation identifier, cancel and close the old stream, and reject queued public events from retired generations. REST mismatches invalidate the streaming book rather than mixing a REST replacement with already queued WebSocket deltas. Fresh WebSocket snapshots restore qualification. Authenticated user events are not discarded by public-stream quarantine.

4. **Safety and observability**

   One consumer and the coordinator lock are retained. The consumer yields after 64 events or a five-millisecond work budget so timers, dashboard work, and producers can run. Unexpected processing failures pause, disarm, and activate the kill switch.

   After a new ZQ intent is durably persisted, the coordinator rechecks snapshot identity, entry authorization, and callback loss immediately before the synchronous send, with no intervening awaited operation. If authorization changed, the unsent intent becomes `ABORTED` and its batch becomes `COMPLETE`; it cannot leave a phantom active batch. Previously submitted intents cannot be abandoned through this method. Existing duplicate/late-fill accounting and hedge obligations remain intact.

   Authenticated `GET /api/v1/diagnostics/events` exposes shared queue occupancy/high-water, processed/skipped/failed counts, maximum handler time, maximum event wait, supervisor-observed event-loop lag, consumer status, and IBKR buffer/callback counts. Counters and maxima describe the current process lifetime; absent callback-counter keys mean no occurrences. Event wait uses received UTC timestamps; handler and event-loop timing use the monotonic loop clock. `/readyz` rejects an active safety halt or a stopped event consumer even if venue connections look healthy.

5. **Measured evidence**

   Baseline commit: `9cdb7c70facc26078a0b0d2e3fdb72727ecd1295`. Same-machine handler measurements used Python 3.13.12 on Windows, ten books, warmup, and shuffled sample blocks. These are isolated handler timings, not production end-to-end speedup claims.

   | Handler | Before, median | After, median |
   |---|---:|---:|
   | IBKR retained size tick, reconciliation incomplete, 50 levels/side | 21.30 ms | 0.0092 ms |
   | IBKR retained size tick, reconciliation inputs complete, 50 levels/side | 25.29 ms | 0.0093 ms |
   | Polymarket delta, 50 levels/side | 14.39 ms | 0.0500 ms |
   | Polymarket delta, 100 levels/side | 28.19 ms | 0.0814 ms |

   IBKR size-tick work fell from 3–6 SQL SELECTs and six full snapshot copies to zero of each, while retaining the coordinator lock. Detailed samples and source hashes are in [event-handler-comparison.json](validation/event-handler-comparison.json).

   The offline pipeline exercise ran the actual event consumer, reducers, SQLite ledger, analytics, supervisor, and two dashboard serializers with ten complete books at 100 levels per side. IBKR quotes arrived through the thread handoff. Duplicate and late fills used simulated hedge transport; no live requests were made. Each scenario verified all six injected accounting/order/fill callbacks in order, a final ZQ quantity of five, zero hedge deficits, and correct final quotes and book sizes.

   | Scenario | Market updates | Elapsed | Observed events/sec | Queue peak / 10,000 | Max event wait | Overflow |
   |---|---:|---:|---:|---:|---:|---:|
   | IBKR-heavy | 30,000 | 3.03 s | 9,912 | 800 | 75 ms | 0 |
   | Polymarket-heavy | 30,000 | 6.09 s | 4,925 | 950 | 200 ms | 0 |
   | Combined sustained run | 300,000 | 60.00 s | 5,000 | 801 | 159 ms | 0 |

   The combined run's IBKR buffer peaked at 378/10,000 and its maximum observed event-loop lag was 16.4 ms. All queues drained. Raw results: [IBKR](validation/event-pipeline-ibkr.json), [Polymarket](validation/event-pipeline-polymarket.json), [combined](validation/event-pipeline-mixed.json). Arrival targets were chosen for synthetic stress; production peak arrival rates have not been established. Disk SQLite, real venue latency, and a large historical ledger are not represented by these replay results.

   Reproduce from the repository using the installed environment:

   ```powershell
   $env:PYTHONPATH = 'src'
   .venv/Scripts/python.exe scripts/benchmark_event_pipeline.py --mode ibkr --events 30000 --rate 10000
   .venv/Scripts/python.exe scripts/benchmark_event_pipeline.py --mode polymarket --events 30000 --rate 5000
   .venv/Scripts/python.exe scripts/benchmark_event_pipeline.py --mode mixed --events 150000 --rate 2500
   ```

6. **Validation and remaining operational work**

   Regression coverage includes coordinator serialization, quote-path accounting exclusion, periodic reconciliation, mutable-state isolation, full-depth promotion, atomic deltas, genuine overflow halts, cancellation while halted, retired request IDs, stream closure/generation recovery, consumer failure, API authentication, and authorization changes during intent persistence. Existing fill, partial-fill, cancellation, late-fill, and reconciliation tests remain part of the suite.

   All required CI checks passed locally: Ruff, mypy (31 source files), pytest (130 tests), dashboard lint, dashboard tests (11 tests across seven files), and the production dashboard build. The Git whitespace check also passed. An additional coverage run found the repository-wide 85% threshold was already unmet: approximately 79% on the baseline and 80% after this patch. The threshold and exclusions were not relaxed; CI currently runs pytest without the optional coverage gate.

   Before a separately authorized production release, replay measured production rates in a comparable read-only/staging environment, exercise local paper-TWS disconnect/reconnect and account callbacks, and verify queue age and critical callback latency during slow venue/disk operations. Then evaluate read-only operation under the existing memory limit before considering a memory increase. No rollout or production restart has been performed here.

   The conservative first release intentionally retains coordinator serialization. Dedicated critical-event scheduling, shorter network waits under that lock, and a ledger-derived-state cache remain possible follow-ups if measured operation shows residual contention. They introduce ordering/invalidation changes and are not necessary to obtain the large handler-cost reductions measured in this patch.
