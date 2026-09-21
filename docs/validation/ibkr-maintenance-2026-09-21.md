# IBKR refresh timeout and scheduled maintenance

Implementation is local and has not been deployed to the VPS. The previously deployed
callback-convergence fix remains separate from this release.

1. **Deadlines.** `IBKR_ACCOUNT_REFRESH_TIMEOUT_SECONDS=30` is a required independent
   setting (1–120 seconds). Both account-refresh watchdog paths use it, including a last
   snapshot end marker arriving after the deadline. Execution/PM request timeouts stay at
   10 seconds and fill-callback convergence stays at 2 seconds. Timeout logs name the missing
   snapshot completion markers. Late markers cannot validate a timed-out read.

2. **Regular schedule.** New entry is blocked from 15:59 through 17:00 America/Chicago
   with the example settings. The engine stays running and never changes operator arming
   permission on successful maintenance completion. Friday's closure continues through
   Sunday 17:00; the daily Gateway maintenance routine also runs during the weekend.
   Chicago daylight saving is handled with `zoneinfo`. Special holiday/shortened-session
   calendars are outside this release; the regular schedule must not be represented as
   a complete exchange calendar. Existing FOMC and opportunity gates remain in force.
   In Taipei/Shanghai, the regular break is 05:00–06:00 during Chicago daylight time and
   06:00–07:00 during standard time, with the drain starting one minute earlier.

3. **Drain and safety.** Working ZQ orders are cancelled using the existing coordinator.
   A drain requires confirmed terminal orders, no active batch or unresolved hedge work,
   and fresh, clean reconciliation before 16:00. Fully hedged positions are not liquidated.
   Missing confirmation latches a safety pause. Late fills remain subject to the existing
   execution ledger, hedge mandate and callback deadline. A timeout exception never skips
   independent order/identity/hedge faults or clears a safety pause.

4. **Expected restart recovery.** Only a drained maintenance cycle with an interruption
   beginning between 16:09 and 16:12 qualifies for the example 16:10 Gateway restart.
   Timed-out account reads remain invalid and trigger reconnection; the expected episode
   does not itself disarm the engine. Retry backoff is bounded at 15 seconds and the episode
   cannot extend beyond 17:05. A critical IBKR 1100 connectivity alert from this specific
   episode can be resolved after a restored connection and fresh full reconciliation;
   older connectivity alerts and unrelated critical alerts are not excused. The exception
   ends on recovery, so later unrelated faults use normal safety behavior.
   The first interruption time is retained across retries, including IBKR 1100 events;
   an earlier unrelated outage cannot acquire the exception by continuing into the window.

5. **Reopening and controls.** Maintenance completion removes only the entry hold.
   Disarm, Pause and Halt are never undone. No profitable opportunity is required to return
   to armed waiting, but orders still require qualified subscriptions, PM books, margin,
   reconciliation, strategy timing and economics. The runtime waits for its connection
   callbacks to be processed before starting a fresh IBKR account read. If recovery is late,
   it may complete automatically before 17:05; expiry is a latched safety failure. New engine
   processes start disarmed and do not recover arming permission from maintenance audits.

6. **Visibility.** Existing audits record MAINTENANCE_STARTED, MAINTENANCE_COMPLETED and
   the usual RECONCILIATION_SAFETY_PAUSE with a specific drain/recovery failure reason.
   Snapshot metadata and the header expose the maintenance hold and reopening timestamp.
   These records are diagnostics, not persistent trading authorization.

7. **Configuration and rollout.** Add the eight new settings from `deploy/zq-arb.env.example`
   before starting the new image. Verify Gateway actually uses 16:10 America/Chicago. Keep
   the existing execution and callback timeouts unchanged. Back up the environment and
   ledger on the VPS, drain the engine through its existing shutdown procedure, deploy the
   immutable reviewed artifact, and confirm configuration, venue recovery and reconciliation.
   This deployment itself starts disarmed; normal daily Gateway restarts preserve an already
   armed engine. Roll back using both the previous image and its matching environment file:
   the old environment validator rejects these new keys. No database migration is needed.

8. **Validation.** Tests use real temporary SQLite ledgers and controlled time, plus fake
   venue transport. They exercise partial fills during cancellation, both timeout paths,
   late markers, expected/unrelated connectivity alerts, repeated reconnect attempts,
   clean and delayed reopening, stale snapshots, operator-control races, startup, weekends
   and daylight saving. The Python environment is isolated under `/private/tmp` using the
   repository's locked dependency versions. Frontend validation uses a temporary macOS
   copy because the workspace dependencies were originally installed on Windows.

   Final results on macOS/Python 3.12: 399 Python tests passed, Ruff passed, and mypy passed
   for all 38 source files. Dashboard validation against `package-lock.json` passed all
   18 tests in 7 files, ESLint, TypeScript compilation and the Vite production build.
   Environment-schema loading and `git diff --check` also passed. These checks do not
   establish a live Gateway restart/reopening result; that remains a post-deployment observation.
