from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select

from zq_arb.analytics.payoff import hedge_shares_per_contract
from zq_arb.config import Settings
from zq_arb.persistence.models import (
    BatchRecord,
    ExecutionEnvironmentRecord,
    ExecutionRecord,
    HedgeObligationRecord,
    OpeningInventoryRecord,
    OrderRecord,
    VenueEventRecord,
)


def evidence_hash(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def receipt_signature(venue: str, payload: dict[str, Any]) -> str | None:
    """Match immutable trade economics, never a timestamp range or unknown order."""
    if venue == "POLYMARKET":
        status = str(payload.get("status", "")).upper().removeprefix("TRADE_STATUS_")
        if status not in {"MATCHED", "MINED", "CONFIRMED"}:
            return None
        values = [
            payload.get("id"),
            payload.get("asset_id") or payload.get("token_id"),
            str(payload.get("side", "")).upper(),
            payload.get("size"),
            payload.get("price"),
        ]
    elif venue == "IBKR":
        values = [
            payload.get("exec_id"),
            payload.get("contract_month"),
            str(payload.get("side", "")).upper(),
            payload.get("shares"),
            payload.get("price"),
        ]
    else:
        return None
    if any(value is None or value == "" for value in values):
        return None
    try:
        for index in (3, 4):
            number = Decimal(str(values[index]))
            if not number.is_finite() or number <= 0:
                return None
            values[index] = str(number.normalize())
    except InvalidOperation:
        return None
    return evidence_hash({"venue": venue, "trade": values})


def validate_opening_inventory(
    record: OpeningInventoryRecord, settings: Settings, identity: dict[str, str]
) -> None:
    if record.identity != identity or record.evidence_hash != evidence_hash(record.snapshot):
        raise RuntimeError("Opening inventory identity or evidence hash does not match")
    snapshot = record.snapshot
    if not snapshot.get("captured_at") or not snapshot.get("source"):
        raise RuntimeError("Opening inventory lacks venue evidence")
    if snapshot.get("open_orders") != [] or snapshot.get("reads_complete") is not True:
        raise RuntimeError("Opening inventory requires complete venue reads and no open orders")
    positions = snapshot.get("positions", [])
    expected = {("IBKR", settings.ibkr_zq_contract_month)} | {
        ("POLYMARKET", leg.yes_token_id)
        for leg in settings.market_legs
        if leg.code in {"INC25", "INC50PLUS"}
    }
    quantities: dict[tuple[str, str], Decimal] = {}
    for item in positions:
        key = (item["venue"], item["instrument"])
        quantity = Decimal(item["quantity"])
        price = Decimal(item["average_price"])
        if (
            key not in expected
            or key in quantities
            or not quantity.is_finite()
            or quantity <= 0
            or not price.is_finite()
            or price <= 0
        ):
            raise RuntimeError("Invalid opening inventory position")
        quantities[key] = quantity
    if set(quantities) != expected:
        raise RuntimeError("Opening inventory must include ZQ and both hedge legs")
    zq = quantities[("IBKR", settings.ibkr_zq_contract_month)]
    if zq != zq.to_integral_value():
        raise RuntimeError("Opening ZQ quantity must be an integer")
    for leg in settings.market_legs:
        if leg.code in {"INC25", "INC50PLUS"}:
            bps = 25 if leg.code == "INC25" else 50
            if quantities[("POLYMARKET", leg.yes_token_id)] < hedge_shares_per_contract(bps) * zq:
                raise RuntimeError("Opening inventory is not fully hedged under the strategy model")
    receipts = snapshot.get("receipts")
    if not isinstance(receipts, dict) or set(receipts) != {"IBKR", "POLYMARKET"}:
        raise RuntimeError("Opening inventory lacks receipt evidence")
    for venue in receipts:
        source = snapshot.get("source_trades", {}).get(venue)
        if not isinstance(source, list):
            raise RuntimeError("Opening inventory lacks source trade history")
        signatures = set()
        for trade in source:
            if venue == "POLYMARKET" and str(trade.get("status", "")).upper() not in {
                "CONFIRMED",
                "TRADE_STATUS_CONFIRMED",
            }:
                raise RuntimeError("Opening inventory contains unsettled historical trades")
            signature = receipt_signature(venue, trade)
            if signature is None:
                raise RuntimeError("Opening inventory contains incomplete historical trades")
            signatures.add(signature)
        if signatures != set(receipts[venue]):
            raise RuntimeError("Opening inventory receipts do not match source trade history")


async def adopt_opening_inventory(
    repository: Any,
    snapshot: dict[str, Any],
    *,
    reason: str,
    allow_empty_account_correction: bool = False,
) -> bool:
    """One-time operator import. Caller stops the engine and backs up SQLite first."""
    database = repository.database
    if not reason.strip():
        raise ValueError("An operator reason is required")
    record = OpeningInventoryRecord(
        id=1,
        identity=snapshot.get("identity", {}),
        snapshot=snapshot,
        evidence_hash=evidence_hash(snapshot),
    )
    validate_opening_inventory(record, database.settings, database.identity)
    age = (datetime.now(UTC) - datetime.fromisoformat(snapshot["captured_at"])).total_seconds()
    if not 0 <= age <= 300:
        raise RuntimeError("Opening inventory requires venue evidence less than five minutes old")
    if snapshot.get("account_verified") is not True:
        raise RuntimeError("Opening inventory requires a verified broker account")
    async with database.session() as session:
        existing = await session.get(OpeningInventoryRecord, 1)
        if existing is not None:
            if existing.evidence_hash == record.evidence_hash:
                return False
            raise RuntimeError("Opening inventory is already present; cannot import twice")
        for model in (BatchRecord, OrderRecord, ExecutionRecord, HedgeObligationRecord):
            if await session.scalar(select(func.count()).select_from(model)):
                raise RuntimeError("Opening inventory import requires an empty execution ledger")
        environment = await session.get(ExecutionEnvironmentRecord, 1)
        if environment is not None and environment.schema_version != 1:
            raise RuntimeError("Opening inventory requires the current execution schema")
        previous_identity = environment.identity if environment else None
        if environment is not None and environment.identity != database.identity:
            changed = {
                key
                for key in environment.identity.keys() | database.identity.keys()
                if environment.identity.get(key) != database.identity.get(key)
            }
            if not allow_empty_account_correction or changed != {"ibkr_account"}:
                raise RuntimeError("Opening inventory cannot rebind this execution environment")
            environment.identity = database.identity
        elif environment is None:
            session.add(
                ExecutionEnvironmentRecord(
                    id=1,
                    schema_version=1,
                    identity=database.identity,
                )
            )
        receipts = (await session.scalars(select(VenueEventRecord))).all()
        acknowledged = 0
        for receipt in receipts:
            if receipt.processed:
                continue
            signature = receipt_signature(receipt.venue, receipt.payload)
            if signature not in snapshot["receipts"].get(receipt.venue, []):
                raise RuntimeError("Unmatched pending venue receipt prevents inventory import")
            receipt.processed = True
            acknowledged += 1
        session.add(record)
        await repository.audit(
            actor="OPERATOR",
            action="OPENING_INVENTORY_ADOPTED",
            reason=reason,
            details={
                "evidence_hash": record.evidence_hash,
                "previous_identity": previous_identity,
                "identity": database.identity,
                "positions": snapshot["positions"],
                "historical_receipts_accounted_for": acknowledged,
            },
        )
    database._environment_validated = False
    await database.validate_execution_environment()
    return True
