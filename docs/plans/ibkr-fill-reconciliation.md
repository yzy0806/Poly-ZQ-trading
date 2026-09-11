**Plan: prevent false trading pauses during IBKR fill reconciliation**

Prepared and approved 2026-09-11. Status: implemented locally. The plan below records the approved design; implementation validation is in `docs/validation/ibkr-callback-reconciliation.md`. The required timeout was added to the local and example environments. The running trading process and live ledger were not changed or restarted during implementation.

1. **Problem and evidence**

   At 19:57:34 Taipei time on 2026-09-11, the venue position became 24 ZQ while the execution ledger still held 23. Reconciliation classified this as unexplained inventory and paused/disarmed the engine. The execution was recorded about 71 milliseconds later. The same order, 166, also appeared briefly as an unexpected open order. Both Polymarket hedges were submitted at 19:57:36; reconciliation became CLEAN at 19:58:05. The pause remained set.

   `coordinator.py:133` reconciles after individual IBKR callbacks. Previous snapshot completion flags remain true while those callbacks update different parts of the view. `coordinator.py:528` adds every own-client open-order callback to the observed-open set, while `repository.py:411` separately rejects a terminal-to-working status regression. Consequently, the in-memory open-order set can disagree with the durable order state. `coordinator.py:749` then latches the safety pause; a later CLEAN result clears alerts but does not re-arm trading.

2. **Required behavior**

   Use the existing UNKNOWN reconciliation status for a short, explainable callback gap, with a reason such as "Awaiting IBKR fill confirmation." UNKNOWN must always block new ZQ submissions and replacements. It must not, by itself, set the lasting pause/disarm flags. Continue consuming callbacks and processing independently verified fills and their existing, capped hedge obligations.

   Preserve the operator's armed/paused state during this temporary condition. When fresh evidence reconciles positions, orders, executions, and Polymarket obligations, clear the temporary gate and evaluate all normal entry checks again. A manual pause, HALT, disconnect recovery, startup disarm, or confirmed safety pause must never be automatically cleared by this change.

   An unexplained order, foreign-client order, unidentified execution, contradictory identity, excess exposure, or persistent inventory mismatch must still block trading and invoke the existing safety response. No position adjustment or synthetic execution may be used to obtain CLEAN.

3. **Correlate callback gaps to known orders**

   Introduce explicit tracking for outstanding IBKR updates. A position-ahead-of-execution candidate must match the configured account and contract, the direction of a known strategy order, and no more than that order's remaining executable quantity. The reverse ordering must be explainable by actual, newly persisted executions whose position updates have not arrived. A terminal status reporting fills not yet present in the execution ledger is also incomplete evidence.

   These matches justify a bounded UNKNOWN state; they do not prove that a position change belongs to the strategy. Only authoritative execution records may create hedge obligations. An unexplained delta, conflicting identity, or independent safety difference bypasses the temporary allowance.

   Proposed policy: a required `IBKR_CALLBACK_SETTLE_SECONDS` setting, initially 2 seconds, measured with a monotonic clock from the first unresolved callback gap. Duplicate callbacks and repeated account reads must not extend the deadline. This deadline applies to IBKR callback convergence, not to Polymarket settlement. Update the local environment, the repository's example environment, configuration validation, and deployment documentation together when implementing it; retain the repository's convention of explicit environment values rather than a hidden config fallback.

   On expiry, latch the pause/disarm, request cancellation of this strategy's remaining working ZQ orders, and keep processing fills and existing hedge recovery. A refresh timeout or missing callback cannot leave the engine indefinitely in the temporary state.

4. **Make order observations and refreshes consistent**

   Have the order-status update path return the effective accepted state and relevant fill evidence. Update the coordinator's observed-open membership from that result and the callback's terminal status, rather than adding an ID before checking the durable state. Both `open_order` and `order_status` need the same transition rules. Late fills after cancellation must still be recorded exactly once.

   Treat a delayed working-status callback for a known terminal order as a conflicting observation requiring validation. Do not silently ignore a genuinely reopened or unexpected venue order merely because its ID is in the ledger. A fresh completed refresh that still reports the conflict must trigger the safety response.

   Request an immediate read-only IBKR refresh when the callback gap cannot be resolved from incoming events. Coordinate this with the existing periodic refresh in `engine.py:334`, using a single in-flight refresh. Correlate execution requests and completion callbacks with unique request IDs where supported. Serialize streams without request IDs and invalidate incomplete or overlapping reads; simply attaching a new generation number to an arriving callback is not proof that it belongs to the new request.

   Preserve snapshot age checks and the ledger revision check used for entry authorization. Extend revision tracking to IBKR execution-state changes, and ensure a read made inconsistent by concurrent updates cannot authorize entry. A fresh refresh that remains inconsistent, or a missed deadline, must escalate rather than retry forever.

5. **Make the state understandable**

   Distinguish "Awaiting IBKR updates" from "Safety pause: confirmed reconciliation mismatch" in the API reason and dashboard. Record the triggering order identity, expected and observed quantities, first-seen time, deadline, and resolution or escalation in the audit trail. Avoid repeated audit entries for duplicate callbacks.

   Preserve the cause and time of an actual safety pause so the dashboard can explain why it remains paused after reconciliation becomes CLEAN. Reuse existing audit and metadata facilities where possible; any persistent schema addition must be additive and compatible with opening-inventory records.

6. **Regression tests and acceptance criteria**

   First reproduce the incident with a deterministic fixture: a reconciled 23-contract opening position, a known one-contract order, then a 24-contract position callback before its execution. Use a controllable clock and mocked venue adapters; no real orders or live database writes are needed.

   | Scenario | Required result |
   | --- | --- |
   | Position before execution, execution before position, and Filled status before execution | UNKNOWN blocks new entry during the gap; exactly one execution and two hedge obligations; no lasting pause when evidence converges within the deadline. |
   | Terminal open-order callback; delayed working callback after terminal evidence | No inconsistent open-order membership; conflicting observations require validation; a confirmed live-order conflict remains blocking. |
   | Partial fills, duplicate callbacks, and a fill racing cancellation | Every real execution counted once; original quantity limits and capped hedge routing preserved. |
   | Required execution or position callback never arrives | Deadline expires even without further callbacks; pause/disarm and residual cancellation occur. |
   | Manual position change, wrong direction, excessive delta, foreign order/client, or unknown execution | Never CLEAN from timing tolerance; independent unexplained evidence takes the immediate safety path. |
   | New event during a refresh, late completion marker, disconnect, or callback loss | Stale/incomplete evidence cannot authorize entry; bounded recovery or the existing fault response applies. |
   | Polymarket mismatch, excess hedge, unresolved intent, or pending settlement | Existing reconciliation and hedge reservation protections remain effective. |
   | Manual pause or HALT during the callback gap | Subsequent CLEAN reconciliation does not re-arm or clear the operator action. |
   | Restart during an unresolved gap | Startup remains disarmed and requires fresh reconciliation; no replayed duplicate hedge. |
   | Position limit and economics | One-contract child limit, maximum position, minimum profit, return, and margin gates still apply after reconciliation clears. |

   Extend the coordinator, execution-safety, event-pipeline, state/API, and opening-inventory tests where relevant. Run the focused cases first, then the full Python suite, Ruff, and mypy. For dashboard changes, run its affected tests, lint, and production build. Use fixture replay in multiple callback orderings to demonstrate the same final ledger and hedge quantities.

7. **Implementation and release sequence**

   Implement failing incident tests first; then the shared order-state handling, bounded UNKNOWN classification, immediate refresh scheduling, and reason/audit display. Keep the existing opening-inventory and user configuration changes intact. No database purge or reconciliation override is required.

   After offline validation, build the deployment artifact and check that the new required environment setting is present in each intended deployment environment. Validate startup and recovery with order submission disabled and a copied test ledger or isolated paper environment. An existing running process needs a controlled restart to load the fix. Live activation remains an explicit operator action, and a previously latched pause is not automatically cleared by installing this change.

   Completion means the reproduced 23-to-24 fill sequence finishes with the correct ledger and hedges without an operator reset, while missing callbacks and genuinely unexplained exposure still produce a bounded, visible safety stop.
