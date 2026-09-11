"""Read verified venue inventory; optionally adopt it into an empty, stopped ledger."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sqlite3
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from zq_arb.adapters.ibkr import _load_official_api
from zq_arb.adapters.polymarket import PolymarketAdapter
from zq_arb.config import Settings, get_settings
from zq_arb.persistence.database import Database, execution_identity
from zq_arb.persistence.models import OpeningInventoryRecord
from zq_arb.persistence.opening_inventory import (
    adopt_opening_inventory,
    evidence_hash,
    receipt_signature,
    validate_opening_inventory,
)
from zq_arb.persistence.repository import Repository


def broker_inventory(settings: Settings) -> dict[str, Any]:
    api = _load_official_api(settings.ibkr_python_api_path)
    account = settings.ibkr_account_id.get_secret_value()
    month = settings.ibkr_zq_contract_month

    class Reader(api.wrapper.EWrapper, api.client.EClient):
        def __init__(self):
            api.wrapper.EWrapper.__init__(self)
            api.client.EClient.__init__(self, self)
            self.ready = threading.Event()
            self.accounts_done = threading.Event()
            self.positions_done = threading.Event()
            self.orders_done = threading.Event()
            self.executions_done = threading.Event()
            self.accounts = []
            self.positions = []
            self.orders = []
            self.trades = []

        def nextValidId(self, orderId):
            self.ready.set()

        def managedAccounts(self, accountsList):
            self.accounts = accountsList.split(",")
            self.accounts_done.set()

        def position(self, acct, contract, position, avgCost):
            if (
                acct == account
                and contract.symbol == "ZQ"
                and contract.lastTradeDateOrContractMonth.startswith(month)
                and Decimal(position) != 0
            ):
                self.positions.append(
                    {
                        "venue": "IBKR",
                        "instrument": month,
                        "quantity": str(position),
                        "average_price": str(Decimal(str(avgCost)) / Decimal(contract.multiplier)),
                        "cost_source": "TWS position average cost / contract multiplier",
                        "contract_id": contract.conId,
                        "local_symbol": contract.localSymbol,
                    }
                )

        def positionEnd(self):
            self.positions_done.set()

        def openOrder(self, orderId, contract, order, orderState):
            if contract.symbol == "ZQ" and contract.lastTradeDateOrContractMonth.startswith(month):
                self.orders.append({"venue": "IBKR", "order_id": orderId})

        def openOrderEnd(self):
            self.orders_done.set()

        def execDetails(self, reqId, contract, execution):
            if (
                execution.acctNumber == account
                and contract.symbol == "ZQ"
                and contract.lastTradeDateOrContractMonth.startswith(month)
            ):
                self.trades.append(
                    {
                        "exec_id": execution.execId,
                        "contract_month": contract.lastTradeDateOrContractMonth,
                        "side": execution.side,
                        "shares": str(execution.shares),
                        "price": str(execution.price),
                        "time": execution.time,
                        "order_id": execution.orderId,
                        "client_id": execution.clientId,
                    }
                )

        def execDetailsEnd(self, reqId):
            self.executions_done.set()

        def error(self, *args):
            pass  # Completion callbacks below are mandatory; never print account details.

    reader = Reader()
    try:
        reader.connect(settings.ibkr_host, settings.ibkr_port, clientId=914612)
        thread = threading.Thread(target=reader.run, daemon=True)
        thread.start()
        if not reader.ready.wait(15):
            raise RuntimeError("TWS handshake incomplete")
        reader.reqManagedAccts()
        reader.reqPositions()
        reader.reqAllOpenOrders()
        request = api.execution.ExecutionFilter()
        request.acctCode = account
        reader.reqExecutions(9101, request)
        for completed in (
            reader.accounts_done,
            reader.positions_done,
            reader.orders_done,
            reader.executions_done,
        ):
            if not completed.wait(15):
                raise RuntimeError("TWS inventory read incomplete")
        if account not in reader.accounts or account.startswith("DU"):
            raise RuntimeError("Configured live account was not verified by TWS")
        return {
            "positions": reader.positions,
            "open_orders": reader.orders,
            "trades": reader.trades,
        }
    finally:
        reader.disconnect()


async def capture(settings: Settings) -> dict[str, Any]:
    if settings.ibkr_trading_mode != "live" or settings.simulate_polymarket_fills:
        raise RuntimeError("This operator import requires real live venue inventory")
    started = datetime.now(UTC)
    broker = await asyncio.to_thread(broker_inventory, settings)
    async with PolymarketAdapter(settings) as adapter:
        pm = await adapter.account_snapshot()
        second_positions = await adapter.current_event_positions()

    def balances(rows):
        return sorted((str(row["asset"]), str(row["size"]), str(row["avgPrice"])) for row in rows)

    if not pm.trades_complete or balances(pm.positions) != balances(second_positions):
        raise RuntimeError("Polymarket inventory is incomplete or changed during capture")
    second_broker = await asyncio.to_thread(broker_inventory, settings)
    if broker != second_broker:
        raise RuntimeError("TWS inventory changed during capture")
    tokens = {
        token for leg in settings.market_legs for token in (leg.yes_token_id, leg.no_token_id)
    }
    positions = broker["positions"] + [
        {
            "venue": "POLYMARKET",
            "instrument": str(item["asset"]),
            "quantity": str(item["size"]),
            "average_price": str(item["avgPrice"]),
            "cost_source": "Polymarket positions API average price (venue precision)",
        }
        for item in pm.positions
        if Decimal(str(item["size"])) != 0
    ]
    sources = {
        "IBKR": broker["trades"],
        "POLYMARKET": [
            trade
            for trade in pm.trades
            if str(trade.get("asset_id") or trade.get("token_id")) in tokens
        ],
    }
    snapshot = {
        "captured_at": started.isoformat(),
        "source": "TWS and authenticated Polymarket account reads",
        "identity": execution_identity(settings),
        "account_verified": True,
        "reads_complete": True,
        "positions": positions,
        "open_orders": broker["open_orders"] + list(pm.open_orders),
        "source_trades": sources,
        "receipts": {
            venue: sorted({receipt_signature(venue, trade) for trade in trades})
            for venue, trades in sources.items()
        },
    }
    validate_opening_inventory(
        OpeningInventoryRecord(
            identity=execution_identity(settings),
            snapshot=snapshot,
            evidence_hash=evidence_hash(snapshot),
        ),
        settings,
        execution_identity(settings),
    )
    return snapshot


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--correct-empty-account", action="store_true")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    settings = get_settings()
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", settings.api_port)) == 0:
            raise RuntimeError("Stop the engine before capturing opening inventory")
    snapshot = await capture(settings)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence = args.output_dir / f"opening-inventory-{timestamp}.json"
    evidence.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    summary = {
        "evidence_file": str(evidence),
        "positions": snapshot["positions"],
        "historical_trade_counts": {
            venue: len(trades) for venue, trades in snapshot["source_trades"].items()
        },
        "applied": False,
    }
    if args.apply:
        source_path = Path(make_url(settings.database_url).database)
        backup = args.output_dir / f"before-opening-inventory-{timestamp}.sqlite3"
        with sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True) as source:
            with sqlite3.connect(backup) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Database backup integrity failed")
        database = Database(settings)
        try:
            await database.initialize()
            summary["applied"] = await adopt_opening_inventory(
                Repository(database),
                snapshot,
                reason=args.reason,
                allow_empty_account_correction=args.correct_empty_account,
            )
        finally:
            await database.close()
        summary["backup_file"] = str(backup)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
