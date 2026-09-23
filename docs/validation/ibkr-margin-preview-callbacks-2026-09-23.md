# Margin-preview callback investigation — September 23, 2026

Status: deployed as `00d9368` after the owner committed, pushed and authorized deployment.
The investigation itself was local; Codex did not commit or push the fix. Investigation
baseline: `4a63f59` (production code `e404b1c`).

## Production evidence and its limits

The September 22 production audit recorded the following sequence (UTC):

| Time | Evidence |
| --- | --- |
| 15:48:34 | Operator ARM action |
| 15:48:37 | Strategy BUY-5 order 4354 submitted at 96.1 |
| 15:54:58 | Residual cancellation requested; the return-on-capital value was unavailable |
| 15:55:03 | Safety pause for `unexpected_ibkr_orders: [4361]` |
| 15:55:15 | Authenticated reconciliation returned to CLEAN |

Order 4354 was CANCELLED, with zero executions or hedge obligations. At the subsequent
15:59 read, reconciliation was CLEAN and the margin preview CURRENT, while the safety pause
remained latched. The intermittent Polymarket HTTP 429 errors were a separate observation.

The old runtime did not persist preview-request IDs or the offending open/status callback.
Consequently, the retained evidence does **not** conclusively prove that production ID 4361
was a what-if request. The reproduced failure below produces the same type of pause, but
must not be described as a recovered recording of that production callback.

## Reproduced defects

1. After preview cleanup removed its active context, a late `openOrder` callback was emitted
   as an ordinary order. The handler ignored both the completed-preview IDs and `whatIf`.
2. The first error for a completed preview removed its ID from the suppression list. Later
   duplicate status callbacks could therefore enter live-order reconciliation.
3. The 100-entry completed-ID deque eventually forgot old requests even without an error.
4. Status filtering used the numeric order ID alone and could hide another API client's
   unrelated order with the same number.
5. A response already queued when the preview timed out could change FAILED back to AVAILABLE.

The adapter defects failed focused tests against the original implementation. The timeout
test also failed against a temporary read-only export of the original source after validating
the October fixture configuration. The old result was AVAILABLE despite the preceding timeout.

## Correction

- Record locally issued preview IDs for the adapter's lifetime, including reconnects. Only
  integer IDs remain after completion; the pending request context is released. This trades
  gradual growth of a small ID set for avoiding arbitrary expiry of callback identities.
- Match status callbacks by API client and issued ID. For contract-bearing open/completed
  callbacks, also require `whatIf=True` and the exact generated preview reference. Conflicting
  live-order evidence removes preview classification and remains visible to reconciliation.
- Finish previews locally without a `cancelOrder` request. IBKR describes a what-if as a
  credit check that does not route the proposed order to a destination, returning its margin
  estimate through `OrderState`; see [IBKR margin checking](https://interactivebrokers.github.io/tws-api/margin.html)
  and the [current Order reference](https://www.interactivebrokers.com/docs/tws-api/ref/order).
  The ordinary live-order cancellation path is unchanged.
- Preserve preview identity across repeated cleanup acknowledgements, completed callbacks,
  failed sends and reconnects. Genuine credit rejections remain failures; executions and
  connection errors remain visible.
- Accept margin results only for the still-PENDING request, checking again under the state
  update lock. A timeout, rejection or newer request cannot be overwritten by a queued reply.
- Log preview request/finish IDs, late open callbacks and identity conflicts without account
  credentials, so a future incident can be matched to its original request.

The fixed paths are exercised with mocked IBKR callbacks and a temporary SQLite ledger.
The integration case remains CLEAN through delayed preview callbacks, then pauses when the
same test injects a genuine unexpected live order. No broker connection is opened by these tests.

## Validation

- 22 new callback/lifecycle regression cases, including both error/open-callback orderings.
- Full backend suite: **463 passed**, **87.99% coverage**, exceeding the 85% gate.
- Ruff passed for `src`, `tests` and `scripts`; MyPy passed for all 39 source files.
- `git diff --check` passed. The dashboard was not modified.

The suite ran with the native Python 3.14.7 environment. Temporary test databases and coverage
output were kept outside the repository; test settings use fixture credentials and mocked venues.

## Authorized production deployment

The owner pushed `00d9368b61373ad66f5ba4efa8e14d6b0b0664b8` and authorized deployment.
[GitHub Actions](https://github.com/yzy0806/Poly-ZQ-trading/actions/runs/35753060635) passed
Ubuntu validation, macOS validation and image publication. The immutable image is
`ghcr.io/yzy0806/poly-zq-trading@sha256:1dc2488685aa0d631c65e0781bf5687ad9e378ff88cf15799f02aaafbad724a0`.
The final container started September 22 at 16:23:37 UTC (September 23 at 00:23:37 UTC+8).

Three consecutive automatic BUY-5 October margin previews at 96.10 completed and qualified
CURRENT. Each request has a matching finish log. Throughout the sampled observation the
engine remained disarmed and unpaused; routine account refreshes briefly entered UNKNOWN
and returned to CLEAN. No safety pause or new executable strategy order occurred. Final
health/readiness checks passed, all ten books were synchronized, and there were no current
alerts, callback identity conflicts, event-consumer failures or log-level errors.

The existing October database was preserved: one completed batch, cancelled order 4354,
zero executions and zero hedge obligations. Trading-record fingerprints matched the
original backup, excluding only the routinely refreshed `batches.updated_at` timestamp.
An initial overly strict timestamp comparison triggered a rollback; row-by-row comparison
confirmed that the timestamp was the sole difference before the successful retry. A prior
pre-stop check also waited for reconciliation to return CLEAN before changing production.
Only `SOFTWARE_VERSION` and `CONFIG_VERSION` changed in the production env. Gateway was
not restarted and no arming or live order/cancellation command was sent.

This short observation verifies ordinary production preview cycles. The deliberately late
and reordered callback cases remain supported by the offline regression tests; the original
4361 callback identity remains unproven. See the [sanitized deployment evidence](production-margin-preview-2026-09-23.json).
