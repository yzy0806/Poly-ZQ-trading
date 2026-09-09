"""Exercise the existing Polymarket BUY-limit method in local simulation only."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from zq_arb.adapters.polymarket import PolymarketAdapter
from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode


class PaperOnlyAdapter(PolymarketAdapter):
    async def _authenticated_client(self) -> Any:
        raise PermissionError("This script cannot initialize an authenticated venue client.")


def positive_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Expected a positive finite decimal.") from None
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive finite decimal.")
    return number


async def place_paper_order(
    settings: Settings, *, token_id: str, price: Decimal, shares: Decimal
) -> dict[str, Any]:
    if (
        settings.run_mode != RunMode.PAPER
        or settings.live_trading_enabled
        or not settings.simulate_polymarket_fills
    ):
        raise PermissionError(
            "Requires PAPER mode, LIVE_TRADING_ENABLED=false and SIMULATE_POLYMARKET_FILLS=true."
        )
    async with PaperOnlyAdapter(settings) as adapter:
        result = await adapter.submit_hedge_limit(
            token_id=token_id,
            limit_price=price,
            shares=shares,
            idempotency_key="paper-cli-" + uuid4().hex,
        )
    return {
        "mode": "LOCAL_SIMULATION",
        "venue_contacted": False,
        "token_id": token_id,
        "result": asdict(result),
        "note": "Synthetic immediate fill; no real order exists or is persisted by this script.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--token-id", default="PAPER-TEST-TOKEN")
    parser.add_argument("--price", type=positive_decimal, required=True)
    parser.add_argument("--shares", type=positive_decimal, default=Decimal("1"))
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "configuration_errors": [
                        {"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                        for item in error.errors(include_input=False, include_context=False)
                    ]
                }
            )
        )
        return 1
    try:
        result = asyncio.run(
            place_paper_order(
                settings, token_id=args.token_id, price=args.price, shares=args.shares
            )
        )
    except (PermissionError, ValueError, RuntimeError) as error:
        print(json.dumps({"local_error": str(error)}))
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
