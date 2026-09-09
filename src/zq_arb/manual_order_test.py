"""User-run, five-share CLOB test. This module never starts the strategy engine."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from polymarket.errors import RequestRejectedError
from pydantic import SecretStr, ValidationError

from zq_arb.adapters.polymarket import PolymarketAdapter, PolymarketProtocolError
from zq_arb.auth_diagnostic import check_auth
from zq_arb.config import Settings

TEST_SHARES = Decimal("5")


def positive_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("Expected a positive finite decimal.") from None
    if not number.is_finite() or number <= 0:
        raise argparse.ArgumentTypeError("Expected a positive finite decimal.")
    return number


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--env-file", type=Path, default=Path(".env"))
    cli.add_argument("--output-dir", type=Path, default=Path("runtime/manual-order-tests"))
    commands = cli.add_subparsers(dest="command", required=True)
    for name in ("preview", "place"):
        command = commands.add_parser(name)
        command.add_argument(
            "--leg",
            required=True,
            choices=["DEC50PLUS", "DEC25", "NO_CHANGE", "INC25", "INC50PLUS"],
        )
        command.add_argument("--outcome", choices=["YES", "NO"], default="YES")
        price = command.add_mutually_exclusive_group(required=True)
        price.add_argument("--price", type=positive_decimal)
        price.add_argument("--best-ask", action="store_true")
        command.add_argument("--max-price", type=positive_decimal, required=True)
        if name == "place":
            command.add_argument("--test-id", required=True)
            command.add_argument("--confirm-real-money", action="store_true")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("--order-id", required=True)
    cancel.add_argument("--test-id", required=True)
    cancel.add_argument("--confirm-real-money", action="store_true")
    return cli


def require_live_settings(settings: Settings, args: argparse.Namespace) -> None:
    if not args.confirm_real_money:
        raise PermissionError("Explicit --confirm-real-money is required.")
    if not settings.run_mode.is_live or not settings.live_trading_enabled:
        raise PermissionError("Requires LIMITED_LIVE or LIVE_ARMED and LIVE_TRADING_ENABLED=true.")
    if settings.simulate_polymarket_fills:
        raise PermissionError("SIMULATE_POLYMARKET_FILLS must be false for a real-money test.")
    if not settings.polymarket_order_submission_enabled:
        raise PermissionError("POLYMARKET_ORDER_SUBMISSION_ENABLED must be true.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", args.test_id):
        raise ValueError("Test ID must be 1-80 letters, digits, underscores or hyphens.")
    if args.command == "cancel" and (not args.order_id.strip() or args.order_id.startswith("SIM-")):
        raise ValueError("Cancellation requires an actual venue order ID.")


def safe_report(
    value: dict[str, Any], settings: Settings, extra_secrets: set[str] | None = None
) -> str:
    """No credentials, signed payloads or arbitrary SDK exception strings are logged."""
    rendered = json.dumps(value, indent=2, default=str)
    secrets = set(extra_secrets or ())
    for name in type(settings).model_fields:
        item = getattr(settings, name)
        if isinstance(item, SecretStr) and "address" not in name:
            secret = item.get_secret_value()
            if secret:
                secrets.add(secret)
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            rendered = rendered.replace(secret, "[REDACTED]")
    return rendered


async def preview(
    adapter: PolymarketAdapter, settings: Settings, args: argparse.Namespace
) -> dict[str, Any]:
    if args.max_price > Decimal("1"):
        raise ValueError("Maximum price cannot exceed 1 collateral unit per share.")
    if settings.polymarket_post_only:
        raise PermissionError("The existing hedge method requires POLYMARKET_POST_ONLY=false.")
    mapping = await adapter.verify_market_mapping()
    if not mapping.verified:
        raise PermissionError("Market mapping failed: " + "; ".join(mapping.errors))
    leg = next(leg for leg in settings.market_legs if leg.code == args.leg)
    token_id = leg.yes_token_id if args.outcome == "YES" else leg.no_token_id
    book = await adapter.fetch_book(token_id, market=leg.code)
    price = book.best_ask if args.best_ask else args.price
    if price is None or not price.is_finite() or price <= 0:
        raise ValueError("No valid limit price/best ask is available.")
    if price > min(args.max_price, settings.polymarket_emergency_max_price):
        raise PermissionError("Limit price exceeds --max-price or the configured price cap.")
    if book.tick_size is None or not book.tick_size.is_finite() or book.tick_size <= 0:
        raise ValueError("Venue did not provide a valid tick size.")
    if price % book.tick_size:
        raise ValueError(f"Price must be a multiple of the venue tick size {book.tick_size}.")
    if (
        book.min_order_size is None
        or not book.min_order_size.is_finite()
        or book.min_order_size <= 0
    ):
        raise ValueError("Venue did not provide a valid minimum order size.")
    if book.min_order_size > TEST_SHARES:
        raise PermissionError(
            f"Venue minimum is {book.min_order_size} shares; "
            f"this test is capped at {TEST_SHARES} shares."
        )
    return {
        "event": settings.polymarket_event_slug,
        "leg": leg.code,
        "market_slug": leg.slug,
        "outcome": args.outcome,
        "token_id": token_id,
        "side": "BUY",
        "order_type": "GTC",
        "shares": str(TEST_SHARES),
        "limit_price": str(price),
        "max_notional_excluding_fees": str(price * TEST_SHARES),
        "observed_best_ask": str(book.best_ask) if book.best_ask is not None else None,
        "min_order_size": str(book.min_order_size),
        "tick_size": str(book.tick_size),
    }


async def run(settings: Settings, args: argparse.Namespace) -> dict[str, Any]:
    if args.command != "preview":
        require_live_settings(settings, args)
    report: dict[str, Any] = {"command": args.command, "started_at": datetime.now(UTC).isoformat()}
    runtime_secrets: set[str] = set()
    journal: Path | None = None
    if args.command != "preview":
        args.output_dir.mkdir(parents=True, exist_ok=True)
        journal = args.output_dir / (args.test_id + ".json")
        # Exclusive creation also blocks overlapping runs with the same test ID.
        with journal.open("x", encoding="utf-8") as handle:
            handle.write(safe_report({**report, "status": "STARTED"}, settings))

    def save() -> None:
        if journal is not None:
            temporary = journal.with_suffix(".tmp")
            temporary.write_text(
                safe_report(report, settings, runtime_secrets) + "\n", encoding="utf-8"
            )
            temporary.replace(journal)

    submitted = False
    phase = "PREFLIGHT"
    try:
        async with PolymarketAdapter(settings) as adapter:
            if args.command in {"preview", "place"}:
                report["order"] = await preview(adapter, settings, args)
                if args.command == "preview":
                    report["status"] = "PREVIEW_ONLY"
                    return report
                eligibility = await adapter.check_eligibility()
                report["eligibility"] = eligibility.model_dump(mode="json")
                if not eligibility.permitted_for_live:
                    raise PermissionError(
                        "The adapter's location/eligibility check failed: " + eligibility.reason
                    )

            # Reuse the credential diagnostic to verify the existing wallet before
            # SDK initialization, which otherwise may try to deploy a missing wallet.
            report["authentication"] = await check_auth(settings)
            if any(
                report["authentication"][key]["status"] != "PASS" for key in ("test_2", "test_3")
            ):
                raise PermissionError(
                    "Wallet/authentication checks failed; see authentication responses."
                )
            if args.command == "place":
                order = report["order"]
                report["collateral"] = await adapter.trading_preflight(
                    Decimal(order["max_notional_excluding_fees"])
                )
            # Capture automatically derived credentials for error-message redaction.
            client = await adapter._authenticated_client()
            context = getattr(client, "_ctx", None)
            credentials = getattr(context, "credentials", None)
            for name in ("key", "secret", "passphrase"):
                value = getattr(credentials, name, None)
                if isinstance(value, str) and value:
                    runtime_secrets.add(value)
            if args.command == "place":
                phase = "PREPARING_ORDER"
                prepared = await adapter.prepare_hedge_limit(
                    token_id=order["token_id"],
                    limit_price=Decimal(order["limit_price"]),
                    shares=TEST_SHARES,
                    idempotency_key="manual-" + args.test_id,
                )
                if prepared.signed_payload is not None:
                    signature = prepared.signed_payload.get("signature")
                    if isinstance(signature, str) and signature:
                        runtime_secrets.add(signature)
                phase = "SUBMITTING_ORDER"
                report["status"] = "SUBMISSION_PENDING"
                save()
                submitted = True
                result = await adapter.post_prepared_hedge(prepared)
                if result.simulated or result.order_id.startswith("SIM-"):
                    raise RuntimeError("Unexpected simulated response in real-money test.")
                report["result"] = asdict(result)
                report["status"] = "VENUE_ACCEPTED"
                report["note"] = "Acceptance is not fill confirmation. Inspect order/trade history."
            else:
                phase = "CANCELING_ORDER"
                report["order_id"] = args.order_id
                report["status"] = "CANCELLATION_PENDING"
                save()
                submitted = True
                canceled = await adapter.cancel_order(args.order_id)
                report["canceled"] = canceled
                report["status"] = "CANCELED" if canceled else "CANCELLATION_NOT_CONFIRMED"
                report["note"] = "Cancellation cannot undo fills that already occurred."
    except Exception as error:
        report["status"] = "OUTCOME_UNKNOWN" if submitted else "NOT_SUBMITTED"
        report["failed_phase"] = phase
        rejected = isinstance(error, PolymarketProtocolError) and str(error).startswith(
            "Polymarket rejected hedge order ("
        )
        if rejected:
            report["status"] = "VENUE_REJECTED"
        if isinstance(error, RequestRejectedError):
            # Preserve the venue's status/code/message, not request auth headers.
            report["venue_error"] = {
                "http_status": error.status,
                "code": error.code,
                "message": str(error),
            }
            if submitted and 400 <= error.status < 500 and error.status != 409:
                # A duplicate response may refer to an already-existing order.
                duplicate = "duplicat" in (str(error) + str(error.code)).lower()
                if not duplicate:
                    report["status"] = (
                        "VENUE_REJECTED"
                        if args.command == "place"
                        else "CANCELLATION_NOT_CONFIRMED"
                    )
        # Permission/value messages are locally authored. SDK exception messages
        # may embed secrets, so keep their type and use auth diagnostic responses.
        report["error_type"] = type(error).__name__
        report["error"] = (
            str(error)
            if rejected
            or isinstance(error, RequestRejectedError)
            or (not submitted and isinstance(error, PermissionError | ValueError))
            else (
                "Adapter/SDK call failed. Check venue orders and trades before another placement."
            )
        )
        if submitted:
            report["retry"] = (
                "No automatic retry. Reconcile at Polymarket before using another test ID."
            )
    finally:
        save()
    return json.loads(safe_report(report, settings, runtime_secrets))  # type: ignore[no-any-return]


def main() -> int:
    args = parser().parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "configuration_errors": [
                        {
                            "field": ".".join(map(str, item["loc"])) or "settings",
                            "type": item["type"],
                            # Settings validators report field names and fixed rules,
                            # not supplied values. Omit input/context to protect secrets.
                            "message": item["msg"],
                        }
                        for item in error.errors(include_input=False, include_context=False)
                    ]
                }
            )
        )
        return 1
    try:
        report = asyncio.run(run(settings, args))
    except FileExistsError:
        print(
            json.dumps(
                {"error": "Test ID already exists. Read its journal before another attempt."}
            )
        )
        return 1
    except (PermissionError, ValueError) as error:
        print(safe_report({"error": str(error)}, settings))
        return 1
    print(safe_report(report, settings))
    return 0 if report["status"] in {"PREVIEW_ONLY", "VENUE_ACCEPTED", "CANCELED"} else 1
