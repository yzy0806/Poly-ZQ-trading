from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from polymarket.errors import RequestRejectedError
from polymarket.models.clob.orders import SignedOrder
from pydantic import ValidationError

from zq_arb import manual_order_test as manual
from zq_arb.adapters.polymarket import PolymarketAdapter
from zq_arb.config import Settings
from zq_arb.domain.enums import RunMode
from zq_arb.domain.models import BookLevel, EligibilityStatus, OrderBook


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    async def blocked(*args: object, **kwargs: object) -> None:
        pytest.fail("Manual-order tests must never contact a venue")

    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "run_mode": RunMode.LIMITED_LIVE,
            "live_trading_enabled": True,
            "polymarket_order_submission_enabled": True,
            "simulate_polymarket_fills": False,
            "polymarket_post_only": False,
        }
    )


@pytest.fixture
def args(tmp_path: Path) -> argparse.Namespace:
    return manual.parser().parse_args(
        [
            "--output-dir",
            str(tmp_path),
            "place",
            "--leg",
            "INC50PLUS",
            "--best-ask",
            "--max-price",
            "0.50",
            "--test-id",
            "five-shares",
            "--confirm-real-money",
        ]
    )


@pytest.fixture
def venue(monkeypatch: pytest.MonkeyPatch, live_settings: Settings) -> SimpleNamespace:
    check_eligibility = PolymarketAdapter.check_eligibility
    signed = SignedOrder(
        builder="0x" + "00" * 32,
        expiration=0,
        maker="0x" + "11" * 20,
        maker_amount=1250000,
        metadata="0x" + "00" * 32,
        order_type="GTC",
        salt=123,
        side="BUY",
        signature="TEST-SIGNATURE-NEVER-LOG",
        signature_type=1,
        signer="0x" + "22" * 20,
        taker_amount=5000000,
        timestamp=123,
        token_id=live_settings.market_legs[-1].yes_token_id,
    )
    client = SimpleNamespace(
        create_limit_order=AsyncMock(return_value=signed),
        post_order=AsyncMock(
            return_value=SimpleNamespace(ok=True, order_id="venue-order-123", status="live")
        ),
        cancel_order=AsyncMock(return_value=SimpleNamespace(canceled=["venue-order-123"])),
    )
    monkeypatch.setattr(PolymarketAdapter, "_authenticated_client", AsyncMock(return_value=client))
    monkeypatch.setattr(
        PolymarketAdapter,
        "verify_market_mapping",
        AsyncMock(return_value=SimpleNamespace(verified=True, errors=())),
    )
    monkeypatch.setattr(
        PolymarketAdapter,
        "check_eligibility",
        AsyncMock(
            return_value=EligibilityStatus(
                checked=True, blocked=False, country="HK", permitted_for_live=True
            )
        ),
    )
    monkeypatch.setattr(
        PolymarketAdapter,
        "trading_preflight",
        AsyncMock(return_value={"allowance_sufficient": True}),
    )
    book = OrderBook(
        token_id=signed.token_id,
        asks=(BookLevel(price=Decimal("0.25"), size=Decimal("20")),),
        tick_size=Decimal("0.01"),
        min_order_size=Decimal("5"),
    )
    fetch = AsyncMock(return_value=book)
    monkeypatch.setattr(PolymarketAdapter, "fetch_book", fetch)
    auth = AsyncMock(return_value={"test_2": {"status": "PASS"}, "test_3": {"status": "PASS"}})
    monkeypatch.setattr(manual, "check_auth", auth)
    return SimpleNamespace(
        client=client, fetch=fetch, auth=auth, book=book, check_eligibility=check_eligibility
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "country,expected", [("HK", True), ("NL", True), ("US", False), (None, False)]
)
@pytest.mark.parametrize("blocked", [True, False, None])
async def test_order_uses_country_policy_and_journals_raw_eligibility(
    args: argparse.Namespace,
    live_settings: Settings,
    venue: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    country: str | None,
    expected: bool,
    blocked: bool | None,
) -> None:
    monkeypatch.setattr(PolymarketAdapter, "check_eligibility", venue.check_eligibility)
    monkeypatch.setattr(
        PolymarketAdapter, "_get", AsyncMock(return_value={"country": country, "blocked": blocked})
    )
    report = await manual.run(live_settings, args)
    assert report["status"] == ("VENUE_ACCEPTED" if expected else "NOT_SUBMITTED")
    assert report["eligibility"]["blocked"] is blocked
    assert report["eligibility"]["country"] == country
    assert report["eligibility"]["permitted_for_live"] is expected
    journal = json.loads((args.output_dir / "five-shares.json").read_text())
    assert journal["eligibility"] == report["eligibility"]
    if expected:
        venue.client.post_order.assert_awaited_once()
    else:
        venue.client.post_order.assert_not_awaited()
        venue.auth.assert_not_awaited()
        assert report["eligibility"]["reason"] in report["error"]


@pytest.mark.asyncio
async def test_uses_real_adapter_methods_and_journals_once(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    report = await manual.run(live_settings, args)
    assert report["status"] == "VENUE_ACCEPTED"
    venue.client.create_limit_order.assert_awaited_once_with(
        token_id=live_settings.market_legs[-1].yes_token_id,
        price=Decimal("0.25"),
        size=Decimal("5"),
        side="BUY",
        post_only=False,
    )
    venue.client.post_order.assert_awaited_once()
    journal = args.output_dir / "five-shares.json"
    assert json.loads(journal.read_text())["result"]["order_id"] == "venue-order-123"
    assert "TEST-SIGNATURE" not in journal.read_text()
    assert report["order"]["shares"] == "5"
    assert report["order"]["max_notional_excluding_fees"] == "1.25"
    PolymarketAdapter.trading_preflight.assert_awaited_once_with(Decimal("1.25"))
    with pytest.raises(FileExistsError):
        await manual.run(live_settings, args)
    venue.client.post_order.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"run_mode": RunMode.PAPER},
        {"live_trading_enabled": False},
        {"simulate_polymarket_fills": True},
        {"polymarket_order_submission_enabled": False},
    ],
)
async def test_live_gates_before_network(
    args: argparse.Namespace, live_settings: Settings, updates: dict[str, object]
) -> None:
    with pytest.raises(PermissionError):
        await manual.run(live_settings.model_copy(update=updates), args)


@pytest.mark.asyncio
async def test_explicit_confirmation_required(
    args: argparse.Namespace, live_settings: Settings
) -> None:
    args.confirm_real_money = False
    with pytest.raises(PermissionError):
        await manual.run(live_settings, args)


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum,ask", [("6", "0.25"), ("5", "0.51"), ("5", "0.255")])
async def test_minimum_cap_and_tick_block_submission(
    args: argparse.Namespace,
    live_settings: Settings,
    venue: SimpleNamespace,
    minimum: str,
    ask: str,
) -> None:
    venue.fetch.return_value = venue.book.model_copy(
        update={
            "min_order_size": Decimal(minimum),
            "asks": (BookLevel(price=Decimal(ask), size=Decimal("20")),),
        }
    )
    report = await manual.run(live_settings, args)
    assert report["status"] == "NOT_SUBMITTED"
    venue.client.post_order.assert_not_awaited()
    venue.auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_never_retried(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    venue.client.post_order.side_effect = httpx.ReadTimeout("Sensitive SDK details")
    report = await manual.run(live_settings, args)
    assert report["status"] == "OUTCOME_UNKNOWN"
    assert "Sensitive" not in json.dumps(report)
    venue.client.post_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_venue_rejection_is_distinct_from_timeout(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    venue.client.post_order.return_value = SimpleNamespace(
        ok=False, code="INSUFFICIENT_BALANCE", message="Insufficient balance"
    )
    report = await manual.run(live_settings, args)
    assert report["status"] == "VENUE_REJECTED"
    assert "INSUFFICIENT_BALANCE" in report["error"]
    venue.client.post_order.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,message,expected",
    [
        (400, "invalid amount for a marketable BUY order ($0.01), min size: $1", "VENUE_REJECTED"),
        (503, "service unavailable", "OUTCOME_UNKNOWN"),
        (400, "order duplicated", "OUTCOME_UNKNOWN"),
    ],
)
async def test_sdk_rejection_keeps_venue_details(
    args: argparse.Namespace,
    live_settings: Settings,
    venue: SimpleNamespace,
    status: int,
    message: str,
    expected: str,
) -> None:
    venue.client.post_order.side_effect = RequestRejectedError(
        message, status=status, code="venue-code"
    )
    report = await manual.run(live_settings, args)
    assert report["status"] == expected
    assert report["failed_phase"] == "SUBMITTING_ORDER"
    assert report["venue_error"] == {
        "http_status": status,
        "code": "venue-code",
        "message": message,
    }
    venue.client.post_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_preparation_rejection_proves_no_post(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    venue.client.create_limit_order.side_effect = RequestRejectedError(
        "Metadata not found", status=404
    )
    report = await manual.run(live_settings, args)
    assert report["status"] == "NOT_SUBMITTED"
    assert report["failed_phase"] == "PREPARING_ORDER"
    assert report["venue_error"]["http_status"] == 404
    venue.client.post_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_error_echo_of_signature_is_redacted(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    venue.client.post_order.side_effect = RequestRejectedError(
        "Invalid signature TEST-SIGNATURE-NEVER-LOG", status=400
    )
    report = await manual.run(live_settings, args)
    assert report["venue_error"]["message"] == "Invalid signature [REDACTED]"
    assert "TEST-SIGNATURE-NEVER-LOG" not in (args.output_dir / "five-shares.json").read_text()


@pytest.mark.asyncio
async def test_derived_credential_echo_is_redacted_on_cancellation(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    args.command = "cancel"
    args.order_id = "venue-order-123"
    venue.client._ctx = SimpleNamespace(
        credentials=SimpleNamespace(
            key="derived-key-value",
            secret="derived-secret-value",  # noqa: S106 - synthetic redaction fixture
            passphrase="derived-passphrase-value",  # noqa: S106 - synthetic redaction fixture
        )
    )
    venue.client.cancel_order.side_effect = RequestRejectedError(
        "Invalid key derived-key-value", status=401
    )
    report = await manual.run(live_settings, args)
    assert report["venue_error"]["message"] == "Invalid key [REDACTED]"
    assert "derived-key-value" not in (args.output_dir / "five-shares.json").read_text()


@pytest.mark.asyncio
async def test_failed_authentication_never_places(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace
) -> None:
    venue.auth.return_value = {"test_2": {"status": "PASS"}, "test_3": {"status": "FAIL"}}
    report = await manual.run(live_settings, args)
    assert report["status"] == "NOT_SUBMITTED"
    venue.client.post_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("canceled", [True, False])
async def test_cancel_uses_only_selected_id(
    args: argparse.Namespace, live_settings: Settings, venue: SimpleNamespace, canceled: bool
) -> None:
    args.command = "cancel"
    args.order_id = "venue-order-123"
    venue.client.cancel_order.return_value.canceled = [args.order_id] if canceled else []
    report = await manual.run(live_settings, args)
    assert report["status"] == ("CANCELED" if canceled else "CANCELLATION_NOT_CONFIRMED")
    venue.client.cancel_order.assert_awaited_once_with(order_id="venue-order-123")
    venue.client.post_order.assert_not_awaited()
    venue.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_never_authenticates_or_places(
    args: argparse.Namespace, settings: Settings, venue: SimpleNamespace
) -> None:
    args.command = "preview"
    settings = settings.model_copy(update={"polymarket_post_only": False})
    report = await manual.run(settings, args)
    assert report["status"] == "PREVIEW_ONLY"
    venue.auth.assert_not_awaited()
    venue.client.post_order.assert_not_awaited()
    assert not list(args.output_dir.glob("*.json"))


def test_cli_reports_actual_configuration_rules_without_inputs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only load the checked-in example, never the user's credentials or network.
    with pytest.raises(ValidationError) as caught:
        Settings(
            _env_file=Path("deploy/zq-arb.env.example"),
            run_mode=RunMode.LIMITED_LIVE,
            live_trading_enabled=True,
            simulate_polymarket_fills=True,
            operator_approval_id="",
            polymarket_private_key="sensitive-test-value-must-not-be-printed",
        )

    def invalid_settings(**kwargs: object) -> Settings:
        raise caught.value

    monkeypatch.setattr(manual, "Settings", invalid_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "test_polymarket_order.py",
            "preview",
            "--leg",
            "INC50PLUS",
            "--best-ask",
            "--max-price",
            "0.8",
        ],
    )
    assert manual.main() == 1
    output = capsys.readouterr().out
    errors = json.loads(output)["configuration_errors"]
    assert any(item["field"] == "settings" for item in errors)
    assert "OPERATOR_APPROVAL_ID is absent" in output
    assert "SIMULATE_POLYMARKET_FILLS must be false" in output
    assert "sensitive-test-value-must-not-be-printed" not in output
    assert all("input" not in item and "ctx" not in item for item in errors)
