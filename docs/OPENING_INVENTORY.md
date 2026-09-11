Existing venue holdings can be adopted once into an empty execution ledger with
`scripts/adopt_opening_inventory.py`. The import is an opening balance, with venue
average costs and captured source history; it does not fabricate orders or fills.

1. Stop the engine and confirm the intended live IBKR account, wallet, contract
   month, and position cap in `.env`. The collector reads both venues, requires
   complete and stable balances, no open orders, and settled Polymarket history.
   Both PM legs must cover the existing ZQ position under the strategy model.

2. Run a capture from the repository root:

   ```powershell
   uv run python scripts/adopt_opening_inventory.py --output-dir <backup-directory>
   ```

   Review the evidence before applying. To collect a fresh snapshot and import it,
   add `--apply --reason "<operator explanation>"`. A verified SQLite backup is
   created before modifying the database. `--correct-empty-account` explicitly
   permits correcting only the broker-account fingerprint in an otherwise empty
   execution ledger; other identity differences and existing execution records
   remain prohibited.

3. Start the engine using the source revision that supports `opening_inventory`.
   It starts disarmed and requires fresh venue reconciliation. The import itself
   never sets reconciliation to clean or arms trading. A startup evidence checksum
   and the existing environment validation protect the recorded opening balance.

4. The position cap includes the imported ZQ balance plus subsequent strategy
   executions. Known historical receipts are recognized by their verified trade
   identity and economics, so replay does not duplicate positions or create hedge
   obligations. Unknown, changed, or failed trade events still block reconciliation.
   Manual position changes after import must be investigated, not silently adopted.

Cost basis uses the venue-reported average prices at their available precision.
Importing an opening balance does not reconstruct prior realized P&L or commissions.
Keep the backup, JSON source evidence, and audit entry together. The database is
external to the application image; deploying an older image without opening-balance
support will not reconcile these imported holdings.
